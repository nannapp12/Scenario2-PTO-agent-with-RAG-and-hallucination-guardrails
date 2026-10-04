"""Retrieval over the Employee Handbook chunks in pgvector (cosine distance, HNSW index)."""
from psycopg_pool import ConnectionPool

from common.db import vector_literal
from common.embeddings import Embedder

from .schemas import Citation


class HandbookRetriever:
    def __init__(self, pool: ConnectionPool, embedder: Embedder, top_k: int, min_score: float):
        self.pool, self.embedder, self.top_k, self.min_score = pool, embedder, top_k, min_score

    def search(self, query: str, doc_id: str) -> list[Citation]:
        vec = vector_literal(list(self.embedder.embed_query(query)))
        with self.pool.connection() as conn:
            rows = conn.execute(
                "SELECT section, content, 1 - (embedding <=> %s::vector) AS score "
                "FROM rag.chunks WHERE doc_id = %s "
                "ORDER BY embedding <=> %s::vector LIMIT %s",
                (vec, doc_id, vec, self.top_k),
            ).fetchall()
        return [Citation(section=s or "", excerpt=c, score=round(float(score), 3))
                for s, c, score in rows if score >= self.min_score]
