"""Streams an LLM answer and parses it into the overlay's shape.
`parse_answer_text` is a pure function (fully testable with canned text,
no provider needed); `generate_answer` is the thin async orchestration
around it. Cancellation is deliberately *not* handled here — the caller
(`orchestrator.py`) cancels the `asyncio.Task` running `generate_answer`,
and `CancelledError` propagates through `provider.generate_stream`
unchanged, same as any other `async for` loop.
"""

from __future__ import annotations

import re
from typing import Awaitable, Callable, List

from .models import ParsedAnswer
from ..llm.provider import LLMProvider

DEFAULT_GENERATION_TIMEOUT_SECONDS = 30.0

_ANSWER_RE = re.compile(r"^\s*ANSWER:\s*(.+)$", re.IGNORECASE)
_POINTS_HEADER_RE = re.compile(r"^\s*POINTS:\s*$", re.IGNORECASE)
_BULLET_RE = re.compile(r"^\s*[-*]\s+(\S.*)$")
_CAVEAT_RE = re.compile(r"^\s*CAVEAT:\s*(.+)$", re.IGNORECASE)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_MAX_TALKING_POINTS = 5


def parse_answer_text(raw_text: str) -> ParsedAnswer:
    """Parses the `ANSWER:`/`POINTS:`/`CAVEAT:` format requested by
    `context_builder.render_prompt`. Falls back to sentence-splitting the
    raw text if the model didn't follow that format — never returns
    nothing just because formatting wasn't followed exactly."""
    short_answer = ""
    talking_points: List[str] = []
    caveat = ""
    in_points = False

    for line in raw_text.strip().splitlines():
        answer_match = _ANSWER_RE.match(line)
        if answer_match:
            short_answer = answer_match.group(1).strip()
            in_points = False
            continue

        if _POINTS_HEADER_RE.match(line):
            in_points = True
            continue

        caveat_match = _CAVEAT_RE.match(line)
        if caveat_match:
            candidate = caveat_match.group(1).strip()
            if candidate.lower().rstrip(".") not in ("none", "n/a", ""):
                caveat = candidate
            in_points = False
            continue

        if in_points:
            bullet_match = _BULLET_RE.match(line)
            if bullet_match:
                talking_points.append(bullet_match.group(1).strip())

    if short_answer or talking_points:
        return ParsedAnswer(short_answer=short_answer, talking_points=talking_points[:_MAX_TALKING_POINTS], caveat=caveat)

    sentences = [s.strip() for s in _SENTENCE_SPLIT_RE.split(raw_text.strip()) if s.strip()]
    if not sentences:
        return ParsedAnswer(short_answer="", talking_points=[], caveat="")
    return ParsedAnswer(short_answer=sentences[0], talking_points=sentences[:_MAX_TALKING_POINTS], caveat="")


async def generate_answer(
    provider: LLMProvider,
    prompt: str,
    *,
    on_delta: Callable[[str], Awaitable[None]],
    timeout: float = DEFAULT_GENERATION_TIMEOUT_SECONDS,
) -> ParsedAnswer:
    """Streams `prompt` through `provider`, calling `on_delta` for every
    chunk (raw growing text, matching the existing partial-answer preview
    behavior from the mock pipeline), then parses the fully-accumulated
    response. Lets `LLMTimeoutError`/`LLMProviderError`/`LLMUnavailableError`
    and `asyncio.CancelledError` propagate unchanged — this function adds
    no error handling of its own, only accumulation + parsing."""
    accumulated: List[str] = []
    async for delta in provider.generate_stream(prompt, timeout=timeout):
        accumulated.append(delta)
        await on_delta(delta)
    return parse_answer_text("".join(accumulated))
