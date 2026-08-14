"""Typed builders for the mock-pipeline event payloads.

Each function returns the `data` dict for one `Event` (see `protocol.py`).
Keeping these as small typed builders (rather than assembling dicts inline
in `mock/live_feed.py`) is the "typed model on each side" half of the
wire-format requirement — Swift's `IPCEventDataModels.swift` mirrors these
shapes field-for-field.
"""

from __future__ import annotations

from typing import Optional


def session_started(session_id: str) -> dict:
    return {"session_id": session_id}


def session_ended(session_id: str) -> dict:
    return {"session_id": session_id}


# Section 16: which physical audio track a chunk/transcript came from —
# "mixed" (single-track mode, the default, unchanged from every earlier
# section) or the two separated-track sources. `speaker_role` is a pure,
# static function of `source` alone (never the orchestrator's dynamic
# "I'm answering" suppression state, which is a behavioral concept, not
# an identity label) — "unknown" is an honest admission, not a guess.
_SPEAKER_ROLE_BY_SOURCE = {"meeting_audio": "interviewer", "microphone": "user"}


def _speaker_role_for_source(source: str) -> str:
    return _SPEAKER_ROLE_BY_SOURCE.get(source, "unknown")


def transcript_partial(session_id: str, text: str, source: str = "mixed") -> dict:
    return {"session_id": session_id, "text": text, "source": source, "speaker_role": _speaker_role_for_source(source)}


def transcript_final(
    session_id: str,
    segment_id: str,
    text: str,
    started_at: float,
    ended_at: Optional[float],
    source: str = "mixed",
) -> dict:
    return {
        "session_id": session_id,
        "id": segment_id,
        "text": text,
        "started_at": started_at,
        "ended_at": ended_at,
        "is_final": True,
        "source": source,
        "speaker_role": _speaker_role_for_source(source),
    }


# Section 19: a typed, safe rejection diagnostic — never the rejected
# text itself. Lets Swift show a compact "N noise events filtered" count
# without ever exposing what was actually (mis)heard.
def transcript_rejected(session_id: str, reason: str) -> dict:
    return {"session_id": session_id, "reason": reason}


def question_detected(
    session_id: str,
    question_id: str,
    text: str,
    confidence: float = 1.0,
    detected_at: Optional[float] = None,
) -> dict:
    return {
        "session_id": session_id,
        "question_id": question_id,
        "text": text,
        "confidence": confidence,
        "detected_at": detected_at if detected_at is not None else 0.0,
    }


def answer_started(session_id: str, question_id: str, sequence: int = 1) -> dict:
    return {"session_id": session_id, "question_id": question_id, "sequence": sequence}


def answer_delta(session_id: str, question_id: str, delta: str, sequence: int = 1) -> dict:
    return {"session_id": session_id, "question_id": question_id, "delta": delta, "sequence": sequence}


# MARK: - Clean speakable-answer streaming (Section 18)
#
# `answer_delta`/`answer_draft_delta` above carry the provider's raw
# output (may include `<think>` blocks, format labels, or other
# non-speakable text) and are never sent over the wire anymore — see
# `ConversationOrchestrator._run_answer_generation`. `answer.speakable_delta`
# is the only delta-content event Swift should render: clean, candidate
# prose only, already stripped of reasoning blocks and protocol labels
# by `SpeakableAnswerStream`.


def answer_speakable_delta(session_id: str, question_id: str, delta: str, sequence: int = 1) -> dict:
    return {"session_id": session_id, "question_id": question_id, "delta": delta, "sequence": sequence}


def answer_speakable_draft_delta(session_id: str, question_id: str, delta: str, sequence: int = 1) -> dict:
    return {"session_id": session_id, "question_id": question_id, "delta": delta, "sequence": sequence}


def answer_slow_warning(session_id: str, question_id: str, sequence: int = 1) -> dict:
    """No clean speakable text has arrived yet after a bounded wait
    (`_FIRST_USABLE_TEXT_WARNING_SECONDS`) — the generation may still be
    reasoning or the model may just be slow. Swift shows "Local model is
    taking longer than expected" plus Retry/Skip, while keeping the prior
    completed answer visible and the queue intact."""
    return {"session_id": session_id, "question_id": question_id, "sequence": sequence}


