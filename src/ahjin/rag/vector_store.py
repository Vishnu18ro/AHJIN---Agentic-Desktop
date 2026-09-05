"""Persistent SQLite Vector Store for AHJIN RAG subsystem."""

import json
import math
import sqlite3
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from ahjin.rag.chunker import ChunkDescriptor


class ScoredChunk(BaseModel):
    """Retrieved chunk with similarity score."""

    chunk: ChunkDescriptor
    score: float


class SQLiteVectorStore:
    """SQLite-backed persistent vector store surviving app restarts."""

    def __init__(self, db_path: str | Path = "ahjin_rag.db") -> None:
        self.db_path = Path(db_path) if isinstance(db_path, str) else db_path
        self._memory_conn: sqlite3.Connection | None = None
        if str(self.db_path) == ":memory:":
            self._memory_conn = sqlite3.connect(":memory:")
            self._memory_conn.row_factory = sqlite3.Row
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        if self._memory_conn is not None:
            return self._memory_conn
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        """Create database tables if they do not exist."""
        with self._get_connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS documents (
                    document_id TEXT PRIMARY KEY,
                    document_name TEXT NOT NULL,
                    chunk_count INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS chunks (
                    chunk_id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL,
                    document_name TEXT NOT NULL,
                    chunk_index INTEGER NOT NULL,
                    page_numbers_json TEXT NOT NULL,
                    content TEXT NOT NULL,
                    embedding_json TEXT NOT NULL,
                    dimension INTEGER NOT NULL,
                    FOREIGN KEY (document_id) REFERENCES documents(document_id)
                );
                """
            )
            conn.commit()

    def add_chunks(
        self, chunks: list[ChunkDescriptor], embeddings: list[list[float]]
    ) -> None:
        """Persist document metadata, chunks, and embeddings."""
        if len(chunks) != len(embeddings):
            raise ValueError("Number of chunks must match number of embeddings.")

        if not chunks:
            return

        with self._get_connection() as conn:
            doc_counts: dict[str, tuple[str, int]] = {}
            for c in chunks:
                doc_id = c.document_id
                doc_name = c.document_name
                curr_count = doc_counts.get(doc_id, (doc_name, 0))[1]
                doc_counts[doc_id] = (doc_name, curr_count + 1)

            for doc_id, (doc_name, count) in doc_counts.items():
                conn.execute(
                    """
                    INSERT INTO documents (document_id, document_name, chunk_count)
                    VALUES (?, ?, ?)
                    ON CONFLICT(document_id) DO UPDATE SET
                        chunk_count = chunk_count + excluded.chunk_count;
                    """,
                    (doc_id, doc_name, count),
                )

            for chunk, emb in zip(chunks, embeddings, strict=True):
                dim = len(emb)
                conn.execute(
                    """
                    INSERT INTO chunks (
                        chunk_id, document_id, document_name, chunk_index,
                        page_numbers_json, content, embedding_json, dimension
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(chunk_id) DO UPDATE SET
                        content = excluded.content,
                        embedding_json = excluded.embedding_json;
                    """,
                    (
                        chunk.chunk_id,
                        chunk.document_id,
                        chunk.document_name,
                        chunk.chunk_index,
                        json.dumps(chunk.page_numbers),
                        chunk.content,
                        json.dumps(emb),
                        dim,
                    ),
                )
            conn.commit()

    def search(self, query_embedding: list[float], top_k: int = 5) -> list[ScoredChunk]:
        """Perform cosine similarity search against stored embeddings."""
        with self._get_connection() as conn:
            query_sql = (
                "SELECT chunk_id, document_id, document_name, chunk_index, "
                "page_numbers_json, content, embedding_json FROM chunks"
            )
            rows = conn.execute(query_sql).fetchall()

        if not rows:
            return []

        scored: list[ScoredChunk] = []
        q_norm = math.sqrt(sum(x * x for x in query_embedding))
        if q_norm == 0:
            return []

        for row in rows:
            emb_raw: Any = json.loads(row["embedding_json"])
            emb: list[float] = [float(x) for x in emb_raw] if isinstance(emb_raw, list) else []  # pyright: ignore[reportUnknownVariableType,reportUnknownArgumentType]

            dot = sum(q * v for q, v in zip(query_embedding, emb, strict=False))
            v_norm = math.sqrt(sum(v * v for v in emb))

            sim = dot / (q_norm * v_norm) if (q_norm * v_norm) > 0 else 0.0

            pages_raw: Any = json.loads(row["page_numbers_json"])
            pages: list[int] = [int(p) for p in pages_raw] if isinstance(pages_raw, list) else []  # pyright: ignore[reportUnknownVariableType,reportUnknownArgumentType]

            chunk = ChunkDescriptor(
                chunk_id=str(row["chunk_id"]),
                document_id=str(row["document_id"]),
                document_name=str(row["document_name"]),
                chunk_index=int(row["chunk_index"]),
                page_numbers=pages,
                content=str(row["content"]),
            )
            scored.append(ScoredChunk(chunk=chunk, score=sim))

        scored.sort(key=lambda x: x.score, reverse=True)
        return scored[:top_k]

    def clear(self) -> None:
        """Clear all stored documents and chunks."""
        with self._get_connection() as conn:
            conn.execute("DELETE FROM chunks;")
            conn.execute("DELETE FROM documents;")
            conn.commit()
