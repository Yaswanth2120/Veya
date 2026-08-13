import asyncio
import unittest

from veya.mock.live_feed import DEFAULT_SCRIPT, ScriptLine, run_live_feed


class RecordingEmitter:
    def __init__(self):
        self.event_names: list[str] = []
        self.events: list[tuple[str, dict]] = []

    async def __call__(self, event_name: str, data: dict) -> None:
        self.event_names.append(event_name)
        self.events.append((event_name, data))


_FAST_SCRIPT = [
    ScriptLine("hello there", 0.0),
    ScriptLine("why did this happen", 0.0, is_question=True),
]


class MockFeedOrderingTests(unittest.IsolatedAsyncioTestCase):
    async def test_event_order_for_a_script_with_one_question(self):
        emitter = RecordingEmitter()
        await run_live_feed("session-1", emitter, script=_FAST_SCRIPT)

        self.assertEqual(
            emitter.event_names,
            [
                "session.started",
                "transcript.partial",
                "transcript.final",
                "transcript.partial",
                "transcript.final",
                "question.detected",
                "answer.started",
                "answer.delta",
                "answer.delta",
                "answer.delta",
                "answer.delta",
                "answer.completed",
                "session.ended",
            ],
        )

    async def test_all_events_carry_the_session_id(self):
        emitter = RecordingEmitter()
        await run_live_feed("session-42", emitter, script=_FAST_SCRIPT)
        for _, data in emitter.events:
            self.assertEqual(data["session_id"], "session-42")

    async def test_transcript_final_matches_line_text(self):
        emitter = RecordingEmitter()
        await run_live_feed("session-1", emitter, script=_FAST_SCRIPT)
        finals = [data["text"] for name, data in emitter.events if name == "transcript.final"]
        self.assertEqual(finals, ["hello there", "why did this happen"])

    async def test_answer_completed_has_talking_points_and_sources(self):
        emitter = RecordingEmitter()
        await run_live_feed("session-1", emitter, script=_FAST_SCRIPT)
        completed = next(data for name, data in emitter.events if name == "answer.completed")
        self.assertEqual(completed["question"], "why did this happen")
        self.assertTrue(len(completed["talking_points"]) > 0)
        self.assertTrue(len(completed["sources"]) > 0)

    async def test_question_id_is_consistent_across_answer_events(self):
        emitter = RecordingEmitter()
        await run_live_feed("session-1", emitter, script=_FAST_SCRIPT)
        question_id = next(data["question_id"] for name, data in emitter.events if name == "question.detected")
        for name, data in emitter.events:
            if name in ("answer.started", "answer.delta", "answer.completed"):
                self.assertEqual(data["question_id"], question_id)

    async def test_script_with_no_question_never_emits_answer_events(self):
        emitter = RecordingEmitter()
        await run_live_feed("session-1", emitter, script=[ScriptLine("just a statement", 0.0)])
        self.assertNotIn("question.detected", emitter.event_names)
        self.assertNotIn("answer.completed", emitter.event_names)

    def test_default_script_is_well_formed_and_has_exactly_one_question(self):
        self.assertTrue(len(DEFAULT_SCRIPT) > 0)
        question_lines = [line for line in DEFAULT_SCRIPT if line.is_question]
        self.assertEqual(len(question_lines), 1)


class MockFeedCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelling_the_feed_task_stops_further_events(self):
        emitter = RecordingEmitter()
        task = asyncio.create_task(run_live_feed("session-1", emitter, script=_FAST_SCRIPT))

        # Let it emit session.started and begin the first transcript line,
        # then cancel before it can finish the script.
        await asyncio.sleep(0.05)
        task.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertIn("session.started", emitter.event_names)
        self.assertNotIn("session.ended", emitter.event_names)

        events_at_cancellation = list(emitter.event_names)
        await asyncio.sleep(0.2)
        self.assertEqual(emitter.event_names, events_at_cancellation)

    async def test_cancellation_propagates_cleanly_with_no_lingering_task_state(self):
        emitter = RecordingEmitter()
        task = asyncio.create_task(run_live_feed("session-1", emitter, script=_FAST_SCRIPT))
        await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        self.assertTrue(task.cancelled() or task.done())


if __name__ == "__main__":
    unittest.main()
