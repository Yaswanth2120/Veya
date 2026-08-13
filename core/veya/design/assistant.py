"""Local-LLM system-design follow-ups. Evolves an existing
`ArchitectureState` in place — nodes/edges/decisions the user didn't ask
to change are preserved, never rebuilt from scratch."""
from __future__ import annotations

import json
from dataclasses import asdict

from ..llm.provider import LLMProvider
from .state import ArchitectureEdge, ArchitectureNode, ArchitectureState

_LIST_FIELDS = ("decisions", "assumptions", "requirements", "risks", "trade_offs", "action_items")


async def propose_followup(provider: LLMProvider, state: ArchitectureState, request: str) -> ArchitectureState:
    current = json.dumps(asdict(state), indent=2)
    prompt = f'''You are a local system-design copilot evolving an existing architecture.
Return ONLY JSON with keys: title, nodes, edges, decisions, assumptions, requirements, risks, trade_offs, action_items.
"nodes" is a list of {{"id": string, "label": string, "kind": string}}.
"edges" is a list of {{"source": node id, "target": node id, "label": string}}.
Every other key is a list of short strings.
Preserve everything from CURRENT STATE the request does not ask to change — extend or modify it, never discard
unrelated nodes/edges/decisions. Keep existing node ids stable when a node is only relabeled/annotated.
CURRENT STATE:\n{current}\nEND CURRENT STATE
FOLLOW-UP REQUEST: {request}'''

    parts = []
    async for delta in provider.generate_stream(prompt, timeout=45):
        parts.append(delta)

    try:
        parsed = json.loads("".join(parts))
    except (ValueError, TypeError):
        # The local model didn't return reviewable JSON — evolve nothing
        # rather than risk silently discarding the existing diagram.
        return state

    nodes = _parse_nodes(parsed.get("nodes"), fallback=state.nodes)
    edges = _parse_edges(parsed.get("edges"), fallback=state.edges)
    fields = {name: _parse_string_list(parsed.get(name), fallback=getattr(state, name)) for name in _LIST_FIELDS}
    title = parsed.get("title") if isinstance(parsed.get("title"), str) and parsed.get("title") else state.title

    return ArchitectureState(version=state.version, title=title, nodes=nodes, edges=edges, **fields)


def _parse_nodes(raw, fallback: list[ArchitectureNode]) -> list[ArchitectureNode]:
    if not isinstance(raw, list):
        return fallback
    nodes = []
    for item in raw:
        if not isinstance(item, dict):
            return fallback
        try:
            nodes.append(ArchitectureNode(id=str(item["id"]), label=str(item.get("label", item["id"])), kind=str(item.get("kind", "service"))))
        except (KeyError, TypeError):
            return fallback
    return nodes or fallback


def _parse_edges(raw, fallback: list[ArchitectureEdge]) -> list[ArchitectureEdge]:
    if not isinstance(raw, list):
        return fallback
    edges = []
    for item in raw:
        if not isinstance(item, dict):
            return fallback
        try:
            edges.append(ArchitectureEdge(source=str(item["source"]), target=str(item["target"]), label=str(item.get("label", ""))))
        except (KeyError, TypeError):
            return fallback
    return edges


def _parse_string_list(raw, fallback: list[str]) -> list[str]:
    if not isinstance(raw, list):
        return fallback
    return [str(item) for item in raw if isinstance(item, (str, int, float))]
