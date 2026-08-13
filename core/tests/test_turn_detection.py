import struct
import unittest

from veya.transcription.turn_detection import TurnDetectionConfig, TurnSignal, VoiceActivityDetector


def loud_chunk(n: int = 800) -> bytes:
    return struct.pack("<" + "h" * n, *([4000, -4000] * (n // 2)))


def quiet_chunk(n: int = 800) -> bytes:
    return struct.pack("<" + "h" * n, *([5, -5] * (n // 2)))


class VoiceActivityDetectorTests(unittest.TestCase):
    def _config(self, **overrides) -> TurnDetectionConfig:
        base = dict(silence_duration_seconds=1.0, min_speech_duration_seconds=0.2, max_turn_duration_seconds=10.0)
        base.update(overrides)
        return TurnDetectionConfig(**base)

    def test_loud_then_quiet_reports_speech_started_then_silence_candidate_then_finalized(self):
        vad = VoiceActivityDetector(self._config())
        self.assertEqual(vad.process_chunk(loud_chunk(), 0.5), TurnSignal.SPEECH_STARTED)
        self.assertEqual(vad.process_chunk(loud_chunk(), 0.5), TurnSignal.SPEECH_CONTINUING)
        self.assertEqual(vad.process_chunk(quiet_chunk(), 0.5), TurnSignal.SILENCE_CANDIDATE)
        self.assertEqual(vad.process_chunk(quiet_chunk(), 0.5), TurnSignal.TURN_FINALIZED)

    def test_a_brief_pause_shorter_than_the_silence_duration_does_not_finalize(self):
        vad = VoiceActivityDetector(self._config(silence_duration_seconds=2.0))
        vad.process_chunk(loud_chunk(), 0.5)
        self.assertEqual(vad.process_chunk(quiet_chunk(), 0.5), TurnSignal.SILENCE_CANDIDATE)
        # Speech resumes before the 2.0s silence threshold is reached —
        # the turn is still open, no finalize.
        self.assertEqual(vad.process_chunk(loud_chunk(), 0.5), TurnSignal.SPEECH_CONTINUING)

    def test_silence_before_any_speech_reports_nothing(self):
        vad = VoiceActivityDetector(self._config())
        self.assertEqual(vad.process_chunk(quiet_chunk(), 0.5), TurnSignal.NONE)
        self.assertEqual(vad.process_chunk(quiet_chunk(), 0.5), TurnSignal.NONE)

    def test_a_single_loud_chunk_below_min_speech_duration_does_not_finalize_on_silence(self):
        vad = VoiceActivityDetector(self._config(min_speech_duration_seconds=1.0, silence_duration_seconds=0.5))
        vad.process_chunk(loud_chunk(), 0.2)  # below min_speech_duration_seconds
        self.assertEqual(vad.process_chunk(quiet_chunk(), 0.5), TurnSignal.NONE)
        self.assertEqual(vad.process_chunk(quiet_chunk(), 0.5), TurnSignal.NONE)

    def test_max_turn_duration_finalizes_even_without_silence(self):
        vad = VoiceActivityDetector(self._config(max_turn_duration_seconds=1.0))
        self.assertEqual(vad.process_chunk(loud_chunk(), 0.5), TurnSignal.SPEECH_STARTED)
        self.assertEqual(vad.process_chunk(loud_chunk(), 0.6), TurnSignal.TURN_FINALIZED)

    def test_force_finalize_flushes_an_open_turn_at_session_end(self):
        vad = VoiceActivityDetector(self._config())
        vad.process_chunk(loud_chunk(), 0.5)
        self.assertEqual(vad.force_finalize(), TurnSignal.TURN_FINALIZED)

    def test_force_finalize_with_nothing_in_progress_returns_none(self):
        vad = VoiceActivityDetector(self._config())
        self.assertIsNone(vad.force_finalize())

    def test_force_finalize_below_min_speech_duration_returns_none(self):
        vad = VoiceActivityDetector(self._config(min_speech_duration_seconds=5.0))
        vad.process_chunk(loud_chunk(), 0.5)
        self.assertIsNone(vad.force_finalize())

    def test_state_resets_after_finalize_for_the_next_turn(self):
        vad = VoiceActivityDetector(self._config())
        vad.process_chunk(loud_chunk(), 0.5)
        vad.process_chunk(quiet_chunk(), 0.5)
        vad.process_chunk(quiet_chunk(), 0.5)  # finalizes
        self.assertFalse(vad.is_in_speech)
        self.assertEqual(vad.process_chunk(loud_chunk(), 0.5), TurnSignal.SPEECH_STARTED)
