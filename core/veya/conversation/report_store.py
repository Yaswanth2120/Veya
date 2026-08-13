"""Durable, per-session storage for the latest `SessionReport` a
`session.analyze` call produced. Without this, `session.report.get` only
worked while the same worker *process* stayed alive — a worker restart
(crash, update, or simply relaunching the app) silently lost every
report, which does not meet "durable RPC" behavior. Same on-disk pattern
as `design/state.py`'s `ArchitectureStore`/`coding/workspace.py`'s
`CodeWorkspaceStore`: one JSON file per session, atomic temp-file-then-
rename writes. Never logs report content.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from ..ipc.errors import ErrorCode, ProtocolError
from .report import SessionReport


class ReportStore:
    def __init__(self, root: Path) -> None:
        self._root = root

    def save(self, report: SessionReport) -> None:
        path = self._path(report.session_id)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(asdict(report), separators=(",", ":")), encoding="utf-8")
            temporary.replace(path)
        except OSError as exc:
            raise ProtocolError(ErrorCode.INTERNAL_ERROR, "The local session report could not be saved.") from exc

    def get(self, session_id: str) -> Optional[SessionReport]:
        path = self._path(session_id)
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            return SessionReport(**raw)
        except (OSError, ValueError, TypeError) as exc:
            raise ProtocolError(ErrorCode.INTERNAL_ERROR, "The local session report could not be read.") from exc

    def delete(self, session_id: str) -> None:
        path = self._path(session_id)
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            raise ProtocolError(ErrorCode.INTERNAL_ERROR, "The local session report could not be deleted.") from exc

    def _path(self, session_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9-]{1,64}", session_id):
            raise ProtocolError(ErrorCode.INVALID_PARAMS, "Invalid report session id.")
        return self._root / f"{session_id}.json"
