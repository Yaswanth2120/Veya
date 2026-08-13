"""Drives one Live Session's real transcription using a genuine
incremental `StreamingASRProvider` (see `streaming_provider.py`) instead
of `TranscriptionSession`'s fixed-window batch-CLI loop.

Exposes the same public surface `dispatcher.py` already depends on
(`session_id`, `handle_chunk`, `close`) so it's a drop-in alternative —
`dispatcher.py` picks whichever this-or-`TranscriptionSession` construction
succeeds (see `_handle_transcription_start`), never both at once.

Local VAD still runs here, independently of the ASR provider, purely for
turn-boundary detection (`turn.state`/`on_turn_boundary`) — the streaming
provider's own partial/final hypotheses are what drive
`transcript.partial`/`transcript.final`, replacing both the old rolling
4-second window *and* the separate partial-preview re-transcription hack
`TranscriptionSession` needed to approximate the same experience.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Awaitable, Callable, Optional

from .streaming_provider import StreamingASRProvider
from .turn_detection import TurnDetectionConfig, TurnSignal, VoiceActivityDetector
from ..ipc import events
from ..ipc.errors import ErrorCode, ProtocolError

logger = logging.getLogger("veya.transcription.streaming_session")

EmitEvent = Callable[[str, dict], Awaitable[None]]


class StreamingTranscriptionSession:
    def __init__(
        self,
        session_id: str,
        sample_rate_hz: int,
        streaming_provider: StreamingASRProvider,
        emit_event: EmitEvent,
        on_final_transcript: Optional[Callable[[str, float, float], Awaitable[None]]] = None,
        on_turn_boundary: Optional[Callable[[float], Awaitable[None]]] = None,
        on_partial_transcript: Optional[Callable[[str, float], Awaitable[None]]] = None,
        vad: Optional[VoiceActivityDetector] = None,
        emit_vad_diagnostics: bool = False,
        source: str = "mixed",
    ) -> None:
        self.session_id = session_id
        self._sample_rate_hz = sample_rate_hz
        self._provider = streaming_provider
        self._emit_event = emit_event
        # Section 16: which physical audio track this session represents
        # — "mixed" (single-track, unchanged default) or a separated-
        # track source, stamped onto every `transcript.partial`/`.final`
        # event this session emits.
        self._source = source
        self._on_final_transcript = on_final_transcript
        self._on_turn_boundary = on_turn_boundary
        # Section 15B: called for every *meaningful* (non-empty,
        # different-from-last) partial hypothesis — real speculative
        # question-candidate/draft tracking is driven from here, not
        # only from `transcript.final`. Never fed into the finalized-turn
        # assembler (partials are never final transcript) and never
        # persisted.
        self._on_partial_transcript = on_partial_transcript
        self._last_partial_text = ""
        self._vad = vad or VoiceActivityDetector(TurnDetectionConfig(sample_rate_hz=sample_rate_hz))
        self._emit_vad_diagnostics = emit_vad_diagnostics
        self._last_chunk_end_time = 0.0
        self._last_final_end_time = 0.0
        self._last_sequence: Optional[int] = None
        self._started = False
        self._consumer_task: Optional[asyncio.Task] = None

    @property
    def is_degraded(self) -> bool:
        return self._provider.is_degraded

    def set_source(self, source: str) -> None:
        """Section 16: called when the meeting-audio track starts partway
        through an already-running microphone session, so this session's
        wire-tagged source updates from "mixed" to "microphone" from that
        point on."""
        self._source = source

    def _validate_sequence(self, sequence: int) -> None:
        if self._last_sequence is not None and sequence <= self._last_sequence:
            raise ProtocolError(
                ErrorCode.INVALID_PARAMS,
                f"Out-of-order or duplicate audio chunk sequence: {sequence}.",
            )
        self._last_sequence = sequence

    async def _ensure_started(self) -> None:
        if self._started:
            return
        self._started = True
        await self._provider.start()
        self._consumer_task = asyncio.create_task(self._consume_hypotheses())

    async def handle_chunk(self, sequence: int, started_at: float, duration: float, pcm: bytes) -> None:
        self._validate_sequence(sequence)
        await self._ensure_started()
        self._last_chunk_end_time = started_at + duration

        signal = self._vad.process_chunk(pcm, duration)
        await self._process_turn_signal(signal)
        if self._emit_vad_diagnostics:
            await self._emit_event(
                "turn.debug",
                events.turn_debug(
                    session_id=self.session_id,
                    rms=self._vad.last_rms,
                    threshold=self._vad.speech_rms_threshold,
                    is_in_speech=self._vad.is_in_speech,
                    speech_seconds=self._vad.speech_seconds,
                    silence_seconds=self._vad.silence_seconds,
                ),
            )

        await self._provider.feed_pcm(pcm)

    async def _process_turn_signal(self, signal: TurnSignal) -> None:
        if signal == TurnSignal.NONE:
            return
        try:
            if signal == TurnSignal.SPEECH_STARTED:
                await self._emit_event("turn.state", events.turn_state(session_id=self.session_id, state="speech"))
            elif signal == TurnSignal.SILENCE_CANDIDATE:
                await self._emit_event("turn.state", events.turn_state(session_id=self.session_id, state="waiting_for_silence"))
            elif signal == TurnSignal.TURN_FINALIZED:
                await self._emit_event("turn.state", events.turn_state(session_id=self.session_id, state="listening"))
                if self._on_turn_boundary is not None:
                    await self._on_turn_boundary(self._last_chunk_end_time)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a turn-detection failure must never break transcription
            logger.error("Unhandled %s while processing a turn signal", type(exc).__name__)

    async def _consume_hypotheses(self) -> None:
        try:
            async for hypothesis in self._provider.hypotheses():
                await self._handle_hypothesis(hypothesis)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - never let a consumer failure crash the worker
            logger.error("Unhandled %s consuming streaming ASR hypotheses", type(exc).__name__)

    async def _handle_hypothesis(self, hypothesis) -> None:  # ASRHypothesis, avoiding an import cycle in the signature
        text = hypothesis.text.strip()
        if not text:
            return

        if not hypothesis.is_final:
            await self._emit_event(
                "transcript.partial", events.transcript_partial(session_id=self.session_id, text=text, source=self._source),
            )
            if text != self._last_partial_text:
                self._last_partial_text = text
                if self._on_partial_transcript is not None:
                    try:
                        await self._on_partial_transcript(text, self._last_chunk_end_time)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:  # noqa: BLE001 - speculative drafting must never break transcription
                        logger.error("Unhandled %s in on_partial_transcript callback", type(exc).__name__)
            return

        # Real timestamps, approximated from cumulative fed audio (the
        # streaming engine's own hypotheses don't carry precise
        # per-fragment timing) — monotonic and non-overlapping, which is
        # all `TurnAssembler`'s boundary comparisons actually need.
        started_at = self._last_final_end_time
        ended_at = max(self._last_chunk_end_time, started_at)
        self._last_final_end_time = ended_at
        self._last_partial_text = ""

        await self._emit_event(
            "transcript.final",
            events.transcript_final(
                session_id=self.session_id, segment_id=str(uuid.uuid4()), text=text, started_at=started_at, ended_at=ended_at,
                source=self._source,
            ),
        )

        if self._on_final_transcript is not None:
            try:
                await self._on_final_transcript(text, started_at, ended_at)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - question detection/answer generation must never break transcription
                logger.error("Unhandled %s in on_final_transcript callback", type(exc).__name__)

    async def close(self) -> None:
        if not self._started:
            return
        await self._provider.stop()
        if self._consumer_task is not None and not self._consumer_task.done():
            try:
                await asyncio.wait_for(self._consumer_task, timeout=10.0)
            except asyncio.TimeoutError:
                self._consumer_task.cancel()

        finalize_signal = self._vad.force_finalize()
        if finalize_signal is not None:
            await self._process_turn_signal(finalize_signal)
