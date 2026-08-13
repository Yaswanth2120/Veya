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


def transcript_partial(session_id: str, text: str) -> dict:
    return {"session_id": session_id, "text": text}


def transcript_final(
    session_id: str,
    segment_id: str,
    text: str,
    started_at: float,
    ended_at: Optional[float],
) -> dict:
    return {
        "session_id": session_id,
        "id": segment_id,
        "text": text,
        "started_at": started_at,
        "ended_at": ended_at,
        "is_final": True,
    }


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


def answer_completed(
    session_id: str,
    question_id: str,
    question: str,
    talking_points: list[str],
    sources: list[dict],
    sequence: int = 1,
    caveat: str = "",
) -> dict:
    """`sources` (Section 9): a list of structured references —
    `{"document_id", "file_name", "chunk_id", "excerpt"}` each — never
    plain strings. Must correspond to chunks actually retrieved for this
    answer; `[]` whenever no retrieval occurred or nothing met the
    relevance threshold. Never fabricated."""
    return {
        "session_id": session_id,
        "question_id": question_id,
        "question": question,
        "talking_points": talking_points,
        "sources": sources,
        "sequence": sequence,
        "caveat": caveat,
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
