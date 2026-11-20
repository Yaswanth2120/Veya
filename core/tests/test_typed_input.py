import asyncio
import tempfile
import unittest
from pathlib import Path

from veya.ipc.dispatcher import Dispatcher, WorkerContext
from veya.ipc.errors import ProtocolError
from veya.ipc.protocol import Request
from veya.knowledge.embeddings import FakeEmbeddingProvider
from veya.llm.errors import LLMProviderError


class Provider:
    def __init__(self):
        self.prompts = []

    async def check_availability(self):
        pass

    async def generate_stream(self, prompt, *, timeout):
        self.prompts.append(prompt)
        yield "ANSWER: A grounded test answer.\n"


class TypedInputTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.events = []

        async def emit(name, data):
            self.events.append((name, data))

        self.provider = Provider()
        self.context = WorkerContext(emit_event=emit, llm_provider_factory=lambda: self.provider,
            embedding_provider_factory=FakeEmbeddingProvider,
            knowledge_index_directory=root / "index", memory_database_path=root / "memory.sqlite")
        self.dispatcher = Dispatcher()
        await self.call("session.start", {"session_id": "s1"})

    async def asyncTearDown(self):
        await self.context.close_transcription_session_if_running()
        self.context.close()
        self.tmp.cleanup()

    async def call(self, method, params):
        return await self.dispatcher.dispatch(Request(id="test", method=method, params=params), self.context)

    async def test_typed_question_generates_real_answer_without_whisper_or_mock_feed(self):
        await self.call("answer.ask", {"session_id": "s1", "text": "Explain caching."})
        for _ in range(100):
            if any(n == "answer.completed" for n, _ in self.events):
                break
            await asyncio.sleep(.01)
        names = [n for n, _ in self.events]
        self.assertIn("question.detected", names)
        self.assertIn("answer.speakable_delta", names)
        self.assertIn("answer.completed", names)
        self.assertIsNone(self.context.transcription_session)
        self.assertIsNone(self.context.feed_task)
        await self.call("session.stop", {"session_id": "s1"})
        self.assertIsNone(self.context.conversation_orchestrator)

    async def test_wrong_session_and_oversized_input_are_rejected(self):
        for params in ({"session_id": "other", "text": "Explain caching."},
                       {"session_id": "s1", "text": "x" * 4001},
                       {"session_id": "s1", "text": "   "}):
            with self.assertRaises(ProtocolError):
                await self.call("answer.ask", params)
        self.assertFalse(self.events)

    async def test_typed_followup_retains_prior_question_context(self):
        await self.call("answer.ask", {"session_id": "s1", "text": "Explain Redis caching."})
        await self.context.conversation_orchestrator._active_answer_task
        await self.call("answer.ask", {"session_id": "s1", "text": "How would that expire?"})
        await self.context.conversation_orchestrator._active_answer_task
        self.assertIn("Recent live conversation", self.provider.prompts[-1])
        self.assertIn("Explain Redis caching.", self.provider.prompts[-1])

    async def test_safe_gpu_failure_reaches_answer_panel_without_mislabeling(self):
        class FailedGPU(Provider):
            safe_error_messages = True

            async def generate_stream(self, prompt, *, timeout):
                raise LLMProviderError("GPU endpoint rejected the request (HTTP 404). Check the served model.")
                yield

        self.context.llm_provider_factory = FailedGPU
        await self.call("answer.ask", {"session_id": "s1", "text": "Explain Redis caching."})
        await self.context.conversation_orchestrator._active_answer_task
        failures = [d for name, d in self.events if name == "answer.completed"]
        self.assertTrue(failures[-1]["is_failed"])
        self.assertIn("HTTP 404", failures[-1]["answer_text"])
        self.assertNotIn("local LLM", failures[-1]["answer_text"])

    async def test_meeting_audio_can_start_without_a_microphone_track(self):
        class Engine:
            def transcribe_pcm(self, pcm, rate):
                return "What is a cache?"
        self.context.transcription_engine_factory = Engine
        await self.call("conversation.start_text", {"session_id": "s1"})
        result = await self.call("transcription.start_meeting_audio", {
            "session_id": "s1", "sample_rate_hz": 16000, "channels": 1, "encoding": "pcm_s16le"})
        self.assertTrue(result["ok"])
        self.assertIsNone(self.context.transcription_session)
        self.assertIsNotNone(self.context.meeting_audio_session)
