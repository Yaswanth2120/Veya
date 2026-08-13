"""Real-time speech-to-text abstraction that can emit *incremental*
partial hypotheses as audio arrives, not only a final transcript once a
fixed window completes.

`WhisperCppStreamingProvider` is the primary, genuine-streaming
implementation: a persistent `whisper-stream-stdin` subprocess (a small
binary vendored under `whisper.cpp/examples/stream-stdin/`, adapted from
whisper.cpp's own real-time reference example) that keeps re-decoding a
bounded, sliding trailing window of audio as new PCM is written to its
stdin, emitting a JSON-Lines hypothesis after every step — not a batch
CLI invoked once per fixed window.

`WhisperCppCliStreamingProvider` wraps the older one-shot-per-window
whisper.cpp CLI path (`engine.py`'s `WhisperCliTranscriptionEngine`)
behind the same interface, so callers never need a different code path
for it — but `is_degraded` is always `True` on it, and callers/Swift must
surface that honestly rather than silently presenting it as the same
experience.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Optional

from .engine import TranscriptionEngine, TranscriptionSetupError, WhisperConfig

logger = logging.getLogger("veya.transcription.streaming")


class StreamingASRUnavailableError(Exception):
    """Raised by `StreamingASRProvider.start()` when the engine could not
    be started (missing binary, missing model, process failed to launch).
    Callers catch this and fall back to a degraded provider — real-time
    transcription must never crash the worker over this."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class ASRHypothesis:
    text: str
    is_final: bool


class StreamingASRProvider:
    """Interface every real-time ASR engine implements. `is_degraded`
    tells callers (and, via the diagnostics event, Swift) whether this is
    the genuine incremental streaming engine or a batch-CLI-based
    approximation of it standing in for it — never hidden either way."""

    is_degraded: bool = False

    async def start(self) -> None:
        raise NotImplementedError

    async def feed_pcm(self, pcm_s16le: bytes) -> None:
        raise NotImplementedError

    def hypotheses(self) -> AsyncIterator[ASRHypothesis]:
        raise NotImplementedError

    async def stop(self) -> None:
        raise NotImplementedError


