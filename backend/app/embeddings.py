"""OpenAI embeddings and resumable pgvector persistence, without a job queue."""

import json
import math
import os
from uuid import UUID

import httpx
import psycopg
from fastapi import HTTPException

MODEL = "text-embedding-3-small"
DIMENSIONS = 1536
BATCH_SIZE = 32  # At most 38,400 input tokens with the upload's 1,200-token limit.
PREVIEW_SIZE = 3


class EmbeddingError(Exception):
    """A safe, user-facing error; never includes provider bodies or credentials."""


def create_embedding_client() -> httpx.Client:
    key = os.getenv("OPENAI_API_KEY", "").strip()
    if not key:
        raise EmbeddingError("Set OPENAI_API_KEY on the backend, then retry embedding.")
    return httpx.Client(
        base_url="https://api.openai.com/v1/",
        headers={"Authorization": f"Bearer {key}"},
        timeout=httpx.Timeout(60.0, connect=10.0),
    )


def embed_texts(client: httpx.Client, texts: list[str]) -> list[list[float]]:
    """Shared model entry point for chunks and future question embeddings."""
    if not texts or len(texts) > BATCH_SIZE or any(not text.strip() for text in texts):
        raise EmbeddingError("Embedding requires 1–32 nonempty text passages.")
    try:
        response = client.post("embeddings", json={
            "model": MODEL, "dimensions": DIMENSIONS,
            "input": texts, "encoding_format": "float",
        })
    except httpx.TimeoutException:
        raise EmbeddingError("Embedding request timed out. Retry the request.") from None
    except httpx.RequestError:
        raise EmbeddingError("Could not reach OpenAI. Check the backend connection and retry.") from None
    if response.status_code in (401, 403):
        raise EmbeddingError("OpenAI rejected the credentials. Check the backend API key and model access.")
    if response.status_code == 429:
        raise EmbeddingError("OpenAI rate or quota limit reached. Check API billing/limits, then retry.")
    if not response.is_success:
        raise EmbeddingError("OpenAI could not generate embeddings. Retry the request later.")

    try:
        payload = response.json()
        if payload["model"] != MODEL or len(payload["data"]) != len(texts):
            raise ValueError("Unexpected model or response count")
        vectors: list[list[float] | None] = [None] * len(texts)
        for item in payload["data"]:
            index, vector = item["index"], item["embedding"]
            if type(index) is not int or not 0 <= index < len(texts) or vectors[index] is not None:
                raise ValueError("Invalid or duplicate response index")
            if not isinstance(vector, list) or len(vector) != DIMENSIONS:
                raise ValueError("Invalid dimensions")
            if any(type(value) not in (int, float) or not math.isfinite(value) or abs(value) > 3.4e38 for value in vector):
                raise ValueError("Invalid vector value")
            if not any(vector):
                raise ValueError("Zero vector cannot support cosine search")
            vectors[index] = vector
        if any(vector is None for vector in vectors):
            raise ValueError("Missing vector")
        return vectors
    except (ValueError, KeyError, TypeError, OverflowError):
        raise EmbeddingError("OpenAI returned invalid embedding data. No vectors from this batch were stored.") from None


def lock_key(document_id: UUID) -> int:
    # Signed bigint, stable between Python processes. Session locks release on disconnect.
    return (document_id.int >> 64) - (1 << 63)


