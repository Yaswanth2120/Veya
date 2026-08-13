import asyncio
import unittest

from veya.ipc.errors import ProtocolError
from veya.transcription.streaming_provider import ASRHypothesis, StreamingASRProvider
from veya.transcription.streaming_session import StreamingTranscriptionSession


class RecordingEmitter:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []
        self._new_event = asyncio.Event()

    async def __call__(self, event_name: str, data: dict) -> None:
        self.events.append((event_name, data))
        self._new_event.set()

    async def wait_for_event(self, name: str, timeout: float = 2.0) -> dict:
        async def _wait():
            while True:
                match = next((data for n, data in self.events if n == name), None)
                if match is not None:
                    return match
                self._new_event.clear()
                await self._new_event.wait()

        return await asyncio.wait_for(_wait(), timeout=timeout)


class FakeStreamingProvider(StreamingASRProvider):
    """Feeds a scripted sequence of hypotheses on `start()`, ignoring
    actual PCM content — exercises `StreamingTranscriptionSession`'s own
    plumbing without a real subprocess or model."""

    is_degraded = False

    def __init__(self, scripted: list[ASRHypothesis]):
        self._scripted = scripted
        self._queue: "asyncio.Queue[ASRHypothesis | None]" = asyncio.Queue()
        self.fed_chunks: list[bytes] = []
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        self.started = True
        for hyp in self._scripted:
            await self._queue.put(hyp)

    async def feed_pcm(self, pcm_s16le: bytes) -> None:
        self.fed_chunks.append(pcm_s16le)

    async def hypotheses(self):
        while True:
            item = await self._queue.get()
            if item is None:
                return
            yield item

    async def stop(self) -> None:
        self.stopped = True
        await self._queue.put(None)


def make_loud_pcm(num_bytes: int) -> bytes:
    import struct

    count = num_bytes // 2
    samples = ([6000, -6000] * ((count + 1) // 2))[:count]
    return struct.pack("<" + "h" * count, *samples)


class StreamingTranscriptionSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_partial_hypothesis_emits_transcript_partial_only(self):
        provider = FakeStreamingProvider([ASRHypothesis(text="hello wor", is_final=False)])
        emitter = RecordingEmitter()
        session = StreamingTranscriptionSession(
            session_id="s1", sample_rate_hz=16000, streaming_provider=provider, emit_event=emitter,
        )
        await session.handle_chunk(0, 0.0, 0.5, make_loud_pcm(1000))
        data = await emitter.wait_for_event("transcript.partial")
        self.assertEqual(data["text"], "hello wor")
        await session.close()

    async def test_a_final_hypothesis_emits_transcript_final_and_calls_the_callback(self):
        provider = FakeStreamingProvider([ASRHypothesis(text="hello world", is_final=True)])
        emitter = RecordingEmitter()
        received: list[tuple[str, float, float]] = []

        async def on_final(text, started_at, ended_at):
            received.append((text, started_at, ended_at))

        session = StreamingTranscriptionSession(
            session_id="s1", sample_rate_hz=16000, streaming_provider=provider, emit_event=emitter,
            on_final_transcript=on_final,
        )
        await session.handle_chunk(0, 0.0, 1.0, make_loud_pcm(1000))
        data = await emitter.wait_for_event("transcript.final")
        self.assertEqual(data["text"], "hello world")
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0][0], "hello world")
        await session.close()

    async def test_pcm_is_forwarded_to_the_provider_not_buffered_into_windows(self):
        provider = FakeStreamingProvider([])
        emitter = RecordingEmitter()
        session = StreamingTranscriptionSession(
            session_id="s1", sample_rate_hz=16000, streaming_provider=provider, emit_event=emitter,
        )
        chunk = make_loud_pcm(100)
        await session.handle_chunk(0, 0.0, 0.1, chunk)
        self.assertEqual(provider.fed_chunks, [chunk])
        await session.close()

    async def test_vad_still_drives_turn_state_independently_of_the_provider(self):
        provider = FakeStreamingProvider([])
        emitter = RecordingEmitter()
        boundaries: list[float] = []

        async def on_boundary(t):
            boundaries.append(t)

        session = StreamingTranscriptionSession(
            session_id="s1", sample_rate_hz=100, streaming_provider=provider, emit_event=emitter,
            on_turn_boundary=on_boundary,
        )
        await session.handle_chunk(0, 0.0, 0.5, make_loud_pcm(100))
        await emitter.wait_for_event("turn.state")
        self.assertEqual((await emitter.wait_for_event("turn.state"))["state"], "speech")
        await session.close()
        # `close()` force-finalizes any open turn.
        self.assertEqual(len(boundaries), 1)

    async def test_out_of_order_sequence_is_rejected(self):
        provider = FakeStreamingProvider([])
        session = StreamingTranscriptionSession(
            session_id="s1", sample_rate_hz=16000, streaming_provider=provider, emit_event=RecordingEmitter(),
        )
        await session.handle_chunk(5, 0.0, 0.1, make_loud_pcm(10))
        with self.assertRaises(ProtocolError):
            await session.handle_chunk(5, 0.1, 0.1, make_loud_pcm(10))
        await session.close()

    async def test_close_stops_the_provider_and_is_a_no_op_if_never_started(self):
        provider = FakeStreamingProvider([])
        session = StreamingTranscriptionSession(
            session_id="s1", sample_rate_hz=16000, streaming_provider=provider, emit_event=RecordingEmitter(),
        )
        await session.close()  # never handled a chunk — must not touch the provider
        self.assertFalse(provider.stopped)

        await session.handle_chunk(0, 0.0, 0.1, make_loud_pcm(10))
        await session.close()
        self.assertTrue(provider.stopped)

    async def test_is_degraded_reflects_the_underlying_provider(self):
        class DegradedProvider(FakeStreamingProvider):
            is_degraded = True

        session = StreamingTranscriptionSession(
            session_id="s1", sample_rate_hz=16000, streaming_provider=DegradedProvider([]), emit_event=RecordingEmitter(),
        )
        self.assertTrue(session.is_degraded)


if __name__ == "__main__":
    unittest.main()
