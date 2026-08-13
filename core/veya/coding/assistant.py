"""Local-LLM coding assistance with explicit, reviewable edit proposals."""
from __future__ import annotations

import json
from ..llm.provider import LLMProvider
from .workspace import CodeFile


async def propose(provider: LLMProvider, file: CodeFile, operation: str, request: str) -> dict:
    history_block = ""
    if file.history:
        # Retains the follow-up conversation instead of restarting from an
        # empty prompt each call — each entry is a prior operation/request/
        # explanation this same file already went through.
        lines = [f'- [{entry.get("operation", "")}] request: {entry.get("request", "")} -> {entry.get("explanation", "")}' for entry in file.history]
        history_block = "PRIOR REQUESTS ON THIS FILE (most recent last):\n" + "\n".join(lines) + "\n"
    prompt = f'''You are a local coding copilot. Operation: {operation}.
Return ONLY JSON with keys explanation, edits, tests, complexity.
Each edit is {{"start": integer, "end": integer, "replacement": string}} using offsets in SOURCE.
Prefer the smallest correct edit that satisfies the request. Never execute code or claim execution.
FILE: {file.name} ({file.language}), VERSION: {file.version}
{history_block}SOURCE:\n{file.content}\nEND SOURCE
REQUEST: {request}'''
    parts = []
    async for delta in provider.generate_stream(prompt, timeout=30): parts.append(delta)
    try:
        parsed = json.loads("".join(parts))
        edits = parsed.get("edits", [])
        if not isinstance(edits, list): edits = []
        return {"base_version": file.version, "explanation": str(parsed.get("explanation", "")), "edits": edits,
                "tests": str(parsed.get("tests", "")), "complexity": str(parsed.get("complexity", ""))}
    except (ValueError, TypeError):
        return {"base_version": file.version, "explanation": "The local model did not return a reviewable edit proposal.", "edits": [], "tests": "", "complexity": ""}
