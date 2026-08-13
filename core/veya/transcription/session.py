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
        partial_window_seconds: float = 2.0,
        partial_interval_seconds: float = 1.0,
        emit_vad_diagnostics: bool = False,
    ) -> None:
        self.session_id = session_id
        self._buffer = RollingWindowBuffer(RollingWindowConfig(sample_rate_hz=sample_rate_hz))
        self._engine = engine
        self._emit_event = emit_event
        self._run_blocking = run_blocking or self._default_run_blocking
        # Section 14: `transcript.partial` is real, not decorative — a
        # short (default 2s) trailing window is re-transcribed with the
        # same engine roughly once a second *while speech is ongoing*
        # (never during silence — nothing new to show), independently of
        # the ~4s final-window cadence below. Genuinely more Whisper
        # invocations (more CPU), which is the real cost of not waiting a
        # full 4 seconds before showing any feedback. Never persisted,
        # never fed into turn assembly/question detection — purely a live
        # preview Swift replaces wholesale on the next partial or clears
        # on the next `transcript.final`.
        self._sample_rate_hz = sample_rate_hz
        self._partial_buffer = bytearray()
        self._partial_window_bytes = max(int(partial_window_seconds * sample_rate_hz) * 2, 2)
        self._partial_interval_bytes = max(int(partial_interval_seconds * sample_rate_hz) * 2, 2)
        self._bytes_since_last_partial = 0
        self._partial_task: Optional[asyncio.Task] = None
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
        # Off by default (real cost: one extra event per audio chunk,
        # every ~0.1-0.5s) — a developer opts in explicitly (see
        # `VEYA_VAD_DIAGNOSTICS` in `ipc/dispatcher.py`) to see real RMS
        # vs. threshold on the actual microphone, not a simulated one.
        self._emit_vad_diagnostics = emit_vad_diagnostics
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
        self._maybe_schedule_partial_transcription(pcm)

        window = self._buffer.add_chunk(pcm)
        if window is not None:
            self._bytes_since_last_window = 0
            await self._window_queue.put((window, started_at, duration))

    def _maybe_schedule_partial_transcription(self, pcm: bytes) -> None:
        self._partial_buffer.extend(pcm)
        if len(self._partial_buffer) > self._partial_window_bytes:
            del self._partial_buffer[: len(self._partial_buffer) - self._partial_window_bytes]
        self._bytes_since_last_partial += len(pcm)

        due = self._bytes_since_last_partial >= self._partial_interval_bytes
        idle = self._partial_task is None or self._partial_task.done()
        # Only while actually in speech — silence has nothing new to
        # preview, and re-transcribing it repeatedly would just waste CPU
        # and risk surfacing a non-speech marker as if it were a partial.
        if due and idle and self._vad.is_in_speech:
            self._bytes_since_last_partial = 0
            snapshot = bytes(self._partial_buffer)
            self._partial_task = asyncio.create_task(self._transcribe_and_emit_partial(snapshot))

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

    async def _transcribe_and_emit_partial(self, pcm: bytes) -> None:
        try:
            raw_text = await self._run_blocking(lambda: self._engine.transcribe_pcm(pcm, self._sample_rate_hz))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - never let a partial-transcription failure crash the worker or leak content
            logger.error("Unhandled %s while transcribing a partial window", type(exc).__name__)
            return

        text = raw_text.strip()
        if not text or _is_non_speech_marker(text):
            return
        await self._emit_event("transcript.partial", events.transcript_partial(session_id=self.session_id, text=text))

    async def _transcribe_and_emit(self, window: bytes, started_at: float, duration: float) -> None:
        raw_text = await self._run_blocking(lambda: self._engine.transcribe_pcm(window, self._buffer.sample_rate_hz))
        text = raw_text.strip()
        if not text or _is_non_speech_marker(text):
            return

        deduped = dedupe_overlap(self._previous_text, text)
        self._previous_text = text
        if not deduped:
            return

        # The content up through this final window is now superseded by
        # `transcript.final` itself — clears the short partial-preview
        # buffer so the next partial only ever previews genuinely new
        # audio, never content Swift already has as final.
        self._partial_buffer.clear()
        self._bytes_since_last_partial = 0

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
        if self._partial_task is not None and not self._partial_task.done():
            self._partial_task.cancel()
            try:
                await self._partial_task
            except asyncio.CancelledError:
                pass

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
