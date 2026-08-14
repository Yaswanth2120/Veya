"""Tracks one spoken turn's evolving "is this an answer request, and how
sure are we" state across incremental ASR hypotheses, so an answer can
start drafting on a high-confidence candidate well before a perfect
silence endpoint — the Section 15 requirement that Veya must not sit in
"Listening" waiting for an ideal turn boundary.

States (mirrors the product spec verbatim):
    idle        — no candidate text accumulated for the current turn.
    candidate   — some text has been seen that plausibly reads as an
                  answer request, but not confidently enough to draft.
    drafting    — confidence is high enough that an answer generation has
                  actually been started for this candidate.
    stabilizing — a short debounce/stability window is running (no new
                  extending fragment for N ms) before committing to the
                  next transition — set/cleared by the caller (the
                  orchestrator owns the actual timer).
    finalized   — a real turn boundary (VAD/stop/max-duration) confirmed
                  the question; any pending refinement is decided here.
    rejected    — classification decided this was not an answer request.

This module only decides *what changed* and *what to do about it*
(`TrackerDecision`) — it never emits IPC events or talks to an LLM
itself; `orchestrator.py` translates decisions into events/generation
calls, which keeps this class trivially unit-testable without any I/O.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from .question_detector import QuestionDetector

_PUNCTUATION_RE = re.compile(r"[^\w\s]")
_WHITESPACE_RE = re.compile(r"\s+")


def _normalize(text: str) -> str:
    """Loose equality for comparing a streaming partial's wording against
    the eventual final transcript of the *same* utterance — Whisper's
    final pass over the accumulated window often cleans up punctuation/
    capitalization/filler words the rolling partial didn't have yet, and
    that alone must never be treated as a "materially different"
    question requiring a wasted regeneration pass."""
    without_punctuation = _PUNCTUATION_RE.sub("", text.lower())
    return _WHITESPACE_RE.sub(" ", without_punctuation).strip()


class CandidateState(str, Enum):
    IDLE = "idle"
    CANDIDATE = "candidate"
    DRAFTING = "drafting"
    STABILIZING = "stabilizing"
    FINALIZED = "finalized"
    REJECTED = "rejected"


@dataclass
class TrackerDecision:
    state: CandidateState
    text: str = ""
    emit_candidate: bool = False
    emit_updated: bool = False
    # `True` when a draft generation should begin or restart right now.
    start_or_replace_draft: bool = False
    # Distinguishes `answer.draft_started` (nothing was active) from
    # `answer.draft_replaced` (a still-active draft is being superseded).
    is_replace: bool = False


class QuestionCandidateTracker:
    def __init__(self, detector: QuestionDetector) -> None:
        self._detector = detector
        self.state: CandidateState = CandidateState.IDLE
        self._text = ""
        self._pre_stabilize_state: Optional[CandidateState] = None
        # The exact text the currently-active draft (if any) was
        # generated from — lets `on_finalize` decide whether the fully
        # finalized text actually adds anything worth a refinement pass,
        # rather than always regenerating.
        self._drafted_text: Optional[str] = None

    def reset(self) -> None:
        self.state = CandidateState.IDLE
        self._text = ""
        self._pre_stabilize_state = None
        self._drafted_text = None

    def mark_stabilizing(self) -> None:
        """Called by the orchestrator right after it schedules a
        stability debounce timer — a purely observational state change,
        reversed automatically the next time text changes or the caller
        finalizes/rejects."""
        if self.state in (CandidateState.CANDIDATE, CandidateState.DRAFTING):
            self._pre_stabilize_state = self.state
            self.state = CandidateState.STABILIZING

    def _effective_state(self) -> CandidateState:
        if self.state == CandidateState.STABILIZING and self._pre_stabilize_state is not None:
            return self._pre_stabilize_state
        return self.state

    def on_pending_text_changed(self, text: str) -> TrackerDecision:
        """Called whenever the still-open turn's accumulated text changes
        (a new ASR hypothesis arrived, no turn boundary yet)."""
        stripped = text.strip()
        if not stripped:
            return TrackerDecision(state=self.state, text=self._text)

        if self.state in (CandidateState.FINALIZED, CandidateState.REJECTED):
            # A fragment arriving after the previous turn concluded means
            # a brand new turn has begun.
            self.reset()

        score = self._detector.score(stripped)
        # A normalized-prefix check, not a raw one: a real incremental ASR
        # engine frequently revises punctuation/capitalization on the
        # *already-spoken* portion of an utterance between one partial
        # and the next (e.g. "what was the bottleneck" -> "What was the
        # bottleneck,") — a literal `str.startswith` would misread that
        # as a materially different question and needlessly replace the
        # draft. Comparing normalized forms tolerates that, and still
        # correctly treats a real topic change as non-extending.
        is_extension = bool(self._text) and _normalize(stripped).startswith(_normalize(self._text))
        previous_text = self._text
        self._text = stripped
        effective = self._effective_state()

        if effective == CandidateState.IDLE:
            if score <= 0.0:
                return TrackerDecision(state=self.state, text=stripped)
            self.state = CandidateState.CANDIDATE
            decision = TrackerDecision(state=self.state, text=stripped, emit_candidate=True)
            if score >= self._detector.confidence_threshold:
                self._begin_drafting(decision, is_replace=False)
            return decision

        if effective == CandidateState.CANDIDATE:
            decision = TrackerDecision(state=CandidateState.CANDIDATE, text=stripped)
            if is_extension:
                decision.emit_updated = True
            else:
                decision.emit_candidate = True
            self.state = CandidateState.CANDIDATE
            if score >= self._detector.confidence_threshold:
                self._begin_drafting(decision, is_replace=False)
            return decision

        # effective == DRAFTING
        decision = TrackerDecision(state=CandidateState.DRAFTING, text=stripped)
        self.state = CandidateState.DRAFTING
        if is_extension:
            # "Retain the same visible answer stream where possible" —
            # a pure extension doesn't restart generation on its own;
            # `on_finalize` decides later whether the fuller text is
            # actually worth a refinement pass.
            decision.emit_updated = True
        else:
            # A materially different fragment while already drafting —
            # the meaning changed, so the obsolete draft is superseded
            # immediately rather than waiting for finalize.
            decision.emit_candidate = True
            self._begin_drafting(decision, is_replace=True)
        return decision

    def on_finalize(self, text: str) -> TrackerDecision:
        """Called when a real turn boundary (VAD/stop/max-duration) or
        the orchestrator's own stability debounce confirms the turn is
        complete. Regeneration only happens if the finalized text is
        actually different from whatever the active draft was already
        generated from — an unchanged extension doesn't pay for a second
        generation pass."""
        stripped = text.strip()
        effective = self._effective_state()
        was_drafting = effective == CandidateState.DRAFTING
        needs_regeneration = not was_drafting or (
            self._drafted_text is None or _normalize(stripped) != _normalize(self._drafted_text)
        )

        decision = TrackerDecision(state=CandidateState.FINALIZED, text=stripped)
        if needs_regeneration:
            decision.start_or_replace_draft = True
            decision.is_replace = was_drafting
            self._drafted_text = stripped

        self.state = CandidateState.FINALIZED
        return decision

    def note_draft_deferred(self) -> None:
        """Called by the orchestrator when it decided *not* to act on a
        `start_or_replace_draft` decision (a different turn's answer was
        already generating/queued) — reverts the `DRAFTING` transition
        this class itself just made back to `CANDIDATE`, and clears
        `_drafted_text`, so a later `on_finalize` for this turn correctly
        concludes no draft actually exists yet and always regenerates,
        instead of wrongly matching against text that was never actually
        sent to the model."""
        if self.state == CandidateState.DRAFTING:
            self.state = CandidateState.CANDIDATE
        self._drafted_text = None

    def on_reject(self) -> None:
        self.state = CandidateState.REJECTED
        self._drafted_text = None

    def _begin_drafting(self, decision: TrackerDecision, is_replace: bool) -> None:
        self.state = CandidateState.DRAFTING
        decision.state = CandidateState.DRAFTING
        decision.start_or_replace_draft = True
        decision.is_replace = is_replace
        self._drafted_text = self._text
