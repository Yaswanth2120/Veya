import asyncio
import unittest

from veya.ipc.errors import ErrorCode, ProtocolError
from veya.transcription.session import TranscriptionSession


class RecordingEmitter:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []
        self._new_event = asyncio.Event()

    async def __call__(self, event_name: str, data: dict) -> None:
        self.events.append((event_name, data))
        self._new_event.set()

    async def wait_for_count(self, count: int, timeout: float = 2.0) -> None:
        async def _wait():
            while len(self.events) < count:
                self._new_event.clear()
                await self._new_event.wait()

        await asyncio.wait_for(_wait(), timeout=timeout)


class FakeEngine:
    """Returns a canned transcript per call, in order, without ever
    invoking a real subprocess or requiring a model file."""

    def __init__(self, responses: list[str]):
        self._responses = list(responses)
        self.calls: list[tuple[bytes, int]] = []

    def transcribe_pcm(self, pcm_s16le: bytes, sample_rate_hz: int) -> str:
        self.calls.append((pcm_s16le, sample_rate_hz))
        return self._responses.pop(0) if self._responses else ""


async def immediate_run_blocking(fn):
    return fn()


def make_pcm(num_bytes: int) -> bytes:
    return b"\x00" * num_bytes


class TranscriptionSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_chunk_below_window_size_does_not_transcribe(self):
        engine = FakeEngine(["should not be used"])
        emitter = RecordingEmitter()
        session = TranscriptionSession(
            session_id="s1",
            sample_rate_hz=100,
            engine=engine,
            emit_event=emitter,
            run_blocking=immediate_run_blocking,
        )
        try:
            # window_seconds defaults to 4.0s @ 100Hz mono 16-bit = 800 bytes
            await session.handle_chunk(0, 0.0, 0.1, make_pcm(100))
            await asyncio.sleep(0.05)
            self.assertEqual(engine.calls, [])
            self.assertEqual(emitter.events, [])
        finally:
            await session.close()

    async def test_completed_window_triggers_transcript_final_event(self):
        engine = FakeEngine(["hello there"])
        emitter = RecordingEmitter()
        session = TranscriptionSession(
            session_id="s1",
            sample_rate_hz=100,
            engine=engine,
            emit_event=emitter,
            run_blocking=immediate_run_blocking,
        )
        try:
            # 4s window @ 100Hz * 2 bytes = 800 bytes.
            await session.handle_chunk(0, 0.0, 4.0, make_pcm(800))
            await emitter.wait_for_count(1)
        finally:
            await session.close()

        self.assertEqual(len(engine.calls), 1)
        name, data = emitter.events[0]
        self.assertEqual(name, "transcript.final")
        self.assertEqual(data["session_id"], "s1")
        self.assertEqual(data["text"], "hello there")
        self.assertTrue(data["is_final"])

    async def test_empty_transcription_result_emits_no_event(self):
        engine = FakeEngine([""])
        emitter = RecordingEmitter()
        session = TranscriptionSession(
            session_id="s1",
            sample_rate_hz=100,
            engine=engine,
            emit_event=emitter,
            run_blocking=immediate_run_blocking,
        )
        await session.handle_chunk(0, 0.0, 4.0, make_pcm(800))
        # `close()` drains the queue (waits for the in-flight window to
        # finish transcribing) before returning, so no arbitrary sleep is
        # needed to know the (non-)event has already been decided.
        await session.close()

        self.assertEqual(emitter.events, [])

    async def test_overlapping_window_text_is_deduplicated_across_events(self):
        engine = FakeEngine(["we moved the auth service first", "auth service first since everything depended"])
        emitter = RecordingEmitter()
        session = TranscriptionSession(
            session_id="s1",
            sample_rate_hz=100,
            engine=engine,
            emit_event=emitter,
            run_blocking=immediate_run_blocking,
        )
        try:
            await session.handle_chunk(0, 0.0, 4.0, make_pcm(800))
            await emitter.wait_for_count(1)
            # overlap_seconds defaults to 1.0s -> 200 bytes retained, so 600
            # more bytes completes the next window.
            await session.handle_chunk(1, 4.0, 4.0, make_pcm(600))
            await emitter.wait_for_count(2)
        finally:
            await session.close()

        self.assertEqual(emitter.events[0][1]["text"], "we moved the auth service first")
        self.assertEqual(emitter.events[1][1]["text"], "since everything depended")

    async def test_out_of_order_sequence_raises_invalid_params(self):
        engine = FakeEngine([])
        emitter = RecordingEmitter()
        session = TranscriptionSession(
            session_id="s1", sample_rate_hz=100, engine=engine, emit_event=emitter, run_blocking=immediate_run_blocking
        )
        try:
            await session.handle_chunk(5, 0.0, 0.1, make_pcm(10))
            with self.assertRaises(ProtocolError) as ctx:
                await session.handle_chunk(5, 0.1, 0.1, make_pcm(10))
            self.assertEqual(ctx.exception.code, ErrorCode.INVALID_PARAMS)

            with self.assertRaises(ProtocolError):
                await session.handle_chunk(4, 0.2, 0.1, make_pcm(10))
        finally:
            await session.close()

    async def test_close_flushes_remaining_partial_audio(self):
        engine = FakeEngine(["partial tail"])
        emitter = RecordingEmitter()
        session = TranscriptionSession(
            session_id="s1", sample_rate_hz=100, engine=engine, emit_event=emitter, run_blocking=immediate_run_blocking
        )
        await session.handle_chunk(0, 0.0, 0.1, make_pcm(100))  # below window size, never triggers on its own
        await session.close()

        self.assertEqual(len(engine.calls), 1)
        self.assertEqual(emitter.events[0][1]["text"], "partial tail")

    async def test_engine_exception_does_not_crash_the_session_or_leak_message(self):
        class BoomEngine:
            def transcribe_pcm(self, pcm_s16le, sample_rate_hz):
                raise ValueError("some sensitive internal detail")

        emitter = RecordingEmitter()
        session = TranscriptionSession(
            session_id="s1",
            sample_rate_hz=100,
            engine=BoomEngine(),
            emit_event=emitter,
            run_blocking=immediate_run_blocking,
        )
        with self.assertLogs("veya.transcription", level="ERROR") as logs:
            await session.handle_chunk(0, 0.0, 4.0, make_pcm(800))
            await session.close()

        logged_text = "\n".join(logs.output)
        self.assertNotIn("sensitive internal detail", logged_text)
        self.assertIn("ValueError", logged_text)
        self.assertEqual(emitter.events, [])

    async def test_on_final_transcript_hook_fires_with_the_deduped_text_and_timing(self):
        engine = FakeEngine(["hello there"])
        emitter = RecordingEmitter()
        received: list[tuple[str, float, float]] = []

        async def on_final_transcript(text, started_at, ended_at):
            received.append((text, started_at, ended_at))

        session = TranscriptionSession(
            session_id="s1",
            sample_rate_hz=100,
            engine=engine,
            emit_event=emitter,
            run_blocking=immediate_run_blocking,
            on_final_transcript=on_final_transcript,
        )
        try:
            await session.handle_chunk(0, 2.0, 4.0, make_pcm(800))
            await emitter.wait_for_count(1)
        finally:
            await session.close()

        self.assertEqual(received, [("hello there", 2.0, 6.0)])

    async def test_on_final_transcript_hook_is_not_called_when_nothing_is_emitted(self):
        engine = FakeEngine([""])
        emitter = RecordingEmitter()
        received = []

        async def on_final_transcript(text, started_at, ended_at):
            received.append(text)

        session = TranscriptionSession(
            session_id="s1",
            sample_rate_hz=100,
            engine=engine,
            emit_event=emitter,
            run_blocking=immediate_run_blocking,
            on_final_transcript=on_final_transcript,
        )
        await session.handle_chunk(0, 0.0, 4.0, make_pcm(800))
        await session.close()

        self.assertEqual(received, [])

    async def test_on_final_transcript_hook_exception_does_not_break_transcription(self):
        engine = FakeEngine(["first", "second"])
        emitter = RecordingEmitter()

        async def failing_hook(text, started_at, ended_at):
            raise ValueError("sensitive internal detail from the hook")

        session = TranscriptionSession(
            session_id="s1",
            sample_rate_hz=100,
            engine=engine,
            emit_event=emitter,
            run_blocking=immediate_run_blocking,
            on_final_transcript=failing_hook,
        )
        try:
            with self.assertLogs("veya.transcription", level="ERROR") as logs:
                await session.handle_chunk(0, 0.0, 4.0, make_pcm(800))
                await emitter.wait_for_count(1)
        finally:
            await session.close()

        # transcript.final still made it out despite the hook blowing up.
        self.assertEqual(emitter.events[0][0], "transcript.final")
        logged_text = "\n".join(logs.output)
        self.assertNotIn("sensitive internal detail", logged_text)
        self.assertIn("ValueError", logged_text)
