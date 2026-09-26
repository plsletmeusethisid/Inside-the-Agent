"""Question embedding and exact, document-scoped pgvector cosine retrieval."""

import json
from time import perf_counter
from uuid import UUID

import psycopg
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.embeddings import DIMENSIONS, MODEL, PREVIEW_SIZE, EmbeddingError, create_embedding_client, embed_texts


class RetrievalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=2000)
    k: int = Field(default=3, ge=1, le=20, strict=True)
    min_similarity: float = Field(default=-1.0, ge=-1.0, le=1.0, allow_inf_nan=False)

    @field_validator("question")
    @classmethod
    def nonblank_question(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Enter a nonblank question")
        return value


def retrieve(connection: psycopg.Connection, document_id: UUID, request: RetrievalRequest) -> dict:
    # Validate completeness before spending a provider request. Uploads are immutable.
    row = connection.execute("""
        SELECT count(c.id), count(c.embedding),
               count(*) FILTER (WHERE c.embedding_model <> %s OR c.embedding_dimensions <> %s)
        FROM documents d LEFT JOIN chunks c ON c.document_id = d.id
        WHERE d.id = %s GROUP BY d.id
    """, (MODEL, DIMENSIONS, document_id)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Document not found")
    total, embedded, incompatible = row
    if not total or embedded != total:
        raise HTTPException(status_code=409, detail="Embed and store all document chunks before searching.")
    if incompatible:
        raise HTTPException(status_code=409, detail="Stored embedding model is incompatible. An explicit migration is required.")
    connection.commit()  # Do not hold an idle transaction during the provider call.

    started = perf_counter()
    try:
        with create_embedding_client() as client:
            vector = embed_texts(client, [request.question])[0]
    except EmbeddingError as exc:
        raise HTTPException(status_code=502, detail=f"Question embedding failed: {exc}") from None
    embedding_ms = (perf_counter() - started) * 1000

    started = perf_counter()
    # <=> is pgvector cosine distance. No approximate index or cross-document search.
    rows = connection.execute("""
        SELECT id, chunk_index, page_number, content, token_count,
               1 - (embedding <=> %s::vector) AS similarity
        FROM chunks
        WHERE document_id = %s AND embedding IS NOT NULL
          AND embedding_model = %s AND embedding_dimensions = %s
          AND 1 - (embedding <=> %s::vector) >= %s
        ORDER BY embedding <=> %s::vector, chunk_index, id
        LIMIT %s
    """, (json.dumps(vector), document_id, MODEL, DIMENSIONS,
          json.dumps(vector), request.min_similarity, json.dumps(vector), request.k)).fetchall()
    search_ms = (perf_counter() - started) * 1000
    return {
        "document_id": str(document_id), "question": request.question,
        "k": request.k, "min_similarity": request.min_similarity,
        "searched_chunks": embedded, "model": MODEL, "dimensions": DIMENSIONS,
        "query_embedding_preview": vector[:PREVIEW_SIZE],
        "score_definition": "1 - cosine distance (pgvector <=>); higher is closer",
        "timings_ms": {"query_embedding": round(embedding_ms, 2), "vector_search": round(search_ms, 2)},
        "results": [{
            "rank": rank, "chunk_id": str(row[0]), "chunk_index": row[1],
            "page_number": row[2], "content": row[3], "token_count": row[4],
            "similarity": row[5],
        } for rank, row in enumerate(rows, 1)],
    }
