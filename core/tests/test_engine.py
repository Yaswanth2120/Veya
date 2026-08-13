import os
import subprocess
import unittest
from unittest.mock import patch

from veya.transcription.engine import (
    WhisperCliTranscriptionEngine,
    WhisperConfig,
    TranscriptionSetupError,
    default_whisper_engine_factory,
)


class WhisperConfigResolveFromEnvTests(unittest.TestCase):
    def test_missing_env_vars_raises_setup_error(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(TranscriptionSetupError):
                WhisperConfig.resolve_from_env()

    def test_nonexistent_binary_raises_setup_error(self):
        with patch.dict(
            os.environ,
            {"VEYA_WHISPER_BIN": "/does/not/exist/whisper-cli", "VEYA_WHISPER_MODEL": __file__},
            clear=True,
        ):
            with self.assertRaises(TranscriptionSetupError):
                WhisperConfig.resolve_from_env()

    def test_nonexistent_model_raises_setup_error(self):
        with patch.dict(
            os.environ,
            {"VEYA_WHISPER_BIN": "/bin/sh", "VEYA_WHISPER_MODEL": "/does/not/exist/model.bin"},
            clear=True,
        ):
            with self.assertRaises(TranscriptionSetupError):
                WhisperConfig.resolve_from_env()

    def test_valid_binary_and_model_resolves_successfully(self):
        real_binary = "/bin/sh"
        with patch.dict(
            os.environ,
            {"VEYA_WHISPER_BIN": real_binary, "VEYA_WHISPER_MODEL": __file__},
            clear=True,
        ):
            config = WhisperConfig.resolve_from_env()
        self.assertEqual(str(config.binary_path), real_binary)
        self.assertEqual(str(config.model_path), __file__)

    def test_default_whisper_engine_factory_raises_setup_error_when_unconfigured(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(TranscriptionSetupError):
                default_whisper_engine_factory()


class WhisperCliTranscriptionEngineTests(unittest.TestCase):
    def test_successful_transcription_returns_stripped_stdout(self):
        config = WhisperConfig(binary_path="/usr/bin/true", model_path="/usr/bin/true")
        engine = WhisperCliTranscriptionEngine(config)

        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="  hello there  \n", stderr="")
        with patch("veya.transcription.engine.subprocess.run", return_value=completed) as mock_run:
            text = engine.transcribe_pcm(b"\x00\x00" * 100, sample_rate_hz=16000)

        self.assertEqual(text, "hello there")
        mock_run.assert_called_once()

    def test_nonzero_exit_raises_setup_error(self):
        config = WhisperConfig(binary_path="/usr/bin/true", model_path="/usr/bin/true")
        engine = WhisperCliTranscriptionEngine(config)

        completed = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="some sensitive detail")
        with patch("veya.transcription.engine.subprocess.run", return_value=completed):
            with self.assertRaises(TranscriptionSetupError):
                engine.transcribe_pcm(b"\x00\x00" * 100, sample_rate_hz=16000)

    def test_timeout_raises_setup_error(self):
        config = WhisperConfig(binary_path="/usr/bin/true", model_path="/usr/bin/true")
        engine = WhisperCliTranscriptionEngine(config)

        with patch(
            "veya.transcription.engine.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="whisper-cli", timeout=30),
        ):
            with self.assertRaises(TranscriptionSetupError):
                engine.transcribe_pcm(b"\x00\x00" * 100, sample_rate_hz=16000)

    def test_wav_file_is_written_with_correct_pcm_params_and_removed_afterward(self):
        config = WhisperConfig(binary_path="/usr/bin/true", model_path="/usr/bin/true")
        engine = WhisperCliTranscriptionEngine(config)

        captured_paths = []

        def fake_run(args, **kwargs):
            wav_path = args[args.index("-f") + 1]
            captured_paths.append(wav_path)
            self.assertTrue(os.path.isfile(wav_path))
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="ok", stderr="")

        with patch("veya.transcription.engine.subprocess.run", side_effect=fake_run):
            engine.transcribe_pcm(b"\x00\x00" * 100, sample_rate_hz=16000)

        self.assertEqual(len(captured_paths), 1)
        self.assertFalse(os.path.exists(captured_paths[0]))
