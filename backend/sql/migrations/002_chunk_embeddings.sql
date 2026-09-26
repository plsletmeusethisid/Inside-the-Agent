-- Additive migration: existing documents, pages, and chunks are preserved.
-- A model change requires a new migration and an explicit re-embedding policy.
ALTER TABLE documents
    ADD COLUMN embedding_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (embedding_status IN ('pending', 'processing', 'complete', 'failed')),
    ADD COLUMN embedding_error TEXT;

ALTER TABLE chunks
    ADD COLUMN embedding vector(1536),
    ADD COLUMN embedding_model TEXT NOT NULL DEFAULT 'text-embedding-3-small'
        CHECK (embedding_model = 'text-embedding-3-small'),
    ADD COLUMN embedding_dimensions INTEGER NOT NULL DEFAULT 1536
        CHECK (embedding_dimensions = 1536),
    ADD COLUMN embedding_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (embedding_status IN ('pending', 'processing', 'embedded', 'failed')),
    ADD COLUMN embedded_at TIMESTAMPTZ,
    ADD CONSTRAINT chunks_embedding_state CHECK (
        (embedding_status = 'embedded' AND embedding IS NOT NULL AND embedded_at IS NOT NULL)
        OR (embedding_status <> 'embedded' AND embedding IS NULL AND embedded_at IS NULL)
    );
