"""Optional, manual-only smoke test measuring REAL audio -> transcript
latency against a real `whisper.cpp` binary and model, using real speech
(the `jfk.wav` sample bundled with whisper.cpp) fed at real-time pacing —
each 0.5s of audio is sent roughly every 0.5s of wall-clock time, the same
cadence real microphone capture uses. Skipped by default. To run it
deliberately:

    VEYA_RUN_WHISPER_SMOKE_TEST=1 \\
    VEYA_WHISPER_BIN=/path/to/whisper-cli \\
    VEYA_WHISPER_MODEL=/path/to/ggml-base.en.bin \\
    python3 -m unittest tests.test_realtime_pipeline_latency_smoke -v

A prior version of this project's latency claim measured only
finalized-turn -> question.detected with text injected directly into the
orchestrator — never touching real audio or the ~4s rolling-window
Whisper re-transcription that dominates real latency. This measures the
part that was missing: real audio chunk -> first `transcript.partial`,
and real audio chunk -> first `transcript.final` for the same window.
Prints (never asserts a specific number — hardware/environment-specific,
not a benchmark claim).
"""

from __future__ import annotations

import asyncio
import os
import time
import unittest
import wave
from pathlib import Path

from veya.transcription.engine import default_whisper_engine_factory
from veya.transcription.session import TranscriptionSession

RUN_SMOKE_TEST = os.environ.get("VEYA_RUN_WHISPER_SMOKE_TEST") == "1"

_JFK_SAMPLE_PATH = (
    Path(__file__).resolve().parent.parent.parent / "whisper.cpp" / "samples" / "jfk.wav"
)


@unittest.skipUnless(RUN_SMOKE_TEST, "set VEYA_RUN_WHISPER_SMOKE_TEST=1 (plus VEYA_WHISPER_BIN/VEYA_WHISPER_MODEL) to run against real audio")
class RealtimePipelineLatencySmokeTest(unittest.IsolatedAsyncioTestCase):
    async def test_real_audio_to_transcript_latency_at_real_time_pacing(self):
        if not _JFK_SAMPLE_PATH.exists():
            self.skipTest(f"whisper.cpp sample audio not found at {_JFK_SAMPLE_PATH}")

        engine = default_whisper_engine_factory()  # raises TranscriptionSetupError if not actually configured

        with wave.open(str(_JFK_SAMPLE_PATH), "rb") as wav_file:
            sample_rate_hz = wav_file.getframerate()
            pcm = wav_file.readframes(wav_file.getnframes())

        timestamps: dict[str, float] = {}

        async def emit_event(name: str, data: dict) -> None:
            if name == "transcript.partial" and "first_partial" not in timestamps:
                timestamps["first_partial"] = time.monotonic()
            if name == "transcript.final" and "first_final" not in timestamps:
                timestamps["first_final"] = time.monotonic()

        session = TranscriptionSession(
            session_id="latency-pipeline-test", sample_rate_hz=sample_rate_hz, engine=engine, emit_event=emit_event,
        )

        chunk_duration = 0.5
        bytes_per_chunk = int(chunk_duration * sample_rate_hz) * 2
        sequence = 0
        offset = 0
        t0 = time.monotonic()
        next_send_time = t0
        while offset < len(pcm):
            chunk = pcm[offset:offset + bytes_per_chunk]
            now = time.monotonic()
            if next_send_time > now:
                await asyncio.sleep(next_send_time - now)
            await session.handle_chunk(sequence, sequence * chunk_duration, chunk_duration, chunk)
            sequence += 1
            offset += bytes_per_chunk
            next_send_time += chunk_duration

        # A bounded wait for whatever's still transcribing (the last
        # window/partial in flight) rather than an unbounded one.
        deadline = time.monotonic() + 10
        while ("first_partial" not in timestamps or "first_final" not in timestamps) and time.monotonic() < deadline:
            await asyncio.sleep(0.1)

        await session.close()

        if "first_partial" in timestamps:
            print(f"\n[latency, this environment only, REAL audio + REAL Whisper] first audio chunk sent -> first transcript.partial: {(timestamps['first_partial'] - t0) * 1000:.0f}ms")
        else:
            print("\n[latency] no transcript.partial observed in this run.")
        if "first_final" in timestamps:
            print(f"[latency, this environment only, REAL audio + REAL Whisper] first audio chunk sent -> first transcript.final (full ~4s window): {(timestamps['first_final'] - t0) * 1000:.0f}ms")
        else:
            print("[latency] no transcript.final observed in this run.")

        # Only asserts the plumbing actually produced *some* real-time
        # feedback before the full window completed — not a specific
        # number, never a benchmark claim.
        if "first_partial" in timestamps and "first_final" in timestamps:
            self.assertLess(timestamps["first_partial"], timestamps["first_final"])
