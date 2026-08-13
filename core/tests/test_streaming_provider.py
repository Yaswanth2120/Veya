import asyncio
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

from veya.transcription.engine import TranscriptionSetupError
from veya.transcription.streaming_provider import (
    ASRHypothesis,
    WhisperCppCliStreamingProvider,
    WhisperCppStreamingProvider,
    default_streaming_asr_provider_factory,
    resolve_streaming_binary_path,
)

# A tiny real executable standing in for `whisper-stream-stdin` — real
# subprocess plumbing (argv, stdin/stdout pipes, EOF handling), no real
# Whisper model required. Ignores every CLI arg it's given (the real
# binary's `-m`/`--step`/etc. flags), reads raw bytes from stdin, and
# emits one JSON-Lines "partial" per 10 bytes received plus one "final"
# on EOF — deliberately not real whisper.cpp output, just enough shape
# to exercise the real asyncio.subprocess integration end-to-end.
_FAKE_BINARY_SOURCE = """#!/usr/bin/env python3
import sys, json

total = 0
chunk_count = 0
while True:
    data = sys.stdin.buffer.read(10)
    if not data:
        break
    total += len(data)
    chunk_count += 1
    print(json.dumps({"type": "partial", "text": f"chunk {chunk_count}"}))
    sys.stdout.flush()

print(json.dumps({"type": "final", "text": f"done, {total} bytes total"}))
sys.stdout.flush()
"""


def _make_fake_binary(tmp_dir: Path) -> Path:
    path = tmp_dir / "fake-whisper-stream-stdin"
    path.write_text(_FAKE_BINARY_SOURCE)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


class WhisperCppStreamingProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_subprocess_plumbing_produces_partial_then_final_hypotheses(self):
        with tempfile.TemporaryDirectory(prefix="veya-streaming-test-") as tmp:
            binary_path = _make_fake_binary(Path(tmp))
            provider = WhisperCppStreamingProvider(binary_path=binary_path, model_path=Path("unused-by-fake"))
            self.assertFalse(provider.is_degraded)

            await provider.start()
            await provider.feed_pcm(b"0123456789")  # exactly 10 bytes -> one partial
            await provider.feed_pcm(b"0123456789")  # a second partial

            hypotheses: list[ASRHypothesis] = []

            async def collect():
                async for hyp in provider.hypotheses():
                    hypotheses.append(hyp)

            collect_task = asyncio.create_task(collect())
            await asyncio.sleep(0.3)
            await provider.stop()
            await asyncio.wait_for(collect_task, timeout=5.0)

        names = [(h.text, h.is_final) for h in hypotheses]
        self.assertIn(("chunk 1", False), names)
        self.assertIn(("chunk 2", False), names)
        self.assertEqual(names[-1], ("done, 20 bytes total", True))

    async def test_stop_before_start_does_not_raise(self):
        provider = WhisperCppStreamingProvider(binary_path=Path("/nonexistent"), model_path=Path("/nonexistent"))
        await provider.stop()  # never started — must be a harmless no-op


class WhisperCppCliStreamingProviderTests(unittest.IsolatedAsyncioTestCase):
    class _FakeEngine:
        def __init__(self, responses):
            self._responses = list(responses)

        def transcribe_pcm(self, pcm_s16le, sample_rate_hz):
            return self._responses.pop(0) if self._responses else ""

    async def test_is_degraded_and_only_emits_final_hypotheses_per_window(self):
        engine = self._FakeEngine(["first window text"])
        provider = WhisperCppCliStreamingProvider(engine=engine, sample_rate_hz=100, window_seconds=0.1)
        self.assertTrue(provider.is_degraded)

        await provider.start()
        await provider.feed_pcm(b"\x00" * 20)  # exactly one window (0.1s * 100Hz * 2 bytes)

        hyp = await asyncio.wait_for(provider.hypotheses().__anext__(), timeout=2.0)
        self.assertEqual(hyp.text, "first window text")
        self.assertTrue(hyp.is_final)
        await provider.stop()

    async def test_close_flushes_a_partial_trailing_window(self):
        engine = self._FakeEngine(["trailing text"])
        provider = WhisperCppCliStreamingProvider(engine=engine, sample_rate_hz=100, window_seconds=1.0)
        await provider.start()
        await provider.feed_pcm(b"\x00" * 10)  # well under one window

        results = []

        async def collect():
            async for hyp in provider.hypotheses():
                results.append(hyp)

        task = asyncio.create_task(collect())
        await provider.stop()
        await asyncio.wait_for(task, timeout=2.0)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].text, "trailing text")


