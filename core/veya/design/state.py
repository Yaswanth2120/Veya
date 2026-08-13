from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..ipc.errors import ErrorCode, ProtocolError


@dataclass
class ArchitectureNode:
    id: str
    label: str
    kind: str = "service"
    # Canvas layout coordinates — cosmetic only, never used for graph
    # semantics (edges reference `id`, not position).
    x: float = 0.0
    y: float = 0.0


@dataclass
class ArchitectureEdge:
    source: str
    target: str
    label: str = ""


@dataclass
class ArchitectureState:
    version: int = 1
    title: str = "System Design"
    nodes: list[ArchitectureNode] = field(default_factory=list)
    edges: list[ArchitectureEdge] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    requirements: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    trade_offs: list[str] = field(default_factory=list)
    action_items: list[str] = field(default_factory=list)


class ArchitectureStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def get(self, session_id: str) -> ArchitectureState:
        path = self._path(session_id)
        if not path.exists(): return ArchitectureState()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            return ArchitectureState(
                version=raw.get("version", 1), title=raw.get("title", "System Design"),
                nodes=[ArchitectureNode(**x) for x in raw.get("nodes", [])], edges=[ArchitectureEdge(**x) for x in raw.get("edges", [])],
                decisions=list(raw.get("decisions", [])), assumptions=list(raw.get("assumptions", [])),
                requirements=list(raw.get("requirements", [])), risks=list(raw.get("risks", [])),
                trade_offs=list(raw.get("trade_offs", [])), action_items=list(raw.get("action_items", [])),
            )
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise ProtocolError(ErrorCode.INTERNAL_ERROR, "The local architecture state could not be read.") from exc

    def replace(self, session_id: str, state: ArchitectureState, base_version: int | None) -> ArchitectureState:
        current = self.get(session_id)
        if base_version is not None and current.version != base_version:
            raise ProtocolError(ErrorCode.INVALID_PARAMS, "Architecture state changed; reload before saving.")
        ids = [node.id for node in state.nodes]
        if len(ids) != len(set(ids)) or any(not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", item) for item in ids):
            raise ProtocolError(ErrorCode.INVALID_PARAMS, "Architecture node IDs must be unique safe identifiers.")
        if any(edge.source not in ids or edge.target not in ids for edge in state.edges):
            raise ProtocolError(ErrorCode.INVALID_PARAMS, "Architecture edges must reference existing nodes.")
        state.version = current.version + 1
        path = self._path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(asdict(state), separators=(",", ":")), encoding="utf-8")
        temporary.replace(path)
        return state

    def delete_session(self, session_id: str) -> None:
        path = self._path(session_id)
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            raise ProtocolError(ErrorCode.INTERNAL_ERROR, "The local architecture state could not be deleted.") from exc

    def _path(self, session_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9-]{1,64}", session_id):
            raise ProtocolError(ErrorCode.INVALID_PARAMS, "Invalid architecture session id.")
        return self.root / f"{session_id}.json"


def mermaid(state: ArchitectureState) -> str:
    lines = ["flowchart LR"]
    for node in state.nodes: lines.append(f'    {node.id}["{node.label.replace(chr(34), chr(39))}"]')
    for edge in state.edges: lines.append(f"    {edge.source} -->|{edge.label.replace('|', '/')}| {edge.target}" if edge.label else f"    {edge.source} --> {edge.target}")
    return "\n".join(lines)
