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
from typing import Awaitable, Callable, Optional

from .answer_generation import generate_answer
from .context_builder import render_prompt
from .models import SessionContext
from .question_candidate_tracker import CandidateState, QuestionCandidateTracker
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
        # The question_id of whatever draft/generation is currently
        # active, if any — lets a finalize-triggered regeneration reuse
        # the same id (so Swift can recognize "this is the same evolving
        # question, not a new one") instead of minting a fresh one.
        self._draft_question_id: Optional[str] = None
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
        decision = self._candidate_tracker.on_pending_text_changed(stripped)

        if decision.emit_candidate:
            await self._emit_event("question.candidate", events.question_candidate(session_id=self.session_id, text=decision.text))
        elif decision.emit_updated:
            await self._emit_event("question.updated", events.question_updated(session_id=self.session_id, text=decision.text))

        if decision.start_or_replace_draft:
            had_active_draft = self._draft_question_id is not None
            question_id = self._draft_question_id if (decision.is_replace and had_active_draft) else str(uuid.uuid4())
            await self._start_answer_generation(
                question_id=question_id, question_text=decision.text,
                emit_draft_marker=True, is_replace=decision.is_replace, is_draft_stream=True,
                exclude_current_question_from_context=False,
            )

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

    async def _speculative_finalize_after_debounce(self) -> None:
        """Fires when no new fragment has extended the current turn for
        `_SPECULATIVE_FINALIZE_DEBOUNCE_SECONDS` after it already scored
        as a strong, complete prompt — finalizes the turn early rather
        than waiting on a VAD silence endpoint that may be delayed or may
        never arrive. Safe against races with the normal VAD-driven path:
        `TurnAssembler.flush()` returns `None` if the buffer was already
        consumed there first, in which case this is a no-op."""
        try:
            await asyncio.sleep(_SPECULATIVE_FINALIZE_DEBOUNCE_SECONDS)
        except asyncio.CancelledError:
            return
        finalized_turn = self._turn_assembler.flush()
        if finalized_turn is not None:
            await self._process_finalized_turn(finalized_turn)

    async def _process_finalized_turn(self, turn_text: str) -> bool:
        """Returns `True` if this finalized turn started or is covered by
        an active answer generation (so callers like `close()` know not
        to immediately cancel it again right after)."""
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
            # visible/running.
            if self._draft_question_id is not None:
                cancelled_question_id = self._draft_question_id
                cancelled_sequence = self._sequence
                await self.cancel_active_answer()
                await self._emit_event(
                    "answer.cancelled",
                    events.answer_cancelled(session_id=self.session_id, question_id=cancelled_question_id, sequence=cancelled_sequence),
                )
            self._candidate_tracker.on_reject()
            if will_use_semantic_stage:
                await self._emit_event("question.rejected", events.question_rejected(session_id=self.session_id))
            return False

        question_text = classification.normalized_question or turn_text
        decision = self._candidate_tracker.on_finalize(turn_text)
        had_active_draft = self._draft_question_id is not None
        question_id = self._draft_question_id if had_active_draft else str(uuid.uuid4())

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

        if decision.start_or_replace_draft:
            # `emit_draft_marker`/`is_draft_stream` are False here: once a
            # turn is finalized, this is the definitive generation — no
            # further refinement is expected, so it streams as a plain
            # `answer.delta`, not `answer.draft_delta`. `answer.draft_replaced`
            # still fires when this supersedes a still-active speculative
            # draft, so Swift knows to discard that draft's stale content.
            await self._start_answer_generation(
                question_id=question_id, question_text=question_text,
                emit_draft_marker=had_active_draft, is_replace=had_active_draft, is_draft_stream=False,
            )
            return True

        # An already-active draft's text exactly matches the finalized
        # text — nothing to regenerate; let it keep streaming to its own
        # natural `answer.completed`.
        return had_active_draft

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
    ) -> None:
        # A new question always supersedes whatever answer was still
        # generating — only one active generation per session at a time.
        await self.cancel_active_answer()
        self._draft_question_id = question_id

        self._sequence += 1
        sequence = self._sequence

        if emit_draft_marker:
            marker_name = "answer.draft_replaced" if is_replace else "answer.draft_started"
            marker_builder = events.answer_draft_replaced if is_replace else events.answer_draft_started
            await self._emit_event(marker_name, marker_builder(session_id=self.session_id, question_id=question_id, sequence=sequence))

        retrieved: list[RetrievedChunk] = []
        if self._retriever is not None:
            try:
                retrieved = await self._retriever.retrieve(self.session_id, question_text)
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
            recent_conversation_block=self._recent_conversation_block(exclude_current_question_from_context),
            user_answer_block=self._recent_user_answer_text or "",
        )

        self._active_answer_task = asyncio.create_task(
            self._run_answer_generation(
                sequence=sequence,
                question_id=question_id,
                question_text=question_text,
                prompt=prompt,
                retrieved=retrieved,
                is_draft_stream=is_draft_stream,
            )
        )

    async def _run_answer_generation(
        self, sequence: int, question_id: str, question_text: str, prompt: str, retrieved: list[RetrievedChunk], is_draft_stream: bool = False,
    ) -> None:
        await self._emit_event(
            "answer.started",
            events.answer_started(session_id=self.session_id, sequence=sequence, question_id=question_id),
        )

        async def on_delta(delta: str) -> None:
            await self._emit_event(
                "answer.delta",
                events.answer_delta(
                    session_id=self.session_id, sequence=sequence, question_id=question_id, delta=delta
                ),
            )
            if is_draft_stream:
                await self._emit_event(
                    "answer.draft_delta",
                    events.answer_draft_delta(session_id=self.session_id, sequence=sequence, question_id=question_id, delta=delta),
                )

        try:
            parsed = await generate_answer(self._llm_provider, prompt, on_delta=on_delta)
        except asyncio.CancelledError:
            raise
        except LLMError as exc:
            logger.error("Unhandled %s during answer generation", type(exc).__name__)
            await self._emit_generation_failed(sequence=sequence, question_id=question_id, question_text=question_text)
            return
        except Exception as exc:  # noqa: BLE001 - never let a raw exception escape, never log its message
            logger.error("Unhandled %s during answer generation", type(exc).__name__)
            await self._emit_generation_failed(sequence=sequence, question_id=question_id, question_text=question_text)
            return

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

    async def _emit_generation_failed(self, sequence: int, question_id: str, question_text: str) -> None:
        # There is no dedicated failure event in this section's protocol —
        # `answer.completed` is the one way a generation round always
        # ends, success or not, so streaming Swift state never hangs on
        # "Generating answer…" forever. The message is an honest status
        # note, never a fabricated answer.
        await self._emit_event(
            "answer.completed",
            events.answer_completed(
                session_id=self.session_id,
                sequence=sequence,
                question_id=question_id,
                question=question_text,
                answer_text=_GENERATION_FAILED_MESSAGE,
                talking_points=[],
                sources=[],
                caveat="",
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
