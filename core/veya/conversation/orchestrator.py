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
from ..ipc import events
from ..knowledge.models import RetrievedChunk
from ..knowledge.retrieval import KnowledgeRetriever, chunk_sources
from ..llm.errors import LLMError
from ..llm.provider import LLMProvider

logger = logging.getLogger("veya.conversation")

EmitEvent = Callable[[str, dict], Awaitable[None]]

_GENERATION_FAILED_MESSAGE = "Answer generation failed — the local LLM provider became unavailable mid-response."


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

    @property
    def answer_intelligence_available(self) -> bool:
        return self._llm_provider is not None

    async def handle_final_transcript(self, text: str, started_at: float, ended_at: float) -> None:
        """Called for real, deduplicated final transcript text only —
        never for partials. A no-op session-wide if no LLM provider is
        available: detecting questions nobody can answer would just be a
        confusing dead-end UI state (see
        docs/QUESTION_AND_ANSWER_INTELLIGENCE.md's fallback behavior)."""
        if not self.answer_intelligence_available:
            return

        result = self._detector.detect(text)
        if result is None:
            return

        question_id = str(uuid.uuid4())
        await self._emit_event(
            "question.detected",
            events.question_detected(
                session_id=self.session_id,
                question_id=question_id,
                text=result.text,
                confidence=result.confidence,
                detected_at=time.time(),
            ),
        )

        await self._start_answer_generation(question_id=question_id, question_text=result.text)

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
        prompt = render_prompt(self._session_context, question_text, document_context_block=document_context_block, memory_context_block=memory_context_block)

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
        await self.cancel_active_answer()
