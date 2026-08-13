"""The `TranscriptionEngine` abstraction and its local-Whisper
implementation. Kept separate from `session.py` so tests can substitute a
fake engine and never invoke a real subprocess or require a model file.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class TranscriptionSetupError(Exception):
    """Raised when real transcription cannot be enabled — missing/invalid
    Whisper binary or model, typically. Always a clean, typed condition:
    the caller reports it to Swift as `TRANSCRIPTION_UNAVAILABLE` and falls
    back to the mock pipeline; it must never crash the worker."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class TranscriptionEngine(Protocol):
    """Anything that can turn a window of mono 16-bit PCM into text. Runs
    on a worker thread (see `session.py`), so implementations may block."""

    def transcribe_pcm(self, pcm_s16le: bytes, sample_rate_hz: int) -> str: ...


@dataclass(frozen=True)
class WhisperConfig:
    binary_path: Path
    model_path: Path
    timeout_seconds: float = 30.0

    @staticmethod
    def resolve_from_env() -> "WhisperConfig":
        """Reads `VEYA_WHISPER_BIN`/`VEYA_WHISPER_MODEL` — no
        developer-specific defaults are hardcoded. Both must point at
        existing, usable files or this raises `TranscriptionSetupError`,
        which callers convert into a typed `TRANSCRIPTION_UNAVAILABLE`
        response rather than letting the worker crash."""
        binary = os.environ.get("VEYA_WHISPER_BIN")
        model = os.environ.get("VEYA_WHISPER_MODEL")
        if not binary or not model:
            raise TranscriptionSetupError(
                "VEYA_WHISPER_BIN and VEYA_WHISPER_MODEL must both be set to enable real transcription."
            )

        binary_path = Path(binary)
        model_path = Path(model)
        if not binary_path.is_file() or not os.access(binary_path, os.X_OK):
            raise TranscriptionSetupError("VEYA_WHISPER_BIN does not point at an executable file.")
        if not model_path.is_file():
            raise TranscriptionSetupError("VEYA_WHISPER_MODEL does not point at an existing file.")

        return WhisperConfig(binary_path=binary_path, model_path=model_path)


class WhisperCliTranscriptionEngine:
    """Invokes a local `whisper.cpp`-style CLI binary per window. Each
    window's PCM is written to a WAV file in a temporary directory that is
    always removed immediately after the subprocess exits (success or
    failure) — raw audio is never written anywhere persistent, and the
    temp path itself is never logged."""

    def __init__(self, config: WhisperConfig) -> None:
        self._config = config

    def transcribe_pcm(self, pcm_s16le: bytes, sample_rate_hz: int) -> str:
        with tempfile.TemporaryDirectory(prefix="veya-whisper-") as tmp_dir:
            wav_path = Path(tmp_dir) / "window.wav"
            with wave.open(str(wav_path), "wb") as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(sample_rate_hz)
                wav_file.writeframes(pcm_s16le)

            try:
                result = subprocess.run(
                    [str(self._config.binary_path), "-m", str(self._config.model_path), "-f", str(wav_path), "-nt", "-np"],
                    capture_output=True,
                    text=True,
                    timeout=self._config.timeout_seconds,
                )
            except subprocess.TimeoutExpired as exc:
                raise TranscriptionSetupError("Whisper process timed out.") from exc

        if result.returncode != 0:
            raise TranscriptionSetupError("Whisper process exited with a non-zero status.")

        return result.stdout.strip()


def default_whisper_engine_factory() -> TranscriptionEngine:
    """The production `transcription_engine_factory` — resolves
    configuration fresh on every call (not cached at import time) so an
    env-var change between worker starts is picked up, and so tests can
    monkeypatch the environment freely."""
    config = WhisperConfig.resolve_from_env()
    return WhisperCliTranscriptionEngine(config)
