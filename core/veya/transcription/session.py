"""Orchestrates one Live Session's real transcription: buffers incoming
audio chunks into rolling windows and transcribes them off the worker's
main stdin-dispatch path.

The worker's stdin loop (`worker.py`) awaits each `Dispatcher.dispatch`
call fully before reading the next line — a real Whisper invocation can
take seconds, and blocking that loop for seconds would stall every other
in-flight RPC (health-check pings, `session.stop`, etc.), not just
transcription. So `handle_chunk` only buffers the audio and enqueues a
window for a *background* consumer task; the RPC response ("chunk
received") and the eventual `transcript.final` event are deliberately
decoupled.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from typing import Awaitable, Callable, Optional

from .engine import TranscriptionEngine
from .overlap import dedupe_overlap
from .rolling_buffer import RollingWindowBuffer, RollingWindowConfig
from .turn_detection import TurnDetectionConfig, TurnSignal, VoiceActivityDetector
from ..ipc import events
from ..ipc.errors import ErrorCode, ProtocolError

logger = logging.getLogger("veya.transcription")

EmitEvent = Callable[[str, dict], Awaitable[None]]

# whisper.cpp emits a bracketed/parenthesized tag instead of real words for
# a non-speech window — "[BLANK_AUDIO]", "(silence)", "[SILENCE]",
# "[ Music ]", etc. These are never real transcript content and must never
# reach Swift/the user-facing history as if they were — matched only when
# the *entire* stripped text is one such tag, so a real sentence that
# merely contains a bracketed aside is never dropped.
_NON_SPEECH_MARKER = re.compile(r"^[\[\(][^\]\)]*[\]\)]$")


def _is_non_speech_marker(text: str) -> bool:
    return bool(_NON_SPEECH_MARKER.fullmatch(text.strip()))


class TranscriptionSession:
    def __init__(
        self,
        session_id: str,
        sample_rate_hz: int,
        engine: TranscriptionEngine,
        emit_event: EmitEvent,
        run_blocking: Optional[Callable[[Callable[[], str]], Awaitable[str]]] = None,
        on_final_transcript: Optional[Callable[[str, float, float], Awaitable[None]]] = None,
        on_turn_boundary: Optional[Callable[[float], Awaitable[None]]] = None,
        vad: Optional[VoiceActivityDetector] = None,
    ) -> None:
        self.session_id = session_id
        self._buffer = RollingWindowBuffer(RollingWindowConfig(sample_rate_hz=sample_rate_hz))
        self._engine = engine
        self._emit_event = emit_event
        self._run_blocking = run_blocking or self._default_run_blocking
        # Section 8: called with (text, started_at, ended_at) right after
        # a `transcript.final` is actually emitted (i.e. post-dedup,
        # never for an empty/fully-deduplicated window) — this is the
        # *only* path real question detection ever sees text through, and
        # it never sees partials. Failures here must never break
        # transcription itself.
        self._on_final_transcript = on_final_transcript
        # Section 14: called with an audio-timeline boundary time whenever
        # local VAD detects a turn has ended (silence endpoint or the
        # max-turn-duration safety cap) — independent of, and usually
        # faster than, the next Whisper window completing. Failures here
        # must never break transcription either.
        self._on_turn_boundary = on_turn_boundary
        self._vad = vad or VoiceActivityDetector(TurnDetectionConfig(sample_rate_hz=sample_rate_hz))
        self._last_chunk_end_time = 0.0
        self._last_sequence: Optional[int] = None
        self._previous_text = ""
        # Bytes fed into the buffer since the last completed window (or
        # since the session began, if none has completed yet) — lets
        # `close()` tell "genuinely new, never-transcribed trailing audio"
        # apart from "just the retained overlap tail, already covered by
        # the last `transcript.final`" without reaching into the buffer's
        # internals.
        self._bytes_since_last_window = 0
        self._window_queue: "asyncio.Queue[tuple[bytes, float, float]]" = asyncio.Queue()
        self._consumer_task: asyncio.Task = asyncio.create_task(self._consume_windows())

    @staticmethod
    async def _default_run_blocking(fn: Callable[[], str]) -> str:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, fn)

    def _validate_sequence(self, sequence: int) -> None:
        if self._last_sequence is not None and sequence <= self._last_sequence:
            raise ProtocolError(
                ErrorCode.INVALID_PARAMS,
                f"Out-of-order or duplicate audio chunk sequence: {sequence}.",
            )
        self._last_sequence = sequence

    async def handle_chunk(self, sequence: int, started_at: float, duration: float, pcm: bytes) -> None:
        """Validates and buffers one chunk. Returns as soon as the chunk is
        buffered — does not wait for any resulting window to transcribe.
        Also runs local VAD on this chunk (independent of and typically
        much faster than window-based transcription) to detect turn
        boundaries in near-real-time."""
        self._validate_sequence(sequence)
        self._bytes_since_last_window += len(pcm)
        self._last_chunk_end_time = started_at + duration
        await self._process_turn_signal(self._vad.process_chunk(pcm, duration))

        window = self._buffer.add_chunk(pcm)
        if window is not None:
            self._bytes_since_last_window = 0
            await self._window_queue.put((window, started_at, duration))

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

    async def _consume_windows(self) -> None:
        while True:
            window, started_at, duration = await self._window_queue.get()
            try:
                await self._transcribe_and_emit(window, started_at, duration)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - never let a transcription failure crash the worker or leak content
                logger.error("Unhandled %s while transcribing a window", type(exc).__name__)
            finally:
                self._window_queue.task_done()

    async def _transcribe_and_emit(self, window: bytes, started_at: float, duration: float) -> None:
        raw_text = await self._run_blocking(lambda: self._engine.transcribe_pcm(window, self._buffer.sample_rate_hz))
        text = raw_text.strip()
        if not text or _is_non_speech_marker(text):
            return

        deduped = dedupe_overlap(self._previous_text, text)
        self._previous_text = text
        if not deduped:
            return

        ended_at = started_at + duration
        await self._emit_event(
            "transcript.final",
            events.transcript_final(
                session_id=self.session_id,
                segment_id=str(uuid.uuid4()),
                text=deduped,
                started_at=started_at,
                ended_at=ended_at,
            ),
        )

        if self._on_final_transcript is not None:
            try:
                await self._on_final_transcript(deduped, started_at, ended_at)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - question detection/answer generation must never break transcription
                logger.error("Unhandled %s in on_final_transcript callback", type(exc).__name__)

    async def close(self) -> None:
        """Flushes any partial trailing audio, then stops the background
        consumer. Cancellation-safe: waiting on the flushed window's
        transcription is itself best-effort and bounded by the consumer
        task's own cancellation below if it takes too long to matter.

        Skips transcribing the flush if nothing new has arrived since the
        last completed window — what's left is then just the retained
        overlap tail, already covered by the previous `transcript.final`,
        and re-transcribing it would be a redundant Whisper call for
        content `dedupe_overlap` would strip anyway."""
        remaining = self._buffer.flush()
        if remaining is not None and self._bytes_since_last_window > 0:
            # Real timestamps, not placeholders: `_process_turn_signal`'s
            # `force_finalize` boundary (below) is compared against the
            # `ended_at` of whatever fragment this flush produces, so it
            # must land on the same audio timeline as `_last_chunk_end_time`.
            flush_duration = self._bytes_since_last_window / (self._buffer.sample_rate_hz * 2)
            flush_started_at = max(0.0, self._last_chunk_end_time - flush_duration)
            await self._window_queue.put((remaining, flush_started_at, flush_duration))
        await self._window_queue.join()
        self._consumer_task.cancel()
        try:
            await self._consumer_task
        except asyncio.CancelledError:
            pass

        # Flushes any turn VAD had still open (speech observed, no
        # silence endpoint reached yet) so trailing speech at session end
        # isn't silently dropped from turn assembly.
        finalize_signal = self._vad.force_finalize()
        if finalize_signal is not None:
            await self._process_turn_signal(finalize_signal)
