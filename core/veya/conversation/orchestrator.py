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

    @property
    def answer_intelligence_available(self) -> bool:
        return self._llm_provider is not None

    async def handle_final_transcript(self, text: str, started_at: float, ended_at: float) -> None:
        """Called for real, deduplicated final transcript text only —
        never for partials. A no-op session-wide if no LLM provider is
        available: detecting questions nobody can answer would just be a
        confusing dead-end UI state (see
        docs/QUESTION_AND_ANSWER_INTELLIGENCE.md's fallback behavior).
        Feeds the fragment into the turn assembler — a turn only reaches
        classification once a real endpoint (VAD boundary/session stop/
        max duration) finalizes it, never on every individual fragment."""
        if not self.answer_intelligence_available:
            return

        finalized_turn = self._turn_assembler.add_fragment(text, started_at, ended_at)
        if finalized_turn is not None:
            await self._process_finalized_turn(finalized_turn)

    async def handle_turn_boundary(self, boundary_time: float) -> None:
        """Called by `TranscriptionSession` when local VAD detects a turn
        has ended at audio-timeline position `boundary_time`. If the
        fragment covering that boundary hasn't arrived yet (Whisper
        transcription lags real-time), this only records the boundary —
        `handle_final_transcript` finalizes once that fragment shows up."""
        if not self.answer_intelligence_available:
            return
        finalized_turn = self._turn_assembler.request_finalize_at(boundary_time)
        if finalized_turn is not None:
            await self._process_finalized_turn(finalized_turn)

    async def _process_finalized_turn(self, turn_text: str) -> bool:
        """Returns `True` if this finalized turn started a fresh answer
        generation (so callers like `close()` know not to immediately
        cancel it again right after)."""
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
            if will_use_semantic_stage:
                await self._emit_event("question.rejected", events.question_rejected(session_id=self.session_id))
            return False

        question_text = classification.normalized_question or turn_text
        question_id = str(uuid.uuid4())
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

        await self._start_answer_generation(question_id=question_id, question_text=question_text)
        return True

    def _remember_transcript(self, text: str) -> None:
        self._recent_transcript_fragments.append(text.strip())
        while sum(len(item) + 1 for item in self._recent_transcript_fragments) > _MAX_RECENT_TRANSCRIPT_CHARACTERS:
            self._recent_transcript_fragments.pop(0)

    def _recent_conversation_block(self) -> str:
        # The final fragment is the question itself, which is sent in a
        # dedicated field below. The preceding speech is useful context; it
        # also avoids needlessly repeating the question in the prompt.
        return "\n".join(self._recent_transcript_fragments[:-1])

    async def _start_answer_generation(self, question_id: str, question_text: str) -> None:
        # A new question always supersedes whatever answer was still
        # generating — only one active generation per session at a time.
        await self.cancel_active_answer()

        self._sequence += 1
        sequence = self._sequence

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
            recent_conversation_block=self._recent_conversation_block(),
        )

        self._active_answer_task = asyncio.create_task(
            self._run_answer_generation(
                sequence=sequence,
                question_id=question_id,
                question_text=question_text,
                prompt=prompt,
                retrieved=retrieved,
            )
        )

    async def _run_answer_generation(
        self, sequence: int, question_id: str, question_text: str, prompt: str, retrieved: list[RetrievedChunk]
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
                talking_points=[_GENERATION_FAILED_MESSAGE],
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
