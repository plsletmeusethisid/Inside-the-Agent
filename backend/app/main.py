"""Document ingestion and inspection API."""

import os
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

import psycopg
import pymupdf
from fastapi import FastAPI, File, Form, HTTPException, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from starlette.concurrency import run_in_threadpool
from starlette.responses import Response

from app.ingestion import chunk_pages, parse_pdf
from app.database import apply_schema
from app.embeddings import PREVIEW_SIZE, embedding_summary, process_embeddings
from app.retrieval import RetrievalRequest, retrieve
from app.generation import generate_answer
from app.tracing import Trace, trace_response
from app.config import read_secret, SecretConfigurationError

MAX_PDF_BYTES = 10 * 1024 * 1024


def database_connection() -> psycopg.Connection:
    try:
        database_url = read_secret("DATABASE_URL")
    except SecretConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    if not database_url:
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")
    try:
        return psycopg.connect(database_url, connect_timeout=3)
    except psycopg.Error as exc:
        raise HTTPException(status_code=503, detail="Database is unavailable") from exc


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Also applies new tables to a pgvector volume created in milestone one.
    with database_connection() as connection:
        apply_schema(connection)
    yield


app = FastAPI(title="Agent Pipeline API", version="0.6.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[os.getenv("FRONTEND_ORIGIN", "http://localhost:3000")],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


@app.get("/health")
def health() -> dict[str, str]:
    """Process liveness; independent of database availability."""
    return {"status": "ok", "service": "agent-pipeline-api"}


@app.get("/ready")
def ready() -> dict[str, str]:
    """Verify PostgreSQL, pgvector, and the document schema."""
    try:
        with database_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT extversion, to_regclass('public.chunks') FROM pg_extension WHERE extname = 'vector'")
                row = cursor.fetchone()
        if row is None or row[1] is None:
            raise HTTPException(status_code=503, detail="Database schema is not initialized")
        return {"status": "ready", "database": "connected", "pgvector": row[0]}
    except psycopg.Error as exc:
        raise HTTPException(status_code=503, detail="Database is unavailable") from exc


@app.post("/documents", status_code=status.HTTP_201_CREATED)
async def upload_document(
    file: UploadFile = File(...),
    chunk_size: int = Form(500, ge=50, le=1200),
    chunk_overlap: int = Form(75, ge=0, le=300),
) -> dict:
    filename, data = await read_upload(file, chunk_size, chunk_overlap)
    return await run_in_threadpool(ingest_document, filename, data, chunk_size, chunk_overlap, uuid4())


@app.post("/documents/stream")
async def stream_upload(
    file: UploadFile = File(...),
    chunk_size: int = Form(500, ge=50, le=1200),
    chunk_overlap: int = Form(75, ge=0, le=300),
):
    filename, data = await read_upload(file, chunk_size, chunk_overlap)
    document_id = uuid4()
    return trace_response(lambda trace: ingest_document(filename, data, chunk_size, chunk_overlap, document_id, trace), document_id)


async def read_upload(file, chunk_size, chunk_overlap):
    """Parse a selectable-text PDF and persist pages and page-aware chunks."""
    if chunk_overlap >= chunk_size:
        raise HTTPException(status_code=422, detail="Overlap must be smaller than chunk size")
    filename = (file.filename or "document.pdf").replace("\\", "/").split("/")[-1]
    if not filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=415, detail="Only PDF files are supported")
    data = await file.read(MAX_PDF_BYTES + 1)
    await file.close()
    if len(data) > MAX_PDF_BYTES:
        raise HTTPException(status_code=413, detail="PDF exceeds the 10 MB limit")
    if not data.startswith(b"%PDF-"):
        raise HTTPException(status_code=415, detail="File is not a PDF")
    return filename, data


def ingest_document(filename, data, chunk_size, chunk_overlap, document_id, trace=None):
    trace = trace or Trace()
    started = trace.start("parse")

    try:
        pages = parse_pdf(data)
    except (pymupdf.FileDataError, ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=422, detail="PDF could not be parsed") from exc
    if not pages:
        raise HTTPException(status_code=422, detail="PDF has no pages")
    if not any(page.content for page in pages):
        raise HTTPException(status_code=422, detail="No selectable text found; scanned PDFs require OCR")
    trace.complete("parse", started, page_count=len(pages))
    started = trace.start("chunking", chunk_size=chunk_size, chunk_overlap=chunk_overlap)
    chunks = chunk_pages(pages, chunk_size, chunk_overlap)
    trace.complete("chunking", started, chunk_count=len(chunks), token_count=sum(chunk.token_count for chunk in chunks))
    started = trace.start("storage", kind="pages_and_chunks")
    try:
        with database_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO documents (id, filename, page_count, chunk_size, chunk_overlap) VALUES (%s, %s, %s, %s, %s)",
                    (document_id, filename, len(pages), chunk_size, chunk_overlap),
                )
                cursor.execute("INSERT INTO document_files (document_id, pdf) VALUES (%s, %s)", (document_id, data))
                cursor.executemany(
                    "INSERT INTO document_pages (document_id, page_number, content) VALUES (%s, %s, %s)",
                    [(document_id, page.number, page.content) for page in pages],
                )
                cursor.executemany(
                    "INSERT INTO chunks (id, document_id, chunk_index, page_number, content, token_count) VALUES (%s, %s, %s, %s, %s, %s)",
                    [(uuid4(), document_id, chunk.index, chunk.page_number, chunk.content, chunk.token_count) for chunk in chunks],
                )
    except psycopg.Error as exc:
        raise HTTPException(status_code=503, detail="Could not store document") from exc
    trace.complete("storage", started, kind="pages_and_chunks", chunk_count=len(chunks))
    return {"id": str(document_id), "filename": filename, "page_count": len(pages), "chunk_count": len(chunks), "chunk_size": chunk_size, "chunk_overlap": chunk_overlap}