def answer_completed(
    session_id: str,
    question_id: str,
    question: str,
    talking_points: list[str],
    sources: list[dict],
    sequence: int = 1,
    caveat: str = "",
    answer_text: str = "",
    is_failed: bool = False,
) -> dict:
    """`sources` (Section 9): a list of structured references —
    `{"document_id", "file_name", "chunk_id", "excerpt"}` each — never
    plain strings. Must correspond to chunks actually retrieved for this
    answer; `[]` whenever no retrieval occurred or nothing met the
    relevance threshold. Never fabricated.

    `answer_text` (Section 16): the natural, speakable answer — the
    primary content Swift renders; `talking_points` is optional
    supporting detail, never the sole answer.

    `is_failed` (Section 17): `True` when generation failed/timed out —
    `answer_text` is then a status message, not a real answer, and Swift
    must never promote it to the primary/durable answer slot (it should
    preserve whatever completed answer was already showing and surface
    this as a dismissable, retryable error instead)."""
    return {
        "session_id": session_id,
        "question_id": question_id,
        "question": question,
        "answer_text": answer_text,
        "talking_points": talking_points,
        "sources": sources,
        "sequence": sequence,
        "caveat": caveat,
        "is_failed": is_failed,
    }


# MARK: - Interviewer-turn scheduling (Section 17)
#
# A finalized interviewer question that arrives while a different
# question's answer is already generating is queued, never used to
# cancel the active answer — see `ConversationOrchestrator._enqueue_turn`.


def answer_queued(session_id: str, question_id: str, text: str, queue_position: int, queue_depth: int) -> dict:
    """`queue_position` is 1-based (this turn's place in line);
    `queue_depth` is the total number of turns currently waiting,
    including this one — lets Swift show an honest "N questions
    pending" status rather than nothing happening."""
    return {
        "session_id": session_id,
        "question_id": question_id,
        "text": text,
        "queue_position": queue_position,
        "queue_depth": queue_depth,
    }


def answer_queue_overflow(session_id: str, question_id: str, text: str) -> dict:
    """The bounded queue (see `_MAX_QUEUED_TURNS`) was already full — this
    finalized question was not queued and will not be answered. Emitted
    so Swift can say so honestly instead of the question silently
    vanishing."""
    return {"session_id": session_id, "question_id": question_id, "text": text}


def answer_dequeued(session_id: str, question_id: str, queue_depth: int) -> dict:
    """A previously queued turn just started generating — `queue_depth`
    is how many remain waiting behind it."""
    return {"session_id": session_id, "question_id": question_id, "queue_depth": queue_depth}


# MARK: - Answer latency diagnostics (Section 17)
#
# Opt-in only (`VEYA_ANSWER_TIMING_DIAGNOSTICS=1`) — raw epoch-second
# timestamps for one answer round, never shown in the normal interview
# UI. Lets a developer diagnostics view and the local benchmark script
# (`core/scripts/benchmark_answer_latency.py`) measure real
# time-to-first-token/completion instead of guessing.


def answer_timing(
    session_id: str,
    question_id: str,
    stabilized_at: float,
    generation_request_start: float,
    first_raw_token_at: Optional[float],
    first_speakable_char_at: Optional[float],
    sequence: int = 1,
    completed_at: Optional[float] = None,
) -> dict:
    """`first_raw_token_at` (diagnostics only — may correspond to hidden
    reasoning content, never rendered) vs. `first_speakable_char_at`
    (Section 18: the real "first usable answer" moment — the first
    character of clean, candidate-speakable prose). Swift's own render
    timestamp is captured client-side when it actually applies the first
    `answer.speakable_delta`, not sent from here."""
    return {
        "session_id": session_id,
        "question_id": question_id,
        "sequence": sequence,
        "stabilized_at": stabilized_at,
        "generation_request_start": generation_request_start,
        "first_raw_token_at": first_raw_token_at,
        "first_speakable_char_at": first_speakable_char_at,
        "completed_at": completed_at,
    }


def worker_ready(protocol_version: int, worker_version: str) -> dict:
    return {"protocol_version": protocol_version, "worker_version": worker_version}


# MARK: - Turn detection (Section 14)


def turn_state(session_id: str, state: str) -> dict:
    """`state` is one of "listening"/"speech"/"waiting_for_silence" — the
    raw VAD-derived signal, never transcript/prompt content."""
    return {"session_id": session_id, "state": state}


def question_classifying(session_id: str) -> dict:
    """Emitted only when a finalized turn is ambiguous enough to need the
    (slower) semantic classification stage — lets Swift show "Understanding
    question" instead of nothing happening for that stretch."""
    return {"session_id": session_id}


