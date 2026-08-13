import unittest

from veya.ipc.dispatcher import Dispatcher, WorkerContext
from veya.ipc.errors import ErrorCode, ProtocolError
from veya.ipc.protocol import Request


class RecordingEmitter:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    async def __call__(self, event_name: str, data: dict) -> None:
        self.events.append((event_name, data))


def make_context() -> tuple[WorkerContext, RecordingEmitter]:
    emitter = RecordingEmitter()
    return WorkerContext(emit_event=emitter), emitter


class DispatcherTests(unittest.IsolatedAsyncioTestCase):
    async def test_ping(self):
        context, _ = make_context()
        result = await Dispatcher().dispatch(Request(id="1", method="system.ping", params={}), context)
        self.assertEqual(result, {"pong": True})

    async def test_info_returns_non_sensitive_metadata(self):
        context, _ = make_context()
        result = await Dispatcher().dispatch(Request(id="1", method="system.info", params={}), context)
        self.assertIn("protocol_version", result)
        self.assertIn("worker_version", result)
        self.assertIn("pid", result)

    async def test_unknown_method_is_method_not_found(self):
        context, _ = make_context()
        with self.assertRaises(ProtocolError) as ctx:
            await Dispatcher().dispatch(Request(id="1", method="does.not.exist", params={}), context)
        self.assertEqual(ctx.exception.code, ErrorCode.METHOD_NOT_FOUND)

    async def test_shutdown_sets_shutdown_event_and_cancels_feed(self):
        context, _ = make_context()
        dispatcher = Dispatcher()
        await dispatcher.dispatch(Request(id="1", method="session.start", params={"session_id": "s1"}), context)
        await dispatcher.dispatch(
            Request(id="2", method="mock.start_live_feed", params={"session_id": "s1"}), context
        )
        self.assertIsNotNone(context.feed_task)

        result = await dispatcher.dispatch(Request(id="3", method="worker.shutdown", params={}), context)

        self.assertEqual(result, {"ok": True})
        self.assertTrue(context.shutdown_event.is_set())
        self.assertIsNone(context.feed_task)

    async def test_session_start_requires_session_id(self):
        context, _ = make_context()
        with self.assertRaises(ProtocolError) as ctx:
            await Dispatcher().dispatch(Request(id="1", method="session.start", params={}), context)
        self.assertEqual(ctx.exception.code, ErrorCode.INVALID_PARAMS)

    async def test_session_stop_unknown_session_is_session_not_found(self):
        context, _ = make_context()
        with self.assertRaises(ProtocolError) as ctx:
            await Dispatcher().dispatch(
                Request(id="1", method="session.stop", params={"session_id": "does-not-exist"}), context
            )
        self.assertEqual(ctx.exception.code, ErrorCode.SESSION_NOT_FOUND)

    async def test_start_live_feed_without_active_session_is_session_not_found(self):
        context, _ = make_context()
        with self.assertRaises(ProtocolError) as ctx:
            await Dispatcher().dispatch(
                Request(id="1", method="mock.start_live_feed", params={"session_id": "s1"}), context
            )
        self.assertEqual(ctx.exception.code, ErrorCode.SESSION_NOT_FOUND)

    async def test_start_live_feed_twice_is_already_running(self):
        context, _ = make_context()
        dispatcher = Dispatcher()
        await dispatcher.dispatch(Request(id="1", method="session.start", params={"session_id": "s1"}), context)
        await dispatcher.dispatch(
            Request(id="2", method="mock.start_live_feed", params={"session_id": "s1"}), context
        )
        with self.assertRaises(ProtocolError) as ctx:
            await dispatcher.dispatch(
                Request(id="3", method="mock.start_live_feed", params={"session_id": "s1"}), context
            )
        self.assertEqual(ctx.exception.code, ErrorCode.ALREADY_RUNNING)

        await dispatcher.dispatch(Request(id="4", method="worker.shutdown", params={}), context)

    async def test_stop_live_feed_without_running_is_not_running(self):
        context, _ = make_context()
        with self.assertRaises(ProtocolError) as ctx:
            await Dispatcher().dispatch(
                Request(id="1", method="mock.stop_live_feed", params={"session_id": "s1"}), context
            )
        self.assertEqual(ctx.exception.code, ErrorCode.NOT_RUNNING)

    async def test_unhandled_exception_becomes_internal_error_without_leaking_message(self):
        async def boom(params, context):
            raise ValueError("some sensitive internal detail")

        context, _ = make_context()
        dispatcher = Dispatcher(handlers={"test.boom": boom})
        with self.assertRaises(ProtocolError) as ctx:
            await dispatcher.dispatch(Request(id="1", method="test.boom", params={}), context)
        self.assertEqual(ctx.exception.code, ErrorCode.INTERNAL_ERROR)
        self.assertNotIn("sensitive internal detail", ctx.exception.message)

    async def test_unhandled_exception_does_not_leak_message_to_stderr(self):
        async def boom(params, context):
            raise ValueError("some sensitive internal detail")

        context, _ = make_context()
        dispatcher = Dispatcher(handlers={"test.boom": boom})
        with self.assertLogs("veya.dispatcher", level="ERROR") as logs:
            with self.assertRaises(ProtocolError):
                await dispatcher.dispatch(Request(id="1", method="test.boom", params={}), context)
        logged_text = "\n".join(logs.output)
        self.assertNotIn("sensitive internal detail", logged_text)
        self.assertIn("ValueError", logged_text)

    async def test_llm_status_reports_provider_status_without_raising(self):
        class FakeProvider:
            async def describe_status(self):
                return {"reachable": True, "base_url": "http://localhost:11434", "configured_model": "llama3.2", "model_installed": False, "available_models": ["qwen3:1.7b"], "error": ""}

        context = WorkerContext(emit_event=RecordingEmitter(), llm_provider_factory=FakeProvider)
        result = await Dispatcher().dispatch(Request(id="1", method="system.llm_status", params={}), context)
        self.assertTrue(result["reachable"])
        self.assertFalse(result["model_installed"])
        self.assertEqual(result["configured_model"], "llama3.2")

    async def test_llm_status_never_raises_when_provider_construction_fails(self):
        def failing_factory():
            raise RuntimeError("VEYA_OLLAMA_URL must point at a local host")

        context = WorkerContext(emit_event=RecordingEmitter(), llm_provider_factory=failing_factory)
        result = await Dispatcher().dispatch(Request(id="1", method="system.llm_status", params={}), context)
        self.assertFalse(result["reachable"])

    async def test_session_stop_cancels_running_feed(self):
        context, _ = make_context()
        dispatcher = Dispatcher()
        await dispatcher.dispatch(Request(id="1", method="session.start", params={"session_id": "s1"}), context)
        await dispatcher.dispatch(
            Request(id="2", method="mock.start_live_feed", params={"session_id": "s1"}), context
        )
        self.assertIsNotNone(context.feed_task)

        await dispatcher.dispatch(Request(id="3", method="session.stop", params={"session_id": "s1"}), context)

        self.assertIsNone(context.feed_task)
        self.assertIsNone(context.active_session_id)


if __name__ == "__main__":
    unittest.main()