class WhisperCppStreamingProvider(StreamingASRProvider):
    """Manages one persistent `whisper-stream-stdin` subprocess for the
    lifetime of a Live Session. PCM is written to its stdin as it arrives;
    JSON-Lines hypotheses are read from its stdout by a background task
    and handed out through `hypotheses()`."""

    is_degraded = False

    def __init__(
        self,
        binary_path: Path,
        model_path: Path,
        sample_rate_hz: int = 16000,
        step_ms: int = 1000,
        length_ms: int = 6000,
        keep_ms: int = 200,
        threads: int = 4,
    ) -> None:
        self._binary_path = binary_path
        self._model_path = model_path
        self._sample_rate_hz = sample_rate_hz
        self._step_ms = step_ms
        self._length_ms = length_ms
        self._keep_ms = keep_ms
        self._threads = threads
        self._process: Optional[asyncio.subprocess.Process] = None
        self._queue: "asyncio.Queue[Optional[ASRHypothesis]]" = asyncio.Queue()
        self._reader_task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        try:
            self._process = await asyncio.create_subprocess_exec(
                str(self._binary_path),
                "-m", str(self._model_path),
                "--step", str(self._step_ms),
                "--length", str(self._length_ms),
                "--keep", str(self._keep_ms),
                "-t", str(self._threads),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError as exc:
            raise StreamingASRUnavailableError(f"failed to launch whisper-stream-stdin: {type(exc).__name__}") from exc
        self._reader_task = asyncio.create_task(self._read_stdout())

    async def _read_stdout(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        try:
            while True:
                line = await self._process.stdout.readline()
                if not line:
                    break
                try:
                    payload = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                text = str(payload.get("text", "")).strip()
                if text:
                    await self._queue.put(ASRHypothesis(text=text, is_final=payload.get("type") == "final"))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - never let a reader failure crash the worker
            logger.error("Unhandled %s reading whisper-stream-stdin output", type(exc).__name__)
        finally:
            await self._queue.put(None)  # sentinel: no more hypotheses will ever arrive

    async def feed_pcm(self, pcm_s16le: bytes) -> None:
        if self._process is None or self._process.stdin is None or self._process.stdin.is_closing():
            return
        try:
            self._process.stdin.write(pcm_s16le)
            await self._process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            logger.error("whisper-stream-stdin's stdin closed unexpectedly while writing audio")

    async def hypotheses(self) -> AsyncIterator[ASRHypothesis]:
        while True:
            item = await self._queue.get()
            if item is None:
                return
            yield item

    async def stop(self) -> None:
        if self._process is not None and self._process.stdin is not None:
            try:
                if not self._process.stdin.is_closing():
                    self._process.stdin.close()
            except Exception:  # noqa: BLE001 - shutdown must never raise
                pass
        if self._process is not None:
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                self._process.kill()
        if self._reader_task is not None and not self._reader_task.done():
            try:
                await asyncio.wait_for(self._reader_task, timeout=5.0)
            except asyncio.TimeoutError:
                self._reader_task.cancel()


class WhisperCppCliStreamingProvider(StreamingASRProvider):
    """Degraded fallback: wraps the older one-shot-per-window whisper.cpp
    CLI engine behind the `StreamingASRProvider` interface, buffering PCM
    into fixed windows and only ever emitting `is_final=True` hypotheses
    (there is no partial output in this mode — always labeled degraded,
    never presented as real-time streaming)."""

    is_degraded = True

    def __init__(self, engine: TranscriptionEngine, sample_rate_hz: int, window_seconds: float = 4.0) -> None:
        self._engine = engine
        self._sample_rate_hz = sample_rate_hz
        self._window_bytes = max(int(window_seconds * sample_rate_hz) * 2, 2)
        self._buffer = bytearray()
        self._queue: "asyncio.Queue[Optional[ASRHypothesis]]" = asyncio.Queue()
        self._stopped = False

    async def start(self) -> None:
        return None

    async def feed_pcm(self, pcm_s16le: bytes) -> None:
        if self._stopped:
            return
        self._buffer.extend(pcm_s16le)
        while len(self._buffer) >= self._window_bytes:
            window = bytes(self._buffer[: self._window_bytes])
            del self._buffer[: self._window_bytes]
            await self._transcribe_and_enqueue(window)

    async def _transcribe_and_enqueue(self, window: bytes) -> None:
        loop = asyncio.get_running_loop()
        try:
            text = await loop.run_in_executor(None, lambda: self._engine.transcribe_pcm(window, self._sample_rate_hz))
        except Exception as exc:  # noqa: BLE001 - never let a transcription failure crash the worker
            logger.error("Unhandled %s during degraded-fallback transcription", type(exc).__name__)
            return
        stripped = text.strip()
        if stripped:
            await self._queue.put(ASRHypothesis(text=stripped, is_final=True))

    async def hypotheses(self) -> AsyncIterator[ASRHypothesis]:
        while True:
            item = await self._queue.get()
            if item is None:
                return
            yield item

    async def stop(self) -> None:
        self._stopped = True
        if self._buffer:
            window = bytes(self._buffer)
            self._buffer.clear()
            await self._transcribe_and_enqueue(window)
        await self._queue.put(None)


def resolve_streaming_binary_path() -> Optional[Path]:
    """`VEYA_WHISPER_STREAM_BIN` if set; otherwise a `whisper-stream-stdin`
    sibling next to `VEYA_WHISPER_BIN`, if it exists — lets a single
    packaged bundle carry both binaries without a second env var most of
    the time. Returns `None` (never raises) if nothing usable is found;
    the caller falls back to the degraded provider."""
    explicit = os.environ.get("VEYA_WHISPER_STREAM_BIN")
    if explicit:
        path = Path(explicit)
        if path.is_file() and os.access(path, os.X_OK):
            return path
        return None

    cli_bin = os.environ.get("VEYA_WHISPER_BIN")
    if not cli_bin:
        return None
    sibling = Path(cli_bin).parent / "whisper-stream-stdin"
    if sibling.is_file() and os.access(sibling, os.X_OK):
        return sibling
    return None


def default_streaming_asr_provider_factory(
    engine_for_fallback: Optional[TranscriptionEngine], sample_rate_hz: int
) -> StreamingASRProvider:
    """Prefers the genuine streaming engine; falls back to the degraded
    CLI-wrapping provider (using `engine_for_fallback`, already resolved
    by the caller against `VEYA_WHISPER_BIN`/`VEYA_WHISPER_MODEL`) if the
    streaming binary isn't available. Raises `TranscriptionSetupError`
    only if neither path is usable — the same typed condition
    `transcription.start` already reports as `TRANSCRIPTION_UNAVAILABLE`."""
    stream_bin = resolve_streaming_binary_path()
    model = os.environ.get("VEYA_WHISPER_MODEL")
    if stream_bin is not None and model:
        model_path = Path(model)
        if model_path.is_file():
            return WhisperCppStreamingProvider(binary_path=stream_bin, model_path=model_path, sample_rate_hz=sample_rate_hz)

    if engine_for_fallback is None:
        raise TranscriptionSetupError(
            "Neither the streaming ASR binary nor the batch whisper.cpp CLI engine is available."
        )
    logger.info("Streaming ASR binary unavailable; using the degraded batch-CLI fallback provider.")
    return WhisperCppCliStreamingProvider(engine=engine_for_fallback, sample_rate_hz=sample_rate_hz)
