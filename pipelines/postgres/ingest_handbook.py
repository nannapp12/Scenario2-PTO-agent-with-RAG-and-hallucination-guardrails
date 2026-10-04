"""Document ingestion pipeline: handbook -> chunk -> embed -> load into pgvector.

Sources: a local file (--path) or every file under a prefix of the ADLS "landing"
container (--blob-prefix, managed identity over HTTPS). Runs as the manual
Container Apps Job `<prefix>-handbook-ingest`; start it after uploading a new handbook.

Idempotent: a document whose text hash hasn't changed is skipped. A changed document's
chunks are replaced in one transaction, so readers never see a half-loaded handbook.
Supports .md, .txt and .pdf.
"""
import argparse
import hashlib
import io
import logging
import os
import re
import sys
from pathlib import Path

if len(_parents := Path(__file__).resolve().parents) > 2:
    sys.path.insert(0, str(_parents[1]))  # repo root, for `common` when run locally (the image sets PYTHONPATH)

from chunking import chunk_text, clean_pdf_pages, normalize  # noqa: E402
from common import db  # noqa: E402
from common.embeddings import Embedder, openai_client  # noqa: E402

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("handbook-ingest")
SUPPORTED = (".md", ".txt", ".pdf")


def extract_text(name: str, data: bytes) -> str:
    if name.lower().endswith(".pdf"):
        from pypdf import PdfReader
        return clean_pdf_pages([page.extract_text() or "" for page in PdfReader(io.BytesIO(data)).pages])
    return data.decode("utf-8-sig")


def doc_id_for(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", Path(name).stem.lower()).strip("-")


def title_for(text: str, name: str) -> str:
    m = re.search(r"^#\s+(.+)$", text, re.MULTILINE)
    return m.group(1).strip() if m else re.sub(r"[-_]+", " ", Path(name).stem)


def ingest(conn, embedder: Embedder, name: str, data: bytes, source_uri: str, force: bool = False) -> None:
    text = normalize(extract_text(name, data))
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    doc_id, title = doc_id_for(name), title_for(text, name)

    row = conn.execute("SELECT sha256 FROM rag.documents WHERE doc_id = %s", (doc_id,)).fetchone()
    if row and row[0] == sha and not force:
        log.info("%s unchanged (sha256=%s); skipping", doc_id, sha)
        return

    chunks = chunk_text(text, outline_headings=not name.lower().endswith(".md"))
    if not chunks:
        raise ValueError(f"No text extracted from {name}")
    vectors = embedder.embed([f"{title} | {c.section}\n\n{c.content}" for c in chunks])

    with conn.transaction():
        conn.execute(
            "INSERT INTO rag.documents (doc_id, title, source_uri, sha256, chunk_count) "
            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (doc_id) DO UPDATE SET title = EXCLUDED.title, "
            "source_uri = EXCLUDED.source_uri, sha256 = EXCLUDED.sha256, "
            "chunk_count = EXCLUDED.chunk_count, ingested_at = now()",
            (doc_id, title, source_uri, sha, len(chunks)),
        )
        conn.execute("DELETE FROM rag.chunks WHERE doc_id = %s", (doc_id,))
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO rag.chunks (doc_id, chunk_index, section, content, embedding) "
                "VALUES (%s, %s, %s, %s, %s::vector)",
                [(doc_id, i, c.section, c.content, db.vector_literal(v))
                 for i, (c, v) in enumerate(zip(chunks, vectors))],
            )
    log.info("Ingested %s: %d chunks, sha256=%s", doc_id, len(chunks), sha)
    log.info("If the PTO section changed, review api/app/pto_policy.json and set handbook_sha256 to %s", sha)


def iter_sources(args):
    if args.path:
        p = Path(args.path)
        yield p.name, p.read_bytes(), p.resolve().as_uri()
        return
    from azure.identity import DefaultAzureCredential
    from azure.storage.blob import BlobServiceClient

    account_url = f"https://{os.environ['STORAGE_ACCOUNT']}.blob.core.windows.net"
    container = BlobServiceClient(account_url, credential=DefaultAzureCredential()) \
        .get_container_client(os.environ.get("LANDING_CONTAINER", "landing"))
    for b in container.list_blobs(name_starts_with=args.blob_prefix):
        if b.name.lower().endswith(SUPPORTED):
            yield b.name, container.download_blob(b.name).readall(), f"{account_url}/{container.container_name}/{b.name}"


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--path", help="local handbook file")
    src.add_argument("--blob-prefix", help="prefix in the landing container, e.g. handbook/")
    p.add_argument("--force", action="store_true", help="re-embed even if unchanged")
    args = p.parse_args(argv)

    embedder = Embedder(
        openai_client(os.environ["AZURE_OPENAI_ENDPOINT"], os.environ.get("AZURE_OPENAI_API_VERSION", "2024-10-21")),
        os.environ.get("EMBEDDING_DEPLOYMENT", "text-embedding-3-small"),
        int(os.environ.get("EMBEDDING_DIMENSIONS", "1536")),
    )
    info = db.conninfo(os.environ["PG_HOST"], os.environ.get("PG_DATABASE", "hr_rag"), os.environ["PG_USER"],
                       os.environ.get("PG_SSLMODE", "verify-full"),
                       os.environ.get("PG_SSLROOTCERT", "/etc/ssl/certs/ca-certificates.crt"))
    count = 0
    with db.connect(info, os.environ.get("PG_PASSWORD", ""), autocommit=True) as conn:
        for name, data, uri in iter_sources(args):
            ingest(conn, embedder, name, data, uri, args.force)
            count += 1
    if count == 0:
        log.warning("No handbook files found.")


if __name__ == "__main__":
    main()
