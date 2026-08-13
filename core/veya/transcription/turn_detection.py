"""Local, chunk-level speech/silence turn detection — an energy-based
voice-activity heuristic (RMS amplitude vs. a configurable threshold),
not a trained VAD model and not a cloud call. Operates on the same raw
`pcm_s16le` chunks `TranscriptionSession.handle_chunk` already receives,
independently of Whisper's ~4-second rolling-window transcription cadence
— this is what lets a turn boundary be detected in near-real-time instead
of only at the next window completion.

This is a practical V1 heuristic, not a claim of parity with a trained
VAD model (e.g. WebRTC VAD, Silero). It is deliberately simple, local,
and auditable.
"""

from __future__ import annotations

import array
import math
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class TurnSignal(str, Enum):
    """One transition `VoiceActivityDetector.process_chunk` can report.
    `NONE` means "no state change this chunk" — most chunks in the middle
    of ongoing speech or ongoing silence report `NONE`."""

    NONE = "none"
    SPEECH_STARTED = "speech_started"
    SPEECH_CONTINUING = "speech_continuing"
    SILENCE_CANDIDATE = "silence_candidate"
    TURN_FINALIZED = "turn_finalized"


@dataclass(frozen=True)
class TurnDetectionConfig:
    sample_rate_hz: int = 16000
    # RMS amplitude (0-32767 for 16-bit PCM) above which a chunk counts as
    # speech. A quiet room / typical laptop mic noise floor sits well
    # below this; a spoken voice at normal distance sits well above it.
    # Not calibrated against a labeled dataset — a documented default, not
    # a benchmark claim.
    speech_rms_threshold: int = 400
    # How long silence must persist before a turn is considered finished.
    # Long enough that an ordinary mid-sentence breath/pause ("...and,
    # uh, what inputs...") doesn't end the turn early; short enough that
    # an interview doesn't feel laggy waiting for an answer.
    silence_duration_seconds: float = 1.2
    # A turn is only eligible to finalize on silence once at least this
    # much speech has actually been observed — prevents a single noisy
    # chunk from starting and instantly "finalizing" a near-empty turn.
    min_speech_duration_seconds: float = 0.3
    # Safety cap: finalize even without silence once a turn has run this
    # long, so a misdetection (or someone who simply talks for a long
    # time without pausing) can't block turn assembly indefinitely.
    max_turn_duration_seconds: float = 45.0


class VoiceActivityDetector:
    """Stateful per real-transcription-session. Feed every incoming audio
    chunk via `process_chunk`; react to the returned `TurnSignal`."""

    def __init__(self, config: Optional[TurnDetectionConfig] = None) -> None:
        self._config = config or TurnDetectionConfig()
        self._in_speech = False
        self._speech_seconds = 0.0
        self._silence_seconds = 0.0
        self._turn_elapsed_seconds = 0.0
        self._silence_candidate_reported = False
        self._last_rms = 0.0

    @property
    def last_rms(self) -> float:
        """The RMS amplitude computed for the most recent chunk passed to
        `process_chunk` — real diagnostic data (not decorative), meant for
        a developer-facing view so real microphone behavior can be
        verified against the actual threshold rather than guessed at."""
        return self._last_rms

    @property
    def is_in_speech(self) -> bool:
        return self._in_speech

    @property
    def speech_rms_threshold(self) -> int:
        return self._config.speech_rms_threshold

    @property
    def speech_seconds(self) -> float:
        return self._speech_seconds

    @property
    def silence_seconds(self) -> float:
        return self._silence_seconds

    def process_chunk(self, pcm_s16le: bytes, duration_seconds: float) -> TurnSignal:
        self._last_rms = self._rms(pcm_s16le)
        is_loud = self._last_rms >= self._config.speech_rms_threshold

        if is_loud:
            return self._handle_loud_chunk(duration_seconds)
        return self._handle_quiet_chunk(duration_seconds)

    def _handle_loud_chunk(self, duration_seconds: float) -> TurnSignal:
        self._silence_seconds = 0.0
        self._silence_candidate_reported = False
        self._turn_elapsed_seconds += duration_seconds
        self._speech_seconds += duration_seconds

        if not self._in_speech:
            self._in_speech = True
            return TurnSignal.SPEECH_STARTED

        if self._turn_elapsed_seconds >= self._config.max_turn_duration_seconds:
            return self._finalize()

        return TurnSignal.SPEECH_CONTINUING

    def _handle_quiet_chunk(self, duration_seconds: float) -> TurnSignal:
        if not self._in_speech:
            return TurnSignal.NONE

        self._turn_elapsed_seconds += duration_seconds
        self._silence_seconds += duration_seconds

        if self._speech_seconds < self._config.min_speech_duration_seconds:
            return TurnSignal.NONE

        if self._silence_seconds >= self._config.silence_duration_seconds:
            return self._finalize()

        if not self._silence_candidate_reported:
            self._silence_candidate_reported = True
            return TurnSignal.SILENCE_CANDIDATE

        return TurnSignal.NONE

    def _finalize(self) -> TurnSignal:
        self._in_speech = False
        self._speech_seconds = 0.0
        self._silence_seconds = 0.0
        self._turn_elapsed_seconds = 0.0
        self._silence_candidate_reported = False
        return TurnSignal.TURN_FINALIZED

    def force_finalize(self) -> Optional[TurnSignal]:
        """Called on session stop — finalizes a turn that was still
        in-progress (speech observed, no silence endpoint reached yet) so
        trailing speech isn't silently dropped. Returns `None` if nothing
        was in progress."""
        if not self._in_speech or self._speech_seconds < self._config.min_speech_duration_seconds:
            self._in_speech = False
            self._speech_seconds = 0.0
            self._silence_seconds = 0.0
            self._turn_elapsed_seconds = 0.0
            self._silence_candidate_reported = False
            return None
        return self._finalize()

    @staticmethod
    def _rms(pcm_s16le: bytes) -> float:
        # `audioop` (the stdlib's historical home for this) was removed in
        # Python 3.13 — computed directly from 16-bit little-endian
        # samples instead, which is all `audioop.rms` did internally.
        if len(pcm_s16le) < 2:
            return 0.0
        usable_length = len(pcm_s16le) - (len(pcm_s16le) % 2)
        samples = array.array("h")
        samples.frombytes(pcm_s16le[:usable_length])
        if not samples:
            return 0.0
        sum_of_squares = sum(sample * sample for sample in samples)
        return math.sqrt(sum_of_squares / len(samples))