def embedding_summary(connection: psycopg.Connection, document_id: UUID) -> dict:
    row = connection.execute("""
        SELECT d.embedding_status, d.embedding_error, count(c.id),
               count(c.embedding), count(*) FILTER (WHERE c.embedding_status = 'failed')
        FROM documents d LEFT JOIN chunks c ON c.document_id = d.id
        WHERE d.id = %s GROUP BY d.id
    """, (document_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Document not found")
    state, error, total, embedded, failed = row
    if state == "processing":
        acquired = connection.execute("SELECT pg_try_advisory_lock(%s)", (lock_key(document_id),)).fetchone()[0]
        if acquired:
            connection.execute("SELECT pg_advisory_unlock(%s)", (lock_key(document_id),))
            state = "interrupted"
            error = "Embedding was interrupted. Retry to resume remaining chunks."
    return {
        "document_id": str(document_id), "status": state,
        "model": MODEL, "dimensions": DIMENSIONS, "total_chunks": total,
        "embedded_chunks": embedded, "remaining_chunks": total - embedded,
        "failed_chunks": failed, "error": error,
    }


def process_embeddings(connection: psycopg.Connection, document_id: UUID) -> dict:
    """Run in a sync API worker. Commit each batch, retaining a session-level lock."""
    connection.autocommit = True
    if not connection.execute("SELECT 1 FROM documents WHERE id = %s", (document_id,)).fetchone():
        raise HTTPException(status_code=404, detail="Document not found")
    key = lock_key(document_id)
    if not connection.execute("SELECT pg_try_advisory_lock(%s)", (key,)).fetchone()[0]:
        raise HTTPException(status_code=409, detail="This document is already being embedded. Progress will update shortly.")
    try:
        mismatch = connection.execute("""
            SELECT 1 FROM chunks WHERE document_id = %s
            AND (embedding_model <> %s OR embedding_dimensions <> %s) LIMIT 1
        """, (document_id, MODEL, DIMENSIONS)).fetchone()
        if mismatch:
            raise HTTPException(status_code=409, detail="Stored embedding model is incompatible. An explicit migration is required.")
        pending = connection.execute("""
            SELECT id, content FROM chunks
            WHERE document_id = %s AND embedding IS NULL ORDER BY chunk_index
        """, (document_id,)).fetchall()
        if not pending:
            connection.execute("UPDATE documents SET embedding_status = 'complete', embedding_error = NULL WHERE id = %s", (document_id,))
            return embedding_summary(connection, document_id)

        with connection.transaction():
            connection.execute("UPDATE documents SET embedding_status = 'processing', embedding_error = NULL WHERE id = %s", (document_id,))
            connection.execute("UPDATE chunks SET embedding_status = 'pending' WHERE document_id = %s AND embedding IS NULL", (document_id,))
        try:
            with create_embedding_client() as client:
                for start in range(0, len(pending), BATCH_SIZE):
                    batch = pending[start:start + BATCH_SIZE]
                    ids = [row[0] for row in batch]
                    connection.execute("UPDATE chunks SET embedding_status = 'processing' WHERE id = ANY(%s)", (ids,))
                    vectors = embed_texts(client, [row[1] for row in batch])
                    # The cast persists all dimensions; previews are computed on read.
                    with connection.transaction():
                        with connection.cursor() as cursor:
                            cursor.executemany("""
                                UPDATE chunks SET embedding = %s::vector,
                                    embedding_status = 'embedded', embedded_at = now()
                                WHERE id = %s AND embedding IS NULL
                            """, [(json.dumps(vector, allow_nan=False), row[0]) for row, vector in zip(batch, vectors, strict=True)])
            connection.execute("UPDATE documents SET embedding_status = 'complete', embedding_error = NULL WHERE id = %s", (document_id,))
        except (EmbeddingError, psycopg.Error) as exc:
            message = str(exc) if isinstance(exc, EmbeddingError) else "Could not store embeddings. Retry to resume remaining chunks."
            # If the database is disconnected this may fail too. The next request
            # detects the released lock and offers recovery from the stored batches.
            with connection.transaction():
                connection.execute("UPDATE chunks SET embedding_status = 'failed' WHERE document_id = %s AND embedding_status = 'processing'", (document_id,))
                connection.execute("UPDATE documents SET embedding_status = 'failed', embedding_error = %s WHERE id = %s", (message, document_id))
            raise HTTPException(status_code=502 if isinstance(exc, EmbeddingError) else 503, detail=message) from None
        return embedding_summary(connection, document_id)
    finally:
        if not connection.closed:
            connection.execute("SELECT pg_advisory_unlock(%s)", (key,))