def turn_debug(
    session_id: str,
    rms: float,
    threshold: int,
    is_in_speech: bool,
    speech_seconds: float,
    silence_seconds: float,
) -> dict:
    """Real local VAD diagnostics for one processed audio chunk — the raw
    RMS amplitude and threshold it was compared against, never transcript
    content. Only emitted when diagnostics are explicitly enabled (see
    `VEYA_VAD_DIAGNOSTICS` in `transcription/session.py`); lets a developer
    screen show *why* a turn boundary did or didn't fire against real
    microphone input, rather than only Whisper's eventual text output."""
    return {
        "session_id": session_id,
        "rms": rms,
        "threshold": threshold,
        "is_in_speech": is_in_speech,
        "speech_seconds": speech_seconds,
        "silence_seconds": silence_seconds,
    }


# MARK: - Question candidate / draft answer lifecycle (Section 15)
#
# Additive alongside the Section 8/14 `question.detected`/`answer.started`/
# `answer.delta`/`answer.completed`/`question.rejected` events (kept
# unchanged for backward compatibility) — these give Swift the finer-
# grained "candidate spotted, still speaking" / "drafting before the
# turn is even finalized" / "refining" states the product spec requires,
# without breaking the existing stable contract.


def question_candidate(session_id: str, text: str) -> dict:
    """A still-open turn now plausibly reads as an answer request — may
    still be extended, replaced, or rejected before it finalizes."""
    return {"session_id": session_id, "text": text}


def question_updated(session_id: str, text: str) -> dict:
    """Later speech extended the current candidate's text without
    changing its meaning — same underlying question, more of it."""
    return {"session_id": session_id, "text": text}


def question_finalized(session_id: str, question_id: str, text: str, confidence: float) -> dict:
    """The turn reached a real boundary (VAD/stop/max-duration) or a
    stability debounce and was classified as an answer request."""
    return {"session_id": session_id, "question_id": question_id, "text": text, "confidence": confidence}


def answer_draft_started(session_id: str, question_id: str, sequence: int) -> dict:
    """A generation began speculatively, before the turn was finalized —
    may still be replaced or refined."""
    return {"session_id": session_id, "question_id": question_id, "sequence": sequence}


def answer_draft_delta(session_id: str, question_id: str, delta: str, sequence: int) -> dict:
    return {"session_id": session_id, "question_id": question_id, "delta": delta, "sequence": sequence}


def answer_draft_replaced(session_id: str, question_id: str, sequence: int) -> dict:
    """A new generation is superseding a still-active previous one for
    this turn (materially different text, or a finalize-triggered
    refinement pass) — Swift should discard whatever the previous
    sequence had streamed and start fresh, never show both."""
    return {"session_id": session_id, "question_id": question_id, "sequence": sequence}


def answer_cancelled(session_id: str, question_id: str, sequence: int) -> dict:
    """A draft was cancelled with no replacement following it (e.g. the
    finalized turn was ultimately classified as not an answer request, or
    an explicit `answer.cancel`)."""
    return {"session_id": session_id, "question_id": question_id, "sequence": sequence}


def question_rejected(session_id: str) -> dict:
    """Emitted when a finalized turn was classified as not an
    answer-request — lets Swift return to "Listening" instead of being
    stuck on "Understanding question" with no further signal."""
    return {"session_id": session_id}


# MARK: - Knowledge ingestion events (Section 9)


def knowledge_ingestion_started(session_id: str, document_id: str, file_name: str) -> dict:
    return {"session_id": session_id, "document_id": document_id, "file_name": file_name}


def knowledge_ingestion_progress(session_id: str, document_id: str, stage: str, chunk_count: int) -> dict:
    return {"session_id": session_id, "document_id": document_id, "stage": stage, "chunk_count": chunk_count}


def knowledge_ingestion_completed(session_id: str, document_id: str, file_name: str, chunk_count: int) -> dict:
    return {
        "session_id": session_id,
        "document_id": document_id,
        "file_name": file_name,
        "chunk_count": chunk_count,
    }


def knowledge_ingestion_failed(session_id: str, document_id: str, file_name: str, status: str, reason: str) -> dict:
    """`reason` is always a safe, typed description (see
    `knowledge.errors.KnowledgeError`) — never document content."""
    return {
        "session_id": session_id,
        "document_id": document_id,
        "file_name": file_name,
        "status": status,
        "reason": reason,
    }