@app.get("/documents/{document_id}")
def get_document(document_id: UUID) -> dict:
    try:
        with database_connection() as connection:
            row = connection.execute(
                """SELECT filename, page_count, chunk_size, chunk_overlap, created_at,
                          EXISTS (SELECT 1 FROM document_files f WHERE f.document_id = documents.id)
                   FROM documents WHERE id = %s""",
                (document_id,),
            ).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="Document not found")
            pages = connection.execute(
                "SELECT page_number, content FROM document_pages WHERE document_id = %s ORDER BY page_number",
                (document_id,),
            ).fetchall()
        return {"id": str(document_id), "filename": row[0], "page_count": row[1], "chunk_size": row[2], "chunk_overlap": row[3],
                "created_at": row[4], "has_original_pdf": row[5], "pages": [{"number": p[0], "content": p[1]} for p in pages]}
    except psycopg.Error as exc:
        raise HTTPException(status_code=503, detail="Could not load document") from exc


@app.get("/documents/{document_id}/pdf")
def get_original_pdf(document_id: UUID):
    """Open the original PDF; the browser's #page=N fragment selects its page."""
    try:
        with database_connection() as connection:
            row = connection.execute("SELECT pdf FROM document_files WHERE document_id = %s", (document_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Original PDF is unavailable. Older uploads retain extracted text only.")
        return Response(bytes(row[0]), media_type="application/pdf", headers={
            "Content-Disposition": f'inline; filename="document-{document_id}.pdf"',
            "X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store",
        })
    except psycopg.Error as exc:
        raise HTTPException(status_code=503, detail="Could not load original PDF") from exc


@app.get("/documents/{document_id}/chunks")
def get_chunks(document_id: UUID) -> dict:
    try:
        with database_connection() as connection:
            exists = connection.execute("SELECT 1 FROM documents WHERE id = %s", (document_id,)).fetchone()
            if not exists:
                raise HTTPException(status_code=404, detail="Document not found")
            rows = connection.execute(
                """SELECT id, chunk_index, page_number, content, token_count,
                          embedding_status, embedding_model, embedding_dimensions,
                          (embedding::real[])[1:%s], embedded_at
                   FROM chunks WHERE document_id = %s ORDER BY chunk_index""",
                (PREVIEW_SIZE, document_id),
            ).fetchall()
        return {"document_id": str(document_id), "chunks": [{
            "id": str(r[0]), "index": r[1], "page_number": r[2],
            "content": r[3], "token_count": r[4], "embedding_status": r[5],
            "embedding_model": r[6], "embedding_dimensions": r[7],
            "embedding_preview": r[8], "embedded_at": r[9],
        } for r in rows]}
    except psycopg.Error as exc:
        raise HTTPException(status_code=503, detail="Could not load chunks") from exc


@app.get("/documents/{document_id}/embeddings")
def get_embeddings(document_id: UUID) -> dict:
    try:
        with database_connection() as connection:
            return embedding_summary(connection, document_id)
    except psycopg.Error as exc:
        raise HTTPException(status_code=503, detail="Could not load embedding progress") from exc


@app.post("/documents/{document_id}/embeddings")
def embed_document(document_id: UUID) -> dict:
    try:
        with database_connection() as connection:
            return process_embeddings(connection, document_id)
    except psycopg.Error as exc:
        raise HTTPException(status_code=503, detail="Database is unavailable. Saved batches are retained; retry when it recovers.") from exc


@app.post("/documents/{document_id}/retrieve")
def retrieve_document(document_id: UUID, request: RetrievalRequest) -> dict:
    """Return ranked source chunks and real scores; no answer is generated."""
    try:
        with database_connection() as connection:
            return retrieve(connection, document_id, request)
    except psycopg.Error as exc:
        raise HTTPException(status_code=503, detail="Could not search document. Check the database and retry.") from exc


@app.post("/documents/{document_id}/answer")
def answer_document(document_id: UUID, request: RetrievalRequest) -> dict:
    """Retrieve context, then generate an answer with validated source references."""
    try:
        with database_connection() as connection:
            context = retrieve(connection, document_id, request)
    except psycopg.Error as exc:
        raise HTTPException(status_code=503, detail="Could not search document. Check the database and retry.") from exc
    # Release the database connection before waiting for the generation provider.
    return generate_answer(context)


@app.post("/documents/{document_id}/embeddings/stream")
def stream_embeddings(document_id: UUID):
    def run(trace):
        with database_connection() as connection:
            return process_embeddings(connection, document_id, trace)
    return trace_response(run, document_id)


def stream_question(document_id, request, answer):
    def run(trace):
        trace.emit("question", question=request.question)
        with database_connection() as connection:
            context = retrieve(connection, document_id, request, trace)
        return generate_answer(context, trace) if answer else context
    return trace_response(run, document_id)


@app.post("/documents/{document_id}/retrieve/stream")
def stream_retrieval(document_id: UUID, request: RetrievalRequest):
    return stream_question(document_id, request, False)


@app.post("/documents/{document_id}/answer/stream")
def stream_answer(document_id: UUID, request: RetrievalRequest):
    return stream_question(document_id, request, True)
