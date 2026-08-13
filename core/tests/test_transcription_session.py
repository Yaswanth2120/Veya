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


def make_loud_pcm(num_bytes: int) -> bytes:
    """Non-silent 16-bit PCM (alternating +/- high amplitude) — used to
    exercise VAD/turn-detection, which `make_pcm`'s all-zero bytes never
    trigger (deliberately, so every pre-existing test above is
    unaffected by VAD's presence unless it opts in to loud audio)."""
    import struct

    count = num_bytes // 2
    samples = ([6000, -6000] * ((count + 1) // 2))[:count]
    return struct.pack("<" + "h" * count, *samples)


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

    async def test_non_speech_markers_emit_no_event_and_never_reach_swift(self):
        # whisper.cpp emits a bracketed tag instead of real words for
        # silent/non-speech windows — these must never surface in
        # Swift/the user-facing history as if they were real transcript
        # content (a review finding: raw "[BLANK_AUDIO]" markers were
        # visible in Previous Sessions).
        for marker in ("[BLANK_AUDIO]", "(silence)", "[SILENCE]", "[ Music ]", "[ Applause ]"):
            with self.subTest(marker=marker):
                engine = FakeEngine([marker])
                emitter = RecordingEmitter()
                session = TranscriptionSession(
                    session_id="s1",
                    sample_rate_hz=100,
                    engine=engine,
                    emit_event=emitter,
                    run_blocking=immediate_run_blocking,
                )
                await session.handle_chunk(0, 0.0, 4.0, make_pcm(800))
                await session.close()

                self.assertEqual(emitter.events, [])

    async def test_a_sentence_containing_a_bracketed_aside_is_not_treated_as_a_marker(self):
        # Only a window whose *entire* text is one bracketed/parenthesized
        # tag is filtered — real speech that happens to include a
        # parenthetical must still come through untouched.
        engine = FakeEngine(["the migration took six weeks (roughly)"])
        emitter = RecordingEmitter()
        session = TranscriptionSession(
            session_id="s1",
            sample_rate_hz=100,
            engine=engine,
            emit_event=emitter,
            run_blocking=immediate_run_blocking,
        )
        await session.handle_chunk(0, 0.0, 4.0, make_pcm(800))
        await session.close()

        self.assertEqual(len(emitter.events), 1)
        self.assertEqual(emitter.events[0][1]["text"], "the migration took six weeks (roughly)")

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

    async def test_speech_then_silence_emits_turn_state_events_and_calls_on_turn_boundary(self):
        from veya.transcription.turn_detection import TurnDetectionConfig, VoiceActivityDetector

        engine = FakeEngine([])
        emitter = RecordingEmitter()
        boundary_calls: list[float] = []

        async def on_turn_boundary(boundary_time: float) -> None:
            boundary_calls.append(boundary_time)

        vad = VoiceActivityDetector(TurnDetectionConfig(silence_duration_seconds=1.0, min_speech_duration_seconds=0.2))
        session = TranscriptionSession(
            session_id="s1", sample_rate_hz=100, engine=engine, emit_event=emitter,
            run_blocking=immediate_run_blocking, on_turn_boundary=on_turn_boundary, vad=vad,
        )

        await session.handle_chunk(0, 0.0, 0.5, make_loud_pcm(100))
        await session.handle_chunk(1, 0.5, 0.5, make_pcm(100))  # silence candidate
        await session.handle_chunk(2, 1.0, 0.5, make_pcm(100))  # finalizes at 1.0s silence

        state_events = [data["state"] for name, data in emitter.events if name == "turn.state"]
        self.assertEqual(state_events, ["speech", "waiting_for_silence", "listening"])
        self.assertEqual(boundary_calls, [1.5])

        await session.close()

    async def test_close_force_finalizes_an_open_turn_and_calls_on_turn_boundary(self):
        from veya.transcription.turn_detection import TurnDetectionConfig, VoiceActivityDetector

        engine = FakeEngine([])
        emitter = RecordingEmitter()
        boundary_calls: list[float] = []

        async def on_turn_boundary(boundary_time: float) -> None:
            boundary_calls.append(boundary_time)

        vad = VoiceActivityDetector(TurnDetectionConfig(silence_duration_seconds=5.0, min_speech_duration_seconds=0.2))
        session = TranscriptionSession(
            session_id="s1", sample_rate_hz=100, engine=engine, emit_event=emitter,
            run_blocking=immediate_run_blocking, on_turn_boundary=on_turn_boundary, vad=vad,
        )

        await session.handle_chunk(0, 0.0, 0.5, make_loud_pcm(100))
        self.assertEqual(boundary_calls, [])  # no silence endpoint reached yet

        await session.close()
        self.assertEqual(boundary_calls, [0.5])  # trailing open turn flushed at session end

    async def test_partial_transcription_fires_while_speech_is_ongoing_without_waiting_for_a_final_window(self):
        # The actual "real-time, not just partial-capable-UI" fix: partial
        # transcription must genuinely happen — re-transcribing a short
        # trailing window on its own cadence — not just be a field Swift
        # could theoretically render but Python never populates.
        engine = FakeEngine(["partial preview"])
        emitter = RecordingEmitter()
        session = TranscriptionSession(
            session_id="s1", sample_rate_hz=100, engine=engine, emit_event=emitter,
            run_blocking=immediate_run_blocking, partial_window_seconds=0.5, partial_interval_seconds=0.2,
        )

        # One loud chunk covering exactly one partial interval — nowhere
        # near completing a full (4s-equivalent) final window.
        await session.handle_chunk(0, 0.0, 0.2, make_loud_pcm(40))
        await emitter.wait_for_count(2)  # turn.state (speech), transcript.partial

        names = [name for name, _ in emitter.events]
        self.assertIn("transcript.partial", names)
        partial_data = next(data for name, data in emitter.events if name == "transcript.partial")
        self.assertEqual(partial_data["text"], "partial preview")
        self.assertNotIn("transcript.final", names)  # nowhere near a full window yet

        await session.close()

    async def test_partial_transcription_does_not_fire_during_silence(self):
        engine = FakeEngine(["should never be used"])
        emitter = RecordingEmitter()
        session = TranscriptionSession(
            session_id="s1", sample_rate_hz=100, engine=engine, emit_event=emitter,
            run_blocking=immediate_run_blocking, partial_window_seconds=0.5, partial_interval_seconds=0.2,
        )

        await session.handle_chunk(0, 0.0, 0.2, make_pcm(40))  # silent — VAD never enters speech
        await asyncio.sleep(0.05)

        self.assertEqual([name for name, _ in emitter.events], [])
        await session.close()

    async def test_non_speech_marker_from_a_partial_window_never_reaches_swift(self):
        engine = FakeEngine(["[BLANK_AUDIO]"])
        emitter = RecordingEmitter()
        session = TranscriptionSession(
            session_id="s1", sample_rate_hz=100, engine=engine, emit_event=emitter,
            run_blocking=immediate_run_blocking, partial_window_seconds=0.5, partial_interval_seconds=0.2,
        )

        await session.handle_chunk(0, 0.0, 0.2, make_loud_pcm(40))
        await asyncio.sleep(0.05)

        self.assertNotIn("transcript.partial", [name for name, _ in emitter.events])
        await session.close()

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
