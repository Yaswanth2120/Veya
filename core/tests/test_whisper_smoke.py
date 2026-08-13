"""Optional, manual-only smoke test against a REAL `whisper.cpp` binary and
model. Skipped by default — this repo's ordinary test run (`python3 -m
unittest discover`) must never require Whisper or a model file to be
installed. To run it deliberately:

    VEYA_RUN_WHISPER_SMOKE_TEST=1 \\
    VEYA_WHISPER_BIN=/path/to/whisper-cli \\
    VEYA_WHISPER_MODEL=/path/to/ggml-base.en.bin \\
    python3 -m unittest tests.test_whisper_smoke -v

This only proves the `WhisperCliTranscriptionEngine` subprocess plumbing
(WAV write → CLI invocation → stdout parse) works end-to-end against a
real binary on synthetic silence; it does not measure real speech
accuracy or real-time latency. See docs/REALTIME_TRANSCRIPTION.md.
"""

from __future__ import annotations

import os
import struct
import unittest

from veya.transcription.engine import WhisperCliTranscriptionEngine, WhisperConfig

RUN_SMOKE_TEST = os.environ.get("VEYA_RUN_WHISPER_SMOKE_TEST") == "1"


@unittest.skipUnless(RUN_SMOKE_TEST, "set VEYA_RUN_WHISPER_SMOKE_TEST=1 to run against a real whisper.cpp binary")
class WhisperSmokeTest(unittest.TestCase):
    def test_transcribes_one_second_of_silence_without_crashing(self):
        config = WhisperConfig.resolve_from_env()
        engine = WhisperCliTranscriptionEngine(config)

        sample_rate_hz = 16000
        silence = struct.pack("<%dh" % sample_rate_hz, *([0] * sample_rate_hz))

        # Only asserts the plumbing doesn't crash and returns a string —
        # silence has no reliable expected transcript.
        text = engine.transcribe_pcm(silence, sample_rate_hz)
        self.assertIsInstance(text, str)