class ProviderResolutionTests(unittest.TestCase):
    def test_resolve_streaming_binary_path_prefers_explicit_env_var(self):
        with tempfile.TemporaryDirectory(prefix="veya-streaming-resolve-") as tmp:
            binary_path = _make_fake_binary(Path(tmp))
            old = os.environ.get("VEYA_WHISPER_STREAM_BIN")
            os.environ["VEYA_WHISPER_STREAM_BIN"] = str(binary_path)
            try:
                self.assertEqual(resolve_streaming_binary_path(), binary_path)
            finally:
                if old is None:
                    os.environ.pop("VEYA_WHISPER_STREAM_BIN", None)
                else:
                    os.environ["VEYA_WHISPER_STREAM_BIN"] = old

    def test_resolve_streaming_binary_path_finds_a_sibling_of_the_cli_binary(self):
        with tempfile.TemporaryDirectory(prefix="veya-streaming-resolve-") as tmp:
            tmp_path = Path(tmp)
            cli_binary = tmp_path / "whisper-cli"
            cli_binary.write_text("#!/bin/sh\n")
            cli_binary.chmod(cli_binary.stat().st_mode | stat.S_IEXEC)
            stream_binary = tmp_path / "whisper-stream-stdin"
            stream_binary.write_text("#!/bin/sh\n")
            stream_binary.chmod(stream_binary.stat().st_mode | stat.S_IEXEC)

            old_stream = os.environ.pop("VEYA_WHISPER_STREAM_BIN", None)
            old_bin = os.environ.get("VEYA_WHISPER_BIN")
            os.environ["VEYA_WHISPER_BIN"] = str(cli_binary)
            try:
                self.assertEqual(resolve_streaming_binary_path(), stream_binary)
            finally:
                if old_stream is not None:
                    os.environ["VEYA_WHISPER_STREAM_BIN"] = old_stream
                if old_bin is None:
                    os.environ.pop("VEYA_WHISPER_BIN", None)
                else:
                    os.environ["VEYA_WHISPER_BIN"] = old_bin

    def test_resolve_streaming_binary_path_returns_none_when_nothing_is_configured(self):
        old_stream = os.environ.pop("VEYA_WHISPER_STREAM_BIN", None)
        old_bin = os.environ.pop("VEYA_WHISPER_BIN", None)
        try:
            self.assertIsNone(resolve_streaming_binary_path())
        finally:
            if old_stream is not None:
                os.environ["VEYA_WHISPER_STREAM_BIN"] = old_stream
            if old_bin is not None:
                os.environ["VEYA_WHISPER_BIN"] = old_bin

    def test_factory_raises_setup_error_when_neither_path_is_usable(self):
        old_stream = os.environ.pop("VEYA_WHISPER_STREAM_BIN", None)
        old_bin = os.environ.pop("VEYA_WHISPER_BIN", None)
        try:
            with self.assertRaises(TranscriptionSetupError):
                default_streaming_asr_provider_factory(engine_for_fallback=None, sample_rate_hz=16000)
        finally:
            if old_stream is not None:
                os.environ["VEYA_WHISPER_STREAM_BIN"] = old_stream
            if old_bin is not None:
                os.environ["VEYA_WHISPER_BIN"] = old_bin

    def test_factory_falls_back_to_degraded_provider_when_streaming_binary_is_unavailable(self):
        old_stream = os.environ.pop("VEYA_WHISPER_STREAM_BIN", None)
        old_bin = os.environ.pop("VEYA_WHISPER_BIN", None)
        try:
            provider = default_streaming_asr_provider_factory(
                engine_for_fallback=WhisperCppCliStreamingProviderTests._FakeEngine([]), sample_rate_hz=16000
            )
            self.assertTrue(provider.is_degraded)
        finally:
            if old_stream is not None:
                os.environ["VEYA_WHISPER_STREAM_BIN"] = old_stream
            if old_bin is not None:
                os.environ["VEYA_WHISPER_BIN"] = old_bin


if __name__ == "__main__":
    unittest.main()
