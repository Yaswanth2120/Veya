"""Durable, user-approved memory. SQLite under the managed Veya
application-support root — never remote storage. A candidate is only ever
created explicitly (via `session.analyze`'s proposed `memory_candidates`,
surfaced for review) and is retrievable in future sessions only once a
user explicitly approves it; rejecting one deletes it outright. Never
logs memory text or arbitrary error content — only typed outcomes.
"""
from __future__ import annotations

import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from ..ipc.errors import ErrorCode, ProtocolError

STATUS_PROPOSED = "PROPOSED"
STATUS_APPROVED = "APPROVED"


@dataclass(frozen=True)
class MemoryRecord:
    id: str
    session_id: str
    text: str
    status: str
    created_at: float
    updated_at: float


class MemoryStore:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(db_path), check_same_thread=False)
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS memory (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                text TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        self._connection.commit()

    def create_candidate(self, session_id: str, text: str) -> MemoryRecord:
        now = time.time()
        record = MemoryRecord(id=str(uuid.uuid4()), session_id=session_id, text=text, status=STATUS_PROPOSED, created_at=now, updated_at=now)
        self._connection.execute(
            "INSERT INTO memory (id, session_id, text, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (record.id, record.session_id, record.text, record.status, record.created_at, record.updated_at),
        )
        self._connection.commit()
        return record

    def list(self, status: Optional[str] = None) -> List[MemoryRecord]:
        if status is not None:
            rows = self._connection.execute(
                "SELECT id, session_id, text, status, created_at, updated_at FROM memory WHERE status = ? ORDER BY created_at DESC", (status,)
            ).fetchall()
        else:
            rows = self._connection.execute(
                "SELECT id, session_id, text, status, created_at, updated_at FROM memory ORDER BY created_at DESC"
            ).fetchall()
        return [MemoryRecord(*row) for row in rows]

    def approved_texts(self, limit: int = 20) -> List[str]:
        rows = self._connection.execute(
            "SELECT text FROM memory WHERE status = ? ORDER BY updated_at DESC LIMIT ?", (STATUS_APPROVED, limit)
        ).fetchall()
        return [row[0] for row in rows]

    def approve(self, memory_id: str) -> MemoryRecord:
        return self._set_status(memory_id, STATUS_APPROVED)

    def reject(self, memory_id: str) -> None:
        # A rejected candidate must never be retrievable — delete outright
        # rather than keep a "REJECTED" row around to leak later.
        cursor = self._connection.execute("DELETE FROM memory WHERE id = ? AND status = ?", (memory_id, STATUS_PROPOSED))
        self._connection.commit()
        if cursor.rowcount == 0:
            raise ProtocolError(ErrorCode.SESSION_NOT_FOUND, "No proposed memory with that id exists.")

    def update(self, memory_id: str, text: str) -> MemoryRecord:
        cursor = self._connection.execute(
            "UPDATE memory SET text = ?, updated_at = ? WHERE id = ?", (text, time.time(), memory_id)
        )
        self._connection.commit()
        if cursor.rowcount == 0:
            raise ProtocolError(ErrorCode.SESSION_NOT_FOUND, "No memory with that id exists.")
        return self._get(memory_id)

    def delete(self, memory_id: str) -> None:
        cursor = self._connection.execute("DELETE FROM memory WHERE id = ?", (memory_id,))
        self._connection.commit()
        if cursor.rowcount == 0:
            raise ProtocolError(ErrorCode.SESSION_NOT_FOUND, "No memory with that id exists.")

    def _set_status(self, memory_id: str, status: str) -> MemoryRecord:
        cursor = self._connection.execute(
            "UPDATE memory SET status = ?, updated_at = ? WHERE id = ?", (status, time.time(), memory_id)
        )
        self._connection.commit()
        if cursor.rowcount == 0:
            raise ProtocolError(ErrorCode.SESSION_NOT_FOUND, "No memory with that id exists.")
        return self._get(memory_id)

    def _get(self, memory_id: str) -> MemoryRecord:
        row = self._connection.execute(
            "SELECT id, session_id, text, status, created_at, updated_at FROM memory WHERE id = ?", (memory_id,)
        ).fetchone()
        if row is None:
            raise ProtocolError(ErrorCode.SESSION_NOT_FOUND, "No memory with that id exists.")
        return MemoryRecord(*row)
