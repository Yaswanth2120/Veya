"""SQLite-backed store for derived knowledge-index data: document
ingestion status and chunk metadata + embeddings. Never stores session,
transcript, question, or answer data — Swift/GRDB remains the sole
persistence authority for all of that (see `docs/KNOWLEDGE_RETRIEVAL.md`).

Deliberately synchronous (plain `sqlite3`, no async driver) — callers run
its methods via `asyncio.to_thread` at call sites that need to stay off
the event loop (`ingestion.py`/`retrieval.py`). This keeps the store
itself trivially testable without an event loop.
"""

from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path
from typing import List, Optional, Tuple

from .models import DocumentChunk, IngestionStatus


def cosine_similarity(a: List[float], b: List[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


class VectorStore:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(db_path), check_same_thread=False)
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._create_schema()

    def _create_schema(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS documents (
                document_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                file_name TEXT NOT NULL,
                status TEXT NOT NULL,
                error_reason TEXT
            );
            CREATE TABLE IF NOT EXISTS chunks (
                chunk_id TEXT PRIMARY KEY,
                document_id TEXT NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
                session_id TEXT NOT NULL,
                file_name TEXT NOT NULL,
                chunk_index INTEGER NOT NULL,
                text TEXT NOT NULL,
                excerpt TEXT NOT NULL,
                char_start INTEGER NOT NULL,
                char_end INTEGER NOT NULL,
                embedding TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_chunks_session ON chunks(session_id);
            """
        )
        self._connection.commit()

    def upsert_document(
        self, document_id: str, session_id: str, file_name: str, status: IngestionStatus, error_reason: Optional[str] = None
    ) -> None:
        self._connection.execute(
            """
            INSERT INTO documents (document_id, session_id, file_name, status, error_reason)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(document_id) DO UPDATE SET
                session_id = excluded.session_id,
                file_name = excluded.file_name,
                status = excluded.status,
                error_reason = excluded.error_reason
            """,
            (document_id, session_id, file_name, status.value, error_reason),
        )
        self._connection.commit()

    def set_document_status(self, document_id: str, status: IngestionStatus, error_reason: Optional[str] = None) -> None:
        self._connection.execute(
            "UPDATE documents SET status = ?, error_reason = ? WHERE document_id = ?",
            (status.value, error_reason, document_id),
        )
        self._connection.commit()

    def get_status(self, document_id: str) -> Optional[IngestionStatus]:
        row = self._connection.execute(
            "SELECT status FROM documents WHERE document_id = ?", (document_id,)
        ).fetchone()
        return IngestionStatus(row[0]) if row else None

    def replace_chunks(
        self,
        document_id: str,
        session_id: str,
        file_name: str,
        chunks: List[DocumentChunk],
        embeddings: List[List[float]],
    ) -> None:
        """Deletes any existing chunks for `document_id` and inserts
        `chunks`/`embeddings` fresh — re-ingesting a document is a clean
        replace, never an accumulating duplicate set (chunk IDs are
        stable/deterministic per `chunking.py`, but this also protects
        against a chunking-config change altering chunk counts)."""
        if len(chunks) != len(embeddings):
            raise ValueError("chunks and embeddings must have the same length.")

        self._connection.execute("DELETE FROM chunks WHERE document_id = ?", (document_id,))
        self._connection.executemany(
            """
            INSERT INTO chunks
                (chunk_id, document_id, session_id, file_name, chunk_index, text, excerpt, char_start, char_end, embedding)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    chunk.chunk_id,
                    chunk.document_id,
                    chunk.session_id,
                    chunk.file_name,
                    chunk.chunk_index,
                    chunk.text,
                    chunk.excerpt,
                    chunk.char_start,
                    chunk.char_end,
                    json.dumps(embedding),
                )
                for chunk, embedding in zip(chunks, embeddings)
            ],
        )
        self._connection.commit()

    def remove_document(self, document_id: str) -> None:
        """Removes the document row and (via `ON DELETE CASCADE`) every
        chunk derived from it."""
        self._connection.execute("DELETE FROM documents WHERE document_id = ?", (document_id,))
        self._connection.commit()

    def remove_session(self, session_id: str) -> None:
        """Removes every document (and, via `ON DELETE CASCADE`, every
        chunk) ingested for `session_id` — the knowledge-index half of
        deleting a session's data entirely."""
        self._connection.execute("DELETE FROM documents WHERE session_id = ?", (session_id,))
        self._connection.commit()

    def search(self, session_id: str, query_embedding: List[float], top_k: int) -> List[Tuple[DocumentChunk, float]]:
        """Session-scoped only — never retrieves chunks belonging to a
        different session, and only from documents whose status is
        `ready` (an in-progress/failed document's stale or partial chunks
        are never used for retrieval)."""
        rows = self._connection.execute(
            """
            SELECT c.chunk_id, c.document_id, c.session_id, c.file_name, c.chunk_index,
                   c.text, c.excerpt, c.char_start, c.char_end, c.embedding
            FROM chunks c
            JOIN documents d ON d.document_id = c.document_id
            WHERE c.session_id = ? AND d.status = ?
            """,
            (session_id, IngestionStatus.READY.value),
        ).fetchall()

        scored: List[Tuple[DocumentChunk, float]] = []
        for row in rows:
            chunk = DocumentChunk(
                chunk_id=row[0],
                document_id=row[1],
                session_id=row[2],
                file_name=row[3],
                chunk_index=row[4],
                text=row[5],
                excerpt=row[6],
                char_start=row[7],
                char_end=row[8],
            )
            embedding = json.loads(row[9])
            score = cosine_similarity(query_embedding, embedding)
            scored.append((chunk, score))

        scored.sort(key=lambda item: item[1], reverse=True)
        return scored[:top_k]

    def close(self) -> None:
        self._connection.close()
