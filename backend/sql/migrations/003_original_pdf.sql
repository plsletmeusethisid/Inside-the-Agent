-- Keep small original uploads for page navigation; legacy documents stay valid.
-- Separate table avoids loading binary data during retrieval or document inspection.
CREATE TABLE document_files (
    document_id UUID PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
    pdf BYTEA NOT NULL CHECK (octet_length(pdf) <= 10485760)
);
