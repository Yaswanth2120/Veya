import asyncio
import io
import json
import unittest

from veya.ipc.protocol import Event
from veya.worker import OutputWriter


class FakeStream(io.StringIO):
    """A stream whose `write` yields control briefly, to make interleaving
    bugs in `OutputWriter` actually surface under concurrent writers
    instead of getting lucky with cooperative scheduling."""

    def __init__(self):
        super().__init__()
        self.write_calls = 0

    def write(self, data: str) -> int:
        self.write_calls += 1
        return super().write(data)


class OutputWriterTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_writes_never_interleave(self):
        stream = FakeStream()
        writer = OutputWriter(stream=stream)

        async def emit(i: int) -> None:
            await writer.write(Event(event="transcript.partial", data={"session_id": "s1", "text": f"chunk-{i}"}))

        await asyncio.gather(*(emit(i) for i in range(50)))

        lines = stream.getvalue().splitlines()
        self.assertEqual(len(lines), 50)
        for line in lines:
            # Every line must parse as exactly one complete JSON object —
            # if writes had interleaved, at least one line would fail to
            # parse or would contain fragments of two messages.
            parsed = json.loads(line)
            self.assertEqual(parsed["type"], "event")
            self.assertTrue(parsed["data"]["text"].startswith("chunk-"))

    async def test_each_message_is_written_as_a_single_line(self):
        stream = FakeStream()
        writer = OutputWriter(stream=stream)
        await writer.write(Event(event="session.started", data={"session_id": "s1"}))
        value = stream.getvalue()
        self.assertEqual(value.count("\n"), 1)
        self.assertTrue(value.endswith("\n"))


if __name__ == "__main__":
    unittest.main()
