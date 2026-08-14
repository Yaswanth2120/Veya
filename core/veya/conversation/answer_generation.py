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
from typing import Awaitable, Callable, List, Optional

from .models import ParsedAnswer
from .speakable_stream import SpeakableAnswerStream
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
    `context_builder.render_prompt`. `ANSWER:` is a natural, speakable,
    potentially multi-sentence/multi-line answer — the primary content —
    so it accumulates every line up through the next recognized header
    rather than only the header's own line. Falls back to
    sentence-splitting the raw text if the model didn't follow the format
    at all — never returns nothing just because formatting wasn't
    followed exactly."""
    short_answer = ""
    answer_lines: List[str] = []
    talking_points: List[str] = []
    caveat = ""
    in_answer = False
    in_points = False

    for line in raw_text.strip().splitlines():
        answer_match = _ANSWER_RE.match(line)
        if answer_match:
            answer_lines = [answer_match.group(1).strip()]
            in_answer = True
            in_points = False
            continue

        if _POINTS_HEADER_RE.match(line):
            in_answer = False
            in_points = True
            continue

        caveat_match = _CAVEAT_RE.match(line)
        if caveat_match:
            candidate = caveat_match.group(1).strip()
            if candidate.lower().rstrip(".") not in ("none", "n/a", ""):
                caveat = candidate
            in_answer = False
            in_points = False
            continue

        if in_points:
            bullet_match = _BULLET_RE.match(line)
            if bullet_match:
                talking_points.append(bullet_match.group(1).strip())
        elif in_answer and line.strip():
            # A natural spoken answer often continues onto following
            # lines/paragraphs rather than staying on the `ANSWER:` line —
            # accumulate them all as one answer, not just the first line.
            answer_lines.append(line.strip())

    short_answer = " ".join(answer_lines).strip()

    if short_answer or talking_points:
        return ParsedAnswer(short_answer=short_answer, talking_points=talking_points[:_MAX_TALKING_POINTS], caveat=caveat)

    # The model ignored the requested format entirely — the raw text is
    # itself presumably natural prose, so it becomes the answer verbatim
    # rather than being chopped into a "first sentence" + duplicated into
    # talking points (which produced a bullet-only-looking result even
    # when the model had actually written a real natural answer).
    fallback_text = raw_text.strip()
    if not fallback_text:
        return ParsedAnswer(short_answer="", talking_points=[], caveat="")
    return ParsedAnswer(short_answer=fallback_text, talking_points=[], caveat="")


async def generate_answer(
    provider: LLMProvider,
    prompt: str,
    *,
    on_speakable_delta: Callable[[str], Awaitable[None]],
    on_raw_delta: Optional[Callable[[str], Awaitable[None]]] = None,
    timeout: float = DEFAULT_GENERATION_TIMEOUT_SECONDS,
) -> ParsedAnswer:
    """Streams `prompt` through `provider`, running every raw chunk
    through a `SpeakableAnswerStream` (Section 18) — `on_speakable_delta`
    only ever receives clean, speakable prose (never `<think>` content,
    the `ANSWER:`/`POINTS:`/`CAVEAT:` labels, or the `POINTS:`/`CAVEAT:`
    sections themselves). `on_raw_delta`, if given, receives the
    provider's raw chunks unchanged — callers must never render this in
    normal UI; it exists only for optional, metadata-scale diagnostics
    (see `ConversationOrchestrator`). The final `ParsedAnswer` is parsed
    from the stream's accumulated *clean* text, so `answer.completed`
    and the live speakable stream are always built from the exact same
    source. Lets `LLMTimeoutError`/`LLMProviderError`/`LLMUnavailableError`
    and `asyncio.CancelledError` propagate unchanged — this function adds
    no error handling of its own, only accumulation + parsing."""
    stream = SpeakableAnswerStream()
    async for delta in provider.generate_stream(prompt, timeout=timeout):
        if on_raw_delta is not None:
            await on_raw_delta(delta)
        speakable = stream.feed(delta)
        if speakable:
            await on_speakable_delta(speakable)
    return parse_answer_text(stream.clean_text())
