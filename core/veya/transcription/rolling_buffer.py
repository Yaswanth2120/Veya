"""Bounded rolling-window audio buffer.

`whisper.cpp`'s CLI takes a whole audio file per invocation — it has no
true streaming-ingest mode. To still produce transcripts while a session is
live, incoming PCM chunks accumulate here until a full `window_seconds` of
audio is available, at which point that window is handed off for
transcription and a `overlap_seconds` tail is kept so the *next* window
starts a little before the previous one ended (avoiding losing a word that
was mid-sentence at the cut point). The buffer never holds more than one
window's worth of PCM plus at most one incoming chunk (bounded, not
unbounded) — true for every real caller here, since each IPC audio chunk
is capped well under one window's duration (see `MAX_AUDIO_CHUNK_BYTES` in
`ipc/dispatcher.py`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

BYTES_PER_SAMPLE = 2  # 16-bit PCM (pcm_s16le)


@dataclass(frozen=True)
class RollingWindowConfig:
    sample_rate_hz: int = 16000
    window_seconds: float = 4.0
    overlap_seconds: float = 1.0

    def __post_init__(self) -> None:
        if self.window_seconds <= 0:
            raise ValueError("window_seconds must be positive.")
        if not (0 <= self.overlap_seconds < self.window_seconds):
            raise ValueError("overlap_seconds must be non-negative and smaller than window_seconds.")


class RollingWindowBuffer:
    def __init__(self, config: Optional[RollingWindowConfig] = None) -> None:
        self.config = config or RollingWindowConfig()
        self._window_bytes = self._seconds_to_bytes(self.config.window_seconds)
        self._overlap_bytes = self._seconds_to_bytes(self.config.overlap_seconds)
        self._buffer = bytearray()

    @property
    def sample_rate_hz(self) -> int:
        return self.config.sample_rate_hz

    @property
    def overlap_bytes(self) -> int:
        return self._overlap_bytes

    def _seconds_to_bytes(self, seconds: float) -> int:
        return int(seconds * self.config.sample_rate_hz) * BYTES_PER_SAMPLE

    def add_chunk(self, pcm: bytes) -> Optional[bytes]:
        """Appends `pcm` to the buffer. Returns a completed window's worth
        of PCM once enough audio has accumulated (retaining the configured
        overlap tail for the next window), otherwise `None`."""
        self._buffer.extend(pcm)
        if len(self._buffer) < self._window_bytes:
            return None

        window = bytes(self._buffer[: self._window_bytes])
        tail_start = self._window_bytes - self._overlap_bytes
        self._buffer = self._buffer[tail_start:]
        return window

    def flush(self) -> Optional[bytes]:
        """Returns whatever partial audio remains (e.g. at session end),
        clearing the buffer. Returns `None` if there's nothing buffered."""
        if not self._buffer:
            return None
        remaining = bytes(self._buffer)
        self._buffer = bytearray()
        return remaining
