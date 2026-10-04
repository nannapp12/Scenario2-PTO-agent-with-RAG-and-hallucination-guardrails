-- Azure Database for PostgreSQL (database hr_rag): handbook RAG + employees synced from
-- Databricks on AWS. Run as the Entra admin after the server is created (see README).
-- At rest: server storage, backups and WAL are AES-256 encrypted (optional CMK).
-- PII columns (*_enc) are additionally AES-256-GCM encrypted by the application.

CREATE EXTENSION IF NOT EXISTS vector;     -- allow-listed by the azure.extensions server parameter

CREATE SCHEMA IF NOT EXISTS rag;
CREATE SCHEMA IF NOT EXISTS hr;

-- ---------- Handbook RAG ----------
CREATE TABLE IF NOT EXISTS rag.documents (
    doc_id       text PRIMARY KEY,
    title        text NOT NULL,
    source_uri   text NOT NULL,
    sha256       text NOT NULL,              -- of the normalized text; compared with pto_policy.json
    chunk_count  int  NOT NULL,
    ingested_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS rag.chunks (
    chunk_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    doc_id       text NOT NULL REFERENCES rag.documents (doc_id) ON DELETE CASCADE,
    chunk_index  int  NOT NULL,
    section      text NOT NULL,
    content      text NOT NULL,
    embedding    vector(1536) NOT NULL,      -- text-embedding-3-small; keep in sync with EMBEDDING_DIMENSIONS
    -- section: heading path, e.g. "V. BENEFITS AND LEAVES OF ABSENCE > B. Vacation Benefits > 2. Accrual"
    UNIQUE (doc_id, chunk_index)
);

CREATE INDEX IF NOT EXISTS chunks_embedding_hnsw
    ON rag.chunks USING hnsw (embedding vector_cosine_ops);

-- ---------- Employees (minimal columns needed for PTO) ----------
CREATE TABLE IF NOT EXISTS hr.employees (
    employee_id        text PRIMARY KEY,
    full_name_enc      bytea,                -- AES-256-GCM, AAD = "<employee_id>|full_name"
    email_enc          bytea,                -- AES-256-GCM, AAD = "<employee_id>|email"
    start_date         date NOT NULL,
    employment_type    text NOT NULL CHECK (employment_type IN ('FULL_TIME', 'PART_TIME', 'TEMPORARY')),
    source_updated_at  timestamptz,
    synced_at          timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS hr.sync_runs (
    run_id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    started_at     timestamptz NOT NULL,
    finished_at    timestamptz NOT NULL DEFAULT now(),
    source_rows    int NOT NULL,
    skipped_rows   int NOT NULL,
    upserted_rows  int NOT NULL,
    deleted_rows   int NOT NULL
);
