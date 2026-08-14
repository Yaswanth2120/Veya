"""Ties question detection and answer generation to the real
transcription flow for one Live Session. `TranscriptionSession` calls
`handle_final_transcript` after each `transcript.final` it emits (see
`transcription/session.py`'s `on_final_transcript` hook); this is the
only entry point — partial transcripts are never analyzed.

Exactly one answer generation runs at a time per session: starting a new
one always cancels whatever was still in flight first (a new question
supersedes an old one), and `close()`/`cancel_active_answer()` gives
`transcription.stop`/`answer.cancel` a clean way to stop it.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from .answer_generation import generate_answer
from .groundedness import check_answer_groundedness, safe_fallback_answer
from .context_builder import render_prompt
from .models import ParsedAnswer, SessionContext
from .question_candidate_tracker import CandidateState, QuestionCandidateTracker
from .transcript_eligibility import (
    TranscriptRejectionReason,
    classify_transcript_text,
    classify_turn_quality,
    looks_like_incomplete_sentence,
)
from .question_detector import QuestionDetector
from .semantic_classifier import LOW_CONFIDENCE_REJECT_BOUND, classify_turn
from .turn_assembler import TurnAssembler
from ..ipc import events
from ..knowledge.models import RetrievedChunk
from ..knowledge.retrieval import KnowledgeRetriever, chunk_sources
from ..llm.errors import LLMError
from ..llm.provider import LLMProvider

logger = logging.getLogger("veya.conversation")

EmitEvent = Callable[[str, dict], Awaitable[None]]

_GENERATION_FAILED_MESSAGE = "Answer generation failed — the local LLM provider became unavailable mid-response."
_NO_USABLE_TEXT_MESSAGE = "Answer generation finished without producing any speakable answer text."
# Section 18: how long to wait after a generation starts before warning
# that no clean, speakable text has arrived yet (a slow/reasoning model
# is still an honest "taking longer than expected", not a silent hang).
# Well under `DEFAULT_GENERATION_TIMEOUT_SECONDS` — this is a UX warning,
# not the point generation is abandoned.
_FIRST_USABLE_TEXT_WARNING_SECONDS = 6.0
_MAX_RECENT_TRANSCRIPT_CHARACTERS = 2_400
# Bounds how long `close()` waits for a still-in-flight answer (including
# one just started by a trailing turn flushed at session end) before
# giving up and cancelling it — long enough for a real local model to
# finish a normal answer, short enough that ending a session doesn't feel
# hung waiting on a slow/stuck generation.
_FINAL_ANSWER_SHUTDOWN_TIMEOUT_SECONDS = 10.0
# A real interview turn only finalizes on a VAD silence endpoint (default
# 1.2s of clean silence) — but continuous background noise, an interviewer
# who keeps talking, or the RMS threshold merging two utterances can mean
# that endpoint never arrives, leaving an obviously-complete question
# ("Tell me about yourself.") sitting unanswered indefinitely. Once the
# turn assembled *so far* already reads as a strong, unambiguous prompt on
# its own (the deterministic gate alone, not the slower semantic stage),
# this is how long to wait with no *new* fragment arriving before treating
# it as finished anyway — short enough to feel responsive, long enough
# that a fragment that's merely mid-utterance isn't answered prematurely.
_SPECULATIVE_FINALIZE_DEBOUNCE_SECONDS = 0.7
# Section 19: bounds how many extra debounce windows a structurally
# incomplete turn (dangling "and"/"or"/comma — see
# `looks_like_incomplete_sentence`) gets before speculative-finalizing
# anyway — a turn that genuinely trails off must not wait forever.
_MAX_SPECULATIVE_FINALIZE_EXTENSIONS = 4
# Section 17: several interviewer questions can be spoken in quick
# succession (the reported failure mode was cancelling a still-generating
# answer every time). This bounds the pending-turn queue so a burst of
# questions is handled honestly (queued, in order) rather than either
# silently dropped or allowed to grow unbounded.
_MAX_QUEUED_TURNS = 3
# Retrieval must never leave answer generation hanging — a slow/unavailable
# embedding provider or vector store still lets an ungrounded answer start
# immediately rather than the user staring at "Generating…" indefinitely.
_RETRIEVAL_TIMEOUT_SECONDS = 3.0


@dataclass
class _QueuedTurn:
    question_id: str
    question_text: str
    # When this turn was finalized/classified — the honest "stabilized
    # question time" for latency diagnostics, captured at queue time
    # rather than dequeue time (waiting in queue is not generation
    # latency).
    stabilized_at: float
    # Section 19: the "recent conversation" context as of the moment
    # *this* turn finalized — captured here, not recomputed at dequeue
    # time, so another turn finalizing (and being remembered) while this
    # one waits in queue never leaks into this turn's prompt.
    recent_conversation_block: str = ""


class ConversationOrchestrator:
    def __init__(
        self,
        session_id: str,
        session_context: SessionContext,
        emit_event: EmitEvent,
        llm_provider: Optional[LLMProvider],
        question_detector: Optional[QuestionDetector] = None,
        retriever: Optional[KnowledgeRetriever] = None,
        memory_texts: Optional[list[str]] = None,
        emit_timing_diagnostics: bool = False,
    ) -> None:
        self.session_id = session_id
        self._session_context = session_context
        self._emit_event = emit_event
        self._llm_provider = llm_provider
        self._detector = question_detector or QuestionDetector()
        # Only ever *approved* memory (see `memory/store.py`) — never raw
        # transcript text — and only what the caller already resolved
        # before constructing this orchestrator.
        self._memory_texts = memory_texts or []
        # `None` (no retriever configured) and "retriever configured but
        # retrieved nothing" both mean the same thing downstream — an
        # unbounded, ungrounded prompt and `sources: []` — so no
        # additional availability flag is needed here beyond this.
        self._retriever = retriever
        self._sequence = 0
        self._active_answer_task: Optional[asyncio.Task] = None
        # The question detector remains intentionally fast and local, but
        # answer generation receives bounded preceding speech context. This
        # prevents a Whisper window boundary from stripping the subject from
        # an otherwise valid spoken prompt.
        self._recent_transcript_fragments: list[str] = []
        # Section 14: coalesces `transcript.final` fragments spanning
        # multiple Whisper windows into one complete spoken turn before
        # question detection/classification ever sees it — the fix for
        # the "each fragment judged independently" bug.
        self._turn_assembler = TurnAssembler()
        self._speculative_finalize_task: Optional[asyncio.Task] = None
        # Section 15: tracks candidate/drafting/stabilizing state across
        # incremental hypotheses so a high-confidence prompt can start
        # drafting an answer before a perfect silence endpoint, and so a
        # later extension/replacement never produces a duplicate visible
        # answer. Purely a decision-maker (see question_candidate_tracker.py)
        # — this class does all the actual event emission/generation.
        self._candidate_tracker = QuestionCandidateTracker(self._detector)
        # The question_id of a speculative draft actually started for the
        # CURRENTLY open interviewer turn, if any — scoped strictly to
        # that one turn (reset whenever a new turn begins or the current
        # one finalizes/rejects) so it never leaks into a later, unrelated
        # turn's decisions. Lets a finalize-triggered regeneration reuse
        # the same id (so Swift can recognize "this is the same evolving
        # question, not a new one") instead of minting a fresh one.
        # Distinct from `_active_answer_task`, which may belong to an
        # entirely different, earlier turn still generating (Section 17).
        self._current_turn_speculative_question_id: Optional[str] = None
        # Section 16: dual-input interview audio. `source` on every
        # transcript callback is one of "mixed" (single-track — today's
        # unchanged default and behavior), "meeting_audio" (separated-
        # track mode's interviewer channel), or "microphone" (separated-
        # track mode's user channel). A completely separate `TurnAssembler`
        # for the user's own speech — it only ever accumulates toward
        # `_recent_user_answer_text`, never toward candidate/draft
        # tracking, and is never affected by cancellation of an
        # interviewer-turn draft or vice versa.
        self._user_turn_assembler = TurnAssembler()
        self._recent_user_answer_text: Optional[str] = None
        # "I'm answering" hold-to-talk/toggle (mixed/microphone-only mode
        # only — see `set_user_speaking`) — while active, "mixed"-source
        # speech is treated exactly like separated-track "microphone"
        # speech: authoritative user context, never a draft trigger.
        self._user_speaking_suppressed = False
        # Section 17: bounded FIFO of finalized interviewer turns waiting
        # for the currently-active answer to finish — see `_enqueue_turn`/
        # `_start_next_queued_turn_if_any`. Never mutated from outside
        # `_process_finalized_turn`/those two methods.
        self._answer_queue: list[_QueuedTurn] = []
        # Guards against processing the exact same finalized turn twice in
        # a row (e.g. a VAD-boundary finalize and the speculative-debounce
        # finalize both firing for the same buffered text) — deliberately
        # only compares against the immediately preceding turn, never a
        # longer history, so a question genuinely repeated minutes later
        # is never mistaken for a duplicate.
        self._last_finalized_turn_text: Optional[str] = None
        # Opt-in only (`VEYA_ANSWER_TIMING_DIAGNOSTICS=1`, see
        # `dispatcher.py`) — real per-answer latency timestamps
        # (stabilized/request-start/first-token/completed) for developer
        # diagnostics and the local benchmark script only. Never shown in
        # the normal interview UI.
        self._emit_timing_diagnostics = emit_timing_diagnostics

    @property
    def answer_intelligence_available(self) -> bool:
        return self._llm_provider is not None

    def _role_for_source(self, source: str) -> str:
        """"interviewer"/"user"/"unknown" — see the Section 16 comment on
        `__init__`. Never persisted/logged as a claim of real speaker
        identification beyond what the source actually tells us: separated
        audio tracks (real, reliable) vs. everything else (never claimed
        reliable — "unknown" unless the user explicitly holds "I'm
        answering")."""
        if source == "meeting_audio":
            return "interviewer"
        if source == "microphone":
            return "user"
        if source == "mixed" and self._user_speaking_suppressed:
            return "user"
        return "unknown"

    def set_user_speaking(self, active: bool) -> None:
        """The mixed/microphone-only mode fallback control ("I'm
        answering" hold-to-talk or toggle) — while active, speech on the
        single "mixed" track is treated exactly like separated-track
        microphone speech: authoritative user context, never a draft
        trigger. Never affects separated-track mode, which already knows
        which physical track is which reliably."""
        self._user_speaking_suppressed = active

    async def handle_final_transcript(self, text: str, started_at: float, ended_at: float, source: str = "mixed") -> None:
        """Called for real, deduplicated final transcript text only —
        never for partials. A no-op session-wide if no LLM provider is
        available: detecting questions nobody can answer would just be a
        confusing dead-end UI state (see
        docs/QUESTION_AND_ANSWER_INTELLIGENCE.md's fallback behavior).
        `source="mixed"` (the default) is today's single-track behavior,
        unchanged. Feeds the fragment into the turn assembler — a turn
        only reaches classification once a real endpoint (VAD boundary/
        session stop/max duration) finalizes it, never on every individual
        fragment."""
        if not self.answer_intelligence_available:
            return

        if self._role_for_source(source) == "user":
            await self._handle_user_final_transcript(text, started_at, ended_at)
            return

        # Section 19: defense in depth — `TranscriptionSession`/
        # `StreamingTranscriptionSession` already filter non-speech
        # markers before this is ever called in production, but a
        # marker-only fragment must never enter turn assembly from any
        # call path.
        if classify_transcript_text(text) == TranscriptRejectionReason.NON_SPEECH_MARKER:
            await self._emit_event(
                "transcript.rejected",
                events.transcript_rejected(session_id=self.session_id, reason=TranscriptRejectionReason.NON_SPEECH_MARKER.value),
            )
            return

        finalized_turn = self._turn_assembler.add_fragment(text, started_at, ended_at)
        if finalized_turn is not None:
            self._cancel_speculative_finalize_timer()
            await self._process_finalized_turn(finalized_turn)
            return

        # The turn is still open (no VAD boundary reached it yet). If real
        # streaming partials are arriving (`handle_partial_transcript`),
        # they already drive candidate tracking far more responsively
        # than waiting for the next `transcript.final` fragment — this is
        # a fallback for engines that only ever produce finals (the
        # degraded batch-CLI path), so a strong prompt still isn't stuck
        # waiting purely on a VAD boundary there either.
        await self._advance_candidate_tracker(self._turn_assembler.peek_pending_text())

    async def handle_partial_transcript(self, text: str, ended_at: float, source: str = "mixed") -> None:
        """Called for every meaningful (non-empty, changed) streaming ASR
        partial hypothesis — never persisted, never fed into
        `TurnAssembler` (partials are never final transcript). This is
        now the primary driver of speculative candidate/draft tracking:
        a high-confidence prompt can start drafting from a partial alone,
        well before any `transcript.final` or VAD boundary arrives.
        `handle_final_transcript`/`handle_turn_boundary` still own the
        actual turn finalization and reconciliation. User-role speech
        (Section 16) never drives drafting even as a partial — only its
        eventual final transcript becomes authoritative context."""
        if not self.answer_intelligence_available:
            return
        if self._role_for_source(source) == "user":
            return
        await self._advance_candidate_tracker(text)

    async def _handle_user_final_transcript(self, text: str, started_at: float, ended_at: float) -> None:
        """The user's own speech (Section 16, separated-track mode or
        "I'm answering" suppression) never runs question detection/
        classification/drafting — it only ever updates the authoritative
        "what did the user actually just say" context a later interviewer
        follow-up grounds itself in. Assembled into complete turns the
        same way interviewer speech is (a real answer can span multiple
        Whisper windows too), just without any of the candidate/draft
        machinery running on it."""
        finalized_turn = self._user_turn_assembler.add_fragment(text, started_at, ended_at)
        if finalized_turn is not None:
            self._recent_user_answer_text = finalized_turn
            self._remember_transcript(finalized_turn)

    async def _advance_candidate_tracker(self, pending_text: str) -> None:
        stripped = pending_text.strip()
        if not stripped:
            return

        was_open_turn = self._candidate_tracker.state in (
            CandidateState.CANDIDATE, CandidateState.DRAFTING, CandidateState.STABILIZING,
        )
        if not was_open_turn:
            # A brand-new turn is beginning — any speculative-draft id
            # left over from a previous, now-finalized/rejected turn is
            # no longer relevant to this one.
            self._current_turn_speculative_question_id = None

        decision = self._candidate_tracker.on_pending_text_changed(stripped)

        if decision.emit_candidate:
            await self._emit_event("question.candidate", events.question_candidate(session_id=self.session_id, text=decision.text))
        elif decision.emit_updated:
            await self._emit_event("question.updated", events.question_updated(session_id=self.session_id, text=decision.text))

        if decision.start_or_replace_draft:
            if decision.is_replace and self._current_turn_speculative_question_id is not None:
                # Refining the SAME still-open turn's speculative draft —
                # always safe to cancel-and-restart, this never competes
                # with a different question.
                await self._start_answer_generation(
                    question_id=self._current_turn_speculative_question_id, question_text=decision.text,
                    emit_draft_marker=True, is_replace=True, is_draft_stream=True,
                    exclude_current_question_from_context=False,
                )
            elif self._active_answer_task is None and not self._answer_queue:
                # A brand-new turn is beginning speculative drafting, and
                # nothing else is active/queued — safe to start eagerly
                # for latency.
                question_id = str(uuid.uuid4())
                self._current_turn_speculative_question_id = question_id
                await self._start_answer_generation(
                    question_id=question_id, question_text=decision.text,
                    emit_draft_marker=True, is_replace=False, is_draft_stream=True,
                    exclude_current_question_from_context=False,
                )
            else:
                # Something else is already active/queued — speculatively
                # drafting here would just be wasted work for a turn that
                # hasn't even finalized yet. `on_finalize` queues it
                # properly once/if it actually finalizes; undo the
                # tracker's own DRAFTING transition so that later finalize
                # doesn't wrongly believe a draft already exists for this
                # text (see `note_draft_deferred`).
                self._candidate_tracker.note_draft_deferred()

        if decision.state == CandidateState.DRAFTING:
            self._schedule_speculative_finalize()
            self._candidate_tracker.mark_stabilizing()
        else:
            self._cancel_speculative_finalize_timer()

    async def handle_turn_boundary(self, boundary_time: float, source: str = "mixed") -> None:
        """Called by `TranscriptionSession` when local VAD detects a turn
        has ended at audio-timeline position `boundary_time`. If the
        fragment covering that boundary hasn't arrived yet (Whisper
        transcription lags real-time), this only records the boundary —
        `handle_final_transcript` finalizes once that fragment shows up."""
        if not self.answer_intelligence_available:
            return
        if self._role_for_source(source) == "user":
            finalized_turn = self._user_turn_assembler.request_finalize_at(boundary_time)
            if finalized_turn is not None:
                self._recent_user_answer_text = finalized_turn
                self._remember_transcript(finalized_turn)
            return
        finalized_turn = self._turn_assembler.request_finalize_at(boundary_time)
        if finalized_turn is not None:
            self._cancel_speculative_finalize_timer()
            await self._process_finalized_turn(finalized_turn)

    def _schedule_speculative_finalize(self) -> None:
        if self._speculative_finalize_task is not None and not self._speculative_finalize_task.done():
            self._speculative_finalize_task.cancel()
        self._speculative_finalize_task = asyncio.create_task(self._speculative_finalize_after_debounce())

    def _cancel_speculative_finalize_timer(self) -> None:
        if self._speculative_finalize_task is not None and not self._speculative_finalize_task.done():
            self._speculative_finalize_task.cancel()
        self._speculative_finalize_task = None

    async def _speculative_finalize_after_debounce(self, extension_count: int = 0) -> None:
        """Fires when no new fragment has extended the current turn for
        `_SPECULATIVE_FINALIZE_DEBOUNCE_SECONDS` after it already scored
        as a strong, complete prompt — finalizes the turn early rather
        than waiting on a VAD silence endpoint that may be delayed or may
        never arrive. Safe against races with the normal VAD-driven path:
        `TurnAssembler.flush()` returns `None` if the buffer was already
        consumed there first, in which case this is a no-op.

        Section 19: a compound question ("...bottleneck, and how...")
        often has a natural pause right at the internal conjunction —
        long enough to trip this debounce, short enough that it is not a
        real VAD silence endpoint. If the buffered text still looks
        structurally incomplete (dangling "and"/"or"/comma/etc.), this
        does not finalize — it waits one more debounce window instead,
        bounded (`_MAX_SPECULATIVE_FINALIZE_EXTENSIONS`) so a turn that
        genuinely trails off mid-sentence still finalizes eventually
        rather than waiting forever (a real VAD boundary/session-end
        flush remains the ultimate backstop regardless)."""
        try:
            await asyncio.sleep(_SPECULATIVE_FINALIZE_DEBOUNCE_SECONDS)
        except asyncio.CancelledError:
            return

        pending_text = self._turn_assembler.peek_pending_text()
        if (
            pending_text
            and looks_like_incomplete_sentence(pending_text)
            and extension_count < _MAX_SPECULATIVE_FINALIZE_EXTENSIONS
        ):
            self._speculative_finalize_task = asyncio.create_task(
                self._speculative_finalize_after_debounce(extension_count=extension_count + 1)
            )
            return

        finalized_turn = self._turn_assembler.flush()
        if finalized_turn is not None:
            await self._process_finalized_turn(finalized_turn)

    async def _process_finalized_turn(self, turn_text: str) -> bool:
        """Returns `True` if this finalized turn started, is queued
        behind, or is covered by an active answer generation (so callers
        like `close()` know not to immediately cancel it again right
        after)."""
        stripped_turn_text = turn_text.strip()
        # Section 17: two independent finalize paths (a real VAD boundary
        # and the speculative-finalize debounce timer) can race for the
        # same buffered text — `TurnAssembler` normally prevents this by
        # returning `None` once consumed, but this is a cheap, explicit
        # guard against ever producing two answers for one spoken turn.
        # Scoped to only the *immediately preceding* turn so a question
        # genuinely repeated minutes later is never mistaken for a
        # duplicate.
        if stripped_turn_text and stripped_turn_text == self._last_finalized_turn_text:
            return False
        self._last_finalized_turn_text = stripped_turn_text or self._last_finalized_turn_text

        # Section 19: a turn assembled entirely from noise/non-speech
        # markers or ASR garbage (e.g. Whisper hallucinating on
        # near-silent audio) is never remembered as context, classified,
        # or allowed to create a question/queue entry/answer — rejected
        # before it can contaminate anything downstream.
        turn_quality = classify_turn_quality(turn_text)
        if turn_quality != TranscriptRejectionReason.NONE:
            if self._current_turn_speculative_question_id is not None:
                await self.cancel_active_answer()
                self._current_turn_speculative_question_id = None
            self._candidate_tracker.on_reject()
            await self._emit_event(
                "transcript.rejected", events.transcript_rejected(session_id=self.session_id, reason=turn_quality.value)
            )
            return False

        # Section 19: snapshot *before* remembering this turn's own text —
        # this is "everything genuinely prior to this turn," captured at
        # the moment it finalizes rather than lazily when generation
        # actually starts. If this turn ends up queued behind another
        # answer, other turns may finalize (and be remembered) in the
        # meantime; without this snapshot, this turn's eventual prompt
        # would silently absorb their unrelated text too.
        recent_conversation_snapshot = self._recent_conversation_block(exclude_current_question=False)
        self._remember_transcript(turn_text)

        # Only announce "classifying" when the (slower) semantic stage is
        # actually about to run — a clear deterministic verdict is fast
        # enough that a transient UI state would just flicker.
        detected_score = self._detector.score(turn_text)
        will_use_semantic_stage = self._llm_provider is not None and LOW_CONFIDENCE_REJECT_BOUND <= detected_score < self._detector.confidence_threshold
        if will_use_semantic_stage:
            await self._emit_event("question.classifying", events.question_classifying(session_id=self.session_id))

        classification = await classify_turn(turn_text, self._detector, self._llm_provider)
        if not classification.is_answer_request:
            # A speculative draft may already be streaming for what just
            # turned out not to be a real answer request — never leave it
            # visible/running. Safe to cancel unconditionally: a
            # speculative draft only ever starts when nothing else is
            # active (see `_advance_candidate_tracker`), so this can only
            # be cancelling this same rejected turn's own draft, never a
            # different, still-wanted answer.
            if self._current_turn_speculative_question_id is not None:
                cancelled_question_id = self._current_turn_speculative_question_id
                cancelled_sequence = self._sequence
                await self.cancel_active_answer()
                await self._emit_event(
                    "answer.cancelled",
                    events.answer_cancelled(session_id=self.session_id, question_id=cancelled_question_id, sequence=cancelled_sequence),
                )
                self._current_turn_speculative_question_id = None
            self._candidate_tracker.on_reject()
            if will_use_semantic_stage:
                await self._emit_event("question.rejected", events.question_rejected(session_id=self.session_id))
            return False

        stabilized_at = time.time()
        question_text = classification.normalized_question or turn_text
        decision = self._candidate_tracker.on_finalize(turn_text)
        had_active_draft = self._current_turn_speculative_question_id is not None
        question_id = self._current_turn_speculative_question_id if had_active_draft else str(uuid.uuid4())
        self._current_turn_speculative_question_id = None

        await self._emit_event(
            "question.finalized",
            events.question_finalized(
                session_id=self.session_id, question_id=question_id, text=question_text, confidence=classification.confidence,
            ),
        )
        await self._emit_event(
            "question.detected",
            events.question_detected(
                session_id=self.session_id,
                question_id=question_id,
                text=question_text,
                confidence=classification.confidence,
                detected_at=time.time(),
            ),
        )

        if not decision.start_or_replace_draft:
            # An already-active draft's text exactly matches the finalized
            # text — nothing to regenerate; let it keep streaming to its
            # own natural `answer.completed`.
            return had_active_draft

        if had_active_draft:
            # `emit_draft_marker`/`is_replace` True: this supersedes a
            # still-active speculative draft for the SAME turn — no
            # competing answer, always safe to cancel-and-replace.
            await self._start_answer_generation(
                question_id=question_id, question_text=question_text,
                emit_draft_marker=True, is_replace=True, is_draft_stream=False, stabilized_at=stabilized_at,
                recent_conversation_block=recent_conversation_snapshot,
            )
            return True

        if self._active_answer_task is not None:
            # A genuinely different, still-generating answer is active —
            # the fix for "a newer question cancels the still-running
            # answer": queue this turn instead, preserving order.
            await self._enqueue_turn(
                question_id=question_id, question_text=question_text, stabilized_at=stabilized_at,
                recent_conversation_block=recent_conversation_snapshot,
            )
            return True

        await self._start_answer_generation(
            question_id=question_id, question_text=question_text,
            emit_draft_marker=False, is_replace=False, is_draft_stream=False, stabilized_at=stabilized_at,
            recent_conversation_block=recent_conversation_snapshot,
        )
        return True

    async def _enqueue_turn(
        self, question_id: str, question_text: str, stabilized_at: Optional[float] = None,
        recent_conversation_block: str = "",
    ) -> None:
        if len(self._answer_queue) >= _MAX_QUEUED_TURNS:
            await self._emit_event(
                "answer.queue_overflow",
                events.answer_queue_overflow(session_id=self.session_id, question_id=question_id, text=question_text),
            )
            return
        self._answer_queue.append(
            _QueuedTurn(
                question_id=question_id, question_text=question_text, stabilized_at=stabilized_at or time.time(),
                recent_conversation_block=recent_conversation_block,
            )
        )
        await self._emit_event(
            "answer.queued",
            events.answer_queued(
                session_id=self.session_id, question_id=question_id, text=question_text,
                queue_position=len(self._answer_queue), queue_depth=len(self._answer_queue),
            ),
        )

    async def _start_next_queued_turn_if_any(self) -> None:
        if not self._answer_queue:
            return
        next_turn = self._answer_queue.pop(0)
        await self._emit_event(
            "answer.dequeued",
            events.answer_dequeued(session_id=self.session_id, question_id=next_turn.question_id, queue_depth=len(self._answer_queue)),
        )
        await self._start_answer_generation(
            question_id=next_turn.question_id, question_text=next_turn.question_text,
            emit_draft_marker=False, is_replace=False, is_draft_stream=False, stabilized_at=next_turn.stabilized_at,
            recent_conversation_block=next_turn.recent_conversation_block,
        )

    async def retry_failed_answer(self, question_id: str, question_text: str) -> None:
        """Explicit, user-initiated retry after a failed/timed-out
        generation (Section 17) — re-runs generation for the same
        already-classified question through the normal start-or-queue
        path, never bypassing the "one active generation" rule."""
        if self._active_answer_task is not None:
            await self._enqueue_turn(question_id=question_id, question_text=question_text)
            return
        await self._start_answer_generation(
            question_id=question_id, question_text=question_text,
            emit_draft_marker=False, is_replace=False, is_draft_stream=False,
        )

    async def skip_active_answer(self) -> None:
        """Explicit, user-initiated "Skip current answer" — never
        triggered automatically. Cancels whatever is actively generating
        and immediately starts the next queued turn, if any, rather than
        leaving the queue stalled behind an answer the user no longer
        wants to wait for."""
        await self.cancel_active_answer()
        self._current_turn_speculative_question_id = None
        await self._start_next_queued_turn_if_any()

    def _remember_transcript(self, text: str) -> None:
        self._recent_transcript_fragments.append(text.strip())
        while sum(len(item) + 1 for item in self._recent_transcript_fragments) > _MAX_RECENT_TRANSCRIPT_CHARACTERS:
            self._recent_transcript_fragments.pop(0)

    def _recent_conversation_block(self, exclude_current_question: bool) -> str:
        # `exclude_current_question=True` (the finalize-triggered path):
        # `_remember_transcript` was already called for the current
        # question's text moments ago, so the final fragment *is* the
        # question itself and must be excluded — it's sent in a dedicated
        # field below, and repeating it here would be redundant.
        # `exclude_current_question=False` (the pre-finalize speculative
        # draft path): the current candidate text hasn't been remembered
        # yet at all, so every fragment here is genuinely prior context —
        # slicing off the last one would silently drop real context.
        fragments = self._recent_transcript_fragments[:-1] if exclude_current_question else self._recent_transcript_fragments
        return "\n".join(fragments)

    async def _start_answer_generation(
        self, question_id: str, question_text: str, *, emit_draft_marker: bool = False, is_replace: bool = False,
        is_draft_stream: bool = False, exclude_current_question_from_context: bool = True,
        stabilized_at: Optional[float] = None, recent_conversation_block: Optional[str] = None,
    ) -> None:
        # Section 19: `recent_conversation_block`, when given, is a
        # snapshot taken at *finalize* time (see `_process_finalized_turn`)
        # — required for a turn that gets queued, since by the time a
        # queued turn actually starts generating, other unrelated turns
        # may have finalized (and been remembered) in between; computing
        # this lazily at generation-start time would silently pull their
        # text into this turn's prompt. `None` means "compute it now" —
        # only ever safe for the speculative-draft path, which always
        # starts synchronously with nothing else active/queued.
        if recent_conversation_block is None:
            recent_conversation_block = self._recent_conversation_block(exclude_current_question_from_context)
        # Callers only ever invoke this when cancelling is either correct
        # (replacing a speculative draft for this SAME turn) or a no-op
        # (nothing else active) — a genuinely different, still-generating
        # answer is queued by the caller instead of ever reaching here.
        await self.cancel_active_answer()

        self._sequence += 1
        sequence = self._sequence
        generation_request_start = time.time()

        if emit_draft_marker:
            marker_name = "answer.draft_replaced" if is_replace else "answer.draft_started"
            marker_builder = events.answer_draft_replaced if is_replace else events.answer_draft_started
            await self._emit_event(marker_name, marker_builder(session_id=self.session_id, question_id=question_id, sequence=sequence))

        retrieved: list[RetrievedChunk] = []
        if self._retriever is not None:
            try:
                retrieved = await asyncio.wait_for(
                    self._retriever.retrieve(self.session_id, question_text), timeout=_RETRIEVAL_TIMEOUT_SECONDS
                )
            except asyncio.TimeoutError:
                logger.error("Retrieval timed out after %.1fs; proceeding without document context", _RETRIEVAL_TIMEOUT_SECONDS)
                retrieved = []
            except Exception as exc:  # noqa: BLE001 - retrieval failing must never block answer generation
                logger.error("Unhandled %s during retrieval; proceeding without document context", type(exc).__name__)
                retrieved = []

        document_context_block = self._retriever.build_context_block(retrieved) if retrieved and self._retriever else ""
        memory_context_block = "\n".join(f"- {text}" for text in self._memory_texts)
        prompt = render_prompt(
            self._session_context,
            question_text,
            document_context_block=document_context_block,
            memory_context_block=memory_context_block,
            recent_conversation_block=recent_conversation_block,
            user_answer_block=self._recent_user_answer_text or "",
        )

        # Section 19: everything the answer is actually allowed to draw
        # numeric claims from — used only by the post-generation
        # groundedness guard below, never sent to the model itself
        # (that's `prompt`, built separately above).
        grounding_text = "\n".join(
            block for block in (document_context_block, memory_context_block, self._recent_user_answer_text or "", question_text) if block
        )

        self._active_answer_task = asyncio.create_task(
            self._run_answer_generation(
                sequence=sequence,
                question_id=question_id,
                question_text=question_text,
                prompt=prompt,
                retrieved=retrieved,
                is_draft_stream=is_draft_stream,
                stabilized_at=stabilized_at or generation_request_start,
                generation_request_start=generation_request_start,
                grounding_text=grounding_text,
            )
        )

    async def _run_answer_generation(
        self, sequence: int, question_id: str, question_text: str, prompt: str, retrieved: list[RetrievedChunk],
        is_draft_stream: bool = False, stabilized_at: float = 0.0, generation_request_start: float = 0.0,
        grounding_text: str = "",
    ) -> None:
        await self._emit_event(
            "answer.started",
            events.answer_started(session_id=self.session_id, sequence=sequence, question_id=question_id),
        )

        first_raw_token_at: list[float] = []
        first_speakable_char_at: list[float] = []

        async def on_raw_delta(delta: str) -> None:
            # Diagnostics timing only — raw content (which may include
            # hidden reasoning) is never emitted over the wire, logged,
            # or persisted. See `SpeakableAnswerStream`/`generate_answer`.
            if not first_raw_token_at:
                first_raw_token_at.append(time.time())

        async def on_speakable_delta(delta: str) -> None:
            if not first_speakable_char_at:
                first_speakable_char_at.append(time.time())
                warning_task.cancel()
            await self._emit_event(
                "answer.speakable_delta",
                events.answer_speakable_delta(session_id=self.session_id, sequence=sequence, question_id=question_id, delta=delta),
            )
            if is_draft_stream:
                await self._emit_event(
                    "answer.speakable_draft_delta",
                    events.answer_speakable_draft_delta(session_id=self.session_id, sequence=sequence, question_id=question_id, delta=delta),
                )

        async def warn_if_slow() -> None:
            try:
                await asyncio.sleep(_FIRST_USABLE_TEXT_WARNING_SECONDS)
            except asyncio.CancelledError:
                return
            await self._emit_event(
                "answer.slow_warning",
                events.answer_slow_warning(session_id=self.session_id, sequence=sequence, question_id=question_id),
            )

        warning_task = asyncio.create_task(warn_if_slow())

        try:
            parsed = await generate_answer(
                self._llm_provider, prompt, on_speakable_delta=on_speakable_delta, on_raw_delta=on_raw_delta
            )
        except asyncio.CancelledError:
            # Superseded by a same-turn replace (see `_start_answer_generation`)
            # — the caller that cancelled us already owns `_active_answer_task`
            # and any dequeue decision; this task must never touch either.
            warning_task.cancel()
            raise
        except LLMError as exc:
            warning_task.cancel()
            logger.error("Unhandled %s during answer generation", type(exc).__name__)
            await self._emit_generation_failed(sequence=sequence, question_id=question_id, question_text=question_text)
            await self._finish_active_generation()
            return
        except Exception as exc:  # noqa: BLE001 - never let a raw exception escape, never log its message
            warning_task.cancel()
            logger.error("Unhandled %s during answer generation", type(exc).__name__)
            await self._emit_generation_failed(sequence=sequence, question_id=question_id, question_text=question_text)
            await self._finish_active_generation()
            return

        warning_task.cancel()

        if not parsed.short_answer and not parsed.talking_points:
            # The model produced no usable speakable text at all (e.g. an
            # unclosed reasoning block swallowed everything, or a
            # provider that returned only whitespace) — an honest,
            # retryable failure, never a blank/empty "completed" answer.
            logger.error("Generation completed with no usable answer text")
            await self._emit_generation_failed(
                sequence=sequence, question_id=question_id, question_text=question_text, message=_NO_USABLE_TEXT_MESSAGE
            )
            await self._finish_active_generation()
            return

        # Section 19: a structured groundedness guard — never claim a
        # numeric before/after change where both values are identical,
        # and never state a specific percentage that appears nowhere in
        # the real context this answer was allowed to draw from. Swaps
        # in an honest "I don't have enough verified context" answer
        # rather than ever showing a fabricated figure. Talking points
        # are dropped too — they're generated from the same ungrounded
        # pass and can't be trusted independently of the answer they
        # were meant to support.
        groundedness = check_answer_groundedness(parsed.short_answer, grounding_text)
        if not groundedness.is_grounded:
            logger.error("Answer failed groundedness check (%s); substituting a safe response", groundedness.reason)
            parsed = ParsedAnswer(short_answer=safe_fallback_answer(), talking_points=[], caveat=parsed.caveat)

        completed_at = time.time()
        await self._emit_event(
            "answer.completed",
            events.answer_completed(
                session_id=self.session_id,
                sequence=sequence,
                question_id=question_id,
                question=question_text,
                answer_text=parsed.short_answer,
                talking_points=parsed.talking_points,
                sources=chunk_sources(retrieved),
                caveat=parsed.caveat,
            ),
        )
        if self._emit_timing_diagnostics:
            await self._emit_event(
                "answer.timing",
                events.answer_timing(
                    session_id=self.session_id, sequence=sequence, question_id=question_id,
                    stabilized_at=stabilized_at, generation_request_start=generation_request_start,
                    first_raw_token_at=first_raw_token_at[0] if first_raw_token_at else None,
                    first_speakable_char_at=first_speakable_char_at[0] if first_speakable_char_at else None,
                    completed_at=completed_at,
                ),
            )
        await self._finish_active_generation()

    async def _finish_active_generation(self) -> None:
        """Called at the true end of a generation round that reached
        `answer.completed` (success or handled failure) — never on
        cancellation, whose caller already owns this bookkeeping. Frees
        the "one active generation" slot and immediately starts the next
        queued turn, if any, so a burst of questions is worked through in
        order rather than needing a fresh trigger."""
        self._active_answer_task = None
        await self._start_next_queued_turn_if_any()

    async def _emit_generation_failed(
        self, sequence: int, question_id: str, question_text: str, message: str = _GENERATION_FAILED_MESSAGE
    ) -> None:
        # `answer.completed` (with `is_failed=True`) is still the one way
        # a generation round always ends, success or not, so streaming
        # Swift state never hangs on "Generating answer…" forever — but
        # `is_failed` lets Swift tell a real answer apart from this status
        # note and preserve whatever completed answer was already
        # visible, rather than overwriting it with a failure message.
        await self._emit_event(
            "answer.completed",
            events.answer_completed(
                session_id=self.session_id,
                sequence=sequence,
                question_id=question_id,
                question=question_text,
                answer_text=message,
                talking_points=[],
                sources=[],
                caveat="",
                is_failed=True,
            ),
        )

    async def cancel_active_answer(self) -> None:
        task = self._active_answer_task
        self._active_answer_task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def close(self) -> None:
        self._cancel_speculative_finalize_timer()

        # `dispatcher.py`'s `close_transcription_session_if_running` calls
        # `TranscriptionSession.close()` *before* this — its own trailing-
        # audio flush + VAD force-finalize can call back into
        # `handle_turn_boundary` while this orchestrator is still fully
        # open, possibly already starting an answer generation for
        # whatever was spoken right at session end. This flush then covers
        # anything from *completed* windows that hadn't reached a turn
        # boundary yet (rare once the above already ran, but harmless if
        # it finds nothing).
        finalized_turn = self._turn_assembler.flush()
        if finalized_turn is not None:
            await self._process_finalized_turn(finalized_turn)

        # A trailing user answer never triggers generation, but still
        # deserves to be captured as context rather than silently dropped
        # if the session ends mid-answer.
        finalized_user_turn = self._user_turn_assembler.flush()
        if finalized_user_turn is not None:
            self._recent_user_answer_text = finalized_user_turn
            self._remember_transcript(finalized_user_turn)

        # Any still-queued turns are dropped, not worked through — a
        # session ending is a deliberate stop, not a pause. Cleared before
        # the wait below so the active task's own completion callback
        # (`_finish_active_generation`) finds nothing to dequeue and
        # doesn't start yet another generation during shutdown.
        self._answer_queue.clear()

        # Whatever is active at this point — started by the flush above,
        # by `TranscriptionSession.close()`'s own callback moments ago, or
        # simply still streaming from an earlier question — gets a
        # bounded chance to actually finish and deliver its events,
        # rather than being unconditionally cancelled. A session ending
        # moments after the last question was asked must not silently
        # drop that answer; a session ending mid-generation with no
        # bound would otherwise make "End Session" feel hung.
        task = self._active_answer_task
        if task is None or task.done():
            return
        try:
            await asyncio.wait_for(task, timeout=_FINAL_ANSWER_SHUTDOWN_TIMEOUT_SECONDS)
        except Exception:  # noqa: BLE001 - timeout (task is cancelled by wait_for itself) or a generation error already handled/logged inside the task
            pass
        finally:
            self._active_answer_task = None
