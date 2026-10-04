-- Least-privilege Entra ID roles for PostgreSQL. No database passwords exist.
-- Replace the names if you changed `prefix` in infra/main.bicep.

-- 1) As the Entra admin, connected to database "postgres":
SELECT * FROM pgaadauth_create_principal('ptorag-id', false, false);            -- API: read-only
SELECT * FROM pgaadauth_create_principal('ptorag-pipelines-id', false, false);  -- sync + ingestion jobs

-- 2) Connected to database "hr_rag", after schema.sql:
REVOKE ALL ON DATABASE hr_rag FROM PUBLIC;
GRANT CONNECT ON DATABASE hr_rag TO "ptorag-id", "ptorag-pipelines-id";
GRANT TEMPORARY ON DATABASE hr_rag TO "ptorag-pipelines-id";   -- sync stages rows in a temp table

GRANT USAGE ON SCHEMA hr, rag TO "ptorag-id";
GRANT SELECT ON hr.employees, rag.documents, rag.chunks TO "ptorag-id";

GRANT USAGE ON SCHEMA hr, rag TO "ptorag-pipelines-id";
GRANT SELECT, INSERT, UPDATE, DELETE ON hr.employees, rag.documents, rag.chunks TO "ptorag-pipelines-id";
GRANT SELECT, INSERT ON hr.sync_runs TO "ptorag-pipelines-id";
