"""Deterministic mocked live-session event feed.

Mirrors the shape of Swift's `MockTranscriptSource`/`MockAnswerGenerator`
(same idea: canned script, no real intelligence) but runs on the Python
side and drives the session over IPC events instead of direct Swift
state mutation. Fully cancellation-safe: `mock.stop_live_feed` cancels the
`asyncio.Task` running `run_live_feed`, and every `await` point here is a
plain `asyncio.sleep`/event-emit that propagates `CancelledError` cleanly
with no dangling state.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from typing import Awaitable, Callable

from ..ipc import events

EmitEvent = Callable[[str, dict], Awaitable[None]]


@dataclass(frozen=True)
class ScriptLine:
    text: str
    duration_seconds: float
    is_question: bool = False


# Deterministic, canned — intentionally mirrors
# `MockTranscriptSource.defaultScript` in Swift so the two fallback paths
# feel the same to a user, even though they're independent implementations.
DEFAULT_SCRIPT: list[ScriptLine] = [
    ScriptLine("Thanks everyone for joining, let's get started with the migration recap.", 0.4),
    ScriptLine("We moved the auth service first since everything else depended on it.", 0.4),
    ScriptLine("So why did the migration take six weeks in total?", 0.4, is_question=True),
    ScriptLine("That's a fair question, let me walk through the timeline.", 0.4),
    ScriptLine("We rolled it out in stages to keep backward compatibility the whole way through.", 0.4),
]

ANSWER_TALKING_POINTS = [
    "Authentication dependency",
    "Staged rollout",
    "Backward compatibility",
    "Final validation",
]
# Structured (Section 9 wire shape), same as a real retrieved source —
# canned/deterministic, never claims to come from real retrieval.
ANSWER_SOURCES = [
    {
        "document_id": "mock-document",
        "file_name": "Migration Notes",
        "chunk_id": "mock-chunk-1",
        "excerpt": "The migration was staged to preserve backward compatibility throughout.",
    }
]

_PARTIAL_STEP_SECONDS = 0.1
_ANSWER_DELTA_STEP_SECONDS = 0.15


async def run_live_feed(
    session_id: str,
    emit_event: EmitEvent,
    script: list[ScriptLine] | None = None,
) -> None:
    """Runs the deterministic mock pipeline for one session, emitting
    events in this fixed order:

        session.started
        (per line) transcript.partial* -> transcript.final
        (on the question line) question.detected, answer.started,
            answer.delta*, answer.completed
        session.ended

    Safe to cancel at any point — cancellation during `asyncio.sleep` or
    between event emissions simply stops the loop; no partial state is
    left behind because nothing here is mutated outside this coroutine's
    own locals.
    """
    script = script if script is not None else DEFAULT_SCRIPT

    await emit_event("session.started", events.session_started(session_id))

    elapsed = 0.0
    answer_sequence = 0
    for line in script:
        await _emit_transcript_line(session_id, line, elapsed, emit_event)
        elapsed += line.duration_seconds

        if line.is_question:
            answer_sequence += 1
            await _emit_answer(session_id, line.text, emit_event, sequence=answer_sequence)

    await emit_event("session.ended", events.session_ended(session_id))


async def _emit_transcript_line(
    session_id: str,
    line: ScriptLine,
    started_at: float,
    emit_event: EmitEvent,
) -> None:
    # A couple of growing partial updates before the final segment, so the
    # overlay has something to show while a "sentence" is "being spoken."
    words = line.text.split(" ")
    midpoint = max(len(words) // 2, 1)
    partial_text = " ".join(words[:midpoint])
    if partial_text:
        await emit_event("transcript.partial", events.transcript_partial(session_id, partial_text))
        await asyncio.sleep(_PARTIAL_STEP_SECONDS)

    await asyncio.sleep(max(line.duration_seconds - _PARTIAL_STEP_SECONDS, 0))

    ended_at = started_at + line.duration_seconds
    await emit_event(
        "transcript.final",
        events.transcript_final(
            session_id=session_id,
            segment_id=str(uuid.uuid4()),
            text=line.text,
            started_at=started_at,
            ended_at=ended_at,
        ),
    )


async def _emit_answer(session_id: str, question_text: str, emit_event: EmitEvent, sequence: int = 1) -> None:
    question_id = str(uuid.uuid4())
    await emit_event(
        "question.detected",
        events.question_detected(session_id, question_id, question_text, confidence=1.0, detected_at=time.time()),
    )

    await emit_event("answer.started", events.answer_started(session_id, question_id, sequence=sequence))

    for point in ANSWER_TALKING_POINTS:
        await asyncio.sleep(_ANSWER_DELTA_STEP_SECONDS)
        await emit_event("answer.delta", events.answer_delta(session_id, question_id, point, sequence=sequence))

    await asyncio.sleep(_ANSWER_DELTA_STEP_SECONDS)
    await emit_event(
        "answer.completed",
        events.answer_completed(
            session_id=session_id,
            question_id=question_id,
            question=question_text,
            talking_points=list(ANSWER_TALKING_POINTS),
            sources=list(ANSWER_SOURCES),
            sequence=sequence,
        ),
    )
