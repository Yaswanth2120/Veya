import base64
import tempfile
import unittest
from pathlib import Path

from veya.ipc.dispatcher import MAX_AUDIO_CHUNK_BYTES, Dispatcher, WorkerContext
from veya.ipc.errors import ErrorCode, ProtocolError
from veya.ipc.protocol import Request
from veya.knowledge.errors import EmbeddingUnavailableError
from veya.llm.errors import LLMUnavailableError
from veya.transcription.engine import TranscriptionSetupError


class RecordingEmitter:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    async def __call__(self, event_name: str, data: dict) -> None:
        self.events.append((event_name, data))


class FakeEngine:
    def transcribe_pcm(self, pcm_s16le: bytes, sample_rate_hz: int) -> str:
        return "fake transcript"


class _UnavailableLLMProvider:
    """A fake `llm_provider_factory` target — raises immediately, no real
    network I/O, so `transcription.start` tests here never depend on
    whether a real Ollama instance happens to be running."""

    async def check_availability(self) -> None:
        raise LLMUnavailableError("no LLM provider configured for this test")


def _unavailable_embedding_provider_factory():
    """A fake `embedding_provider_factory` target — raises on
    construction, before `WorkerContext` would otherwise lazily create a
    *real* `VectorStore` (touching a real SQLite file) as a side effect of
    `transcription.start` trying to build a retriever. Every test in this
    module that doesn't care about knowledge/retrieval gets this by
    default so it never writes outside a temp directory."""
    raise EmbeddingUnavailableError("no embedding provider configured for this test")


def make_context(
    engine_factory=FakeEngine,
    llm_provider_factory=_UnavailableLLMProvider,
    embedding_provider_factory=_unavailable_embedding_provider_factory,
    knowledge_index_directory: Path = None,
) -> tuple[WorkerContext, RecordingEmitter]:
    emitter = RecordingEmitter()
    return (
        WorkerContext(
            emit_event=emitter,
            transcription_engine_factory=engine_factory,
            llm_provider_factory=llm_provider_factory,
            embedding_provider_factory=embedding_provider_factory,
            knowledge_index_directory=knowledge_index_directory or Path(tempfile.mkdtemp(prefix="veya-test-knowledge-")),
            # `transcription.start` always resolves approved memory (Section
            # 13) — without this override every test here would otherwise
            # open a real SQLite file under the developer's actual
            # Application Support directory.
            memory_database_path=Path(tempfile.mkdtemp(prefix="veya-test-memory-")) / "memory.sqlite",
        ),
        emitter,
    )


def make_pcm_base64(num_bytes: int = 10) -> str:
    return base64.b64encode(b"\x00" * num_bytes).decode("ascii")


class TranscriptionStartTests(unittest.IsolatedAsyncioTestCase):
    async def test_requires_an_active_session(self):
        context, _ = make_context()
        with self.assertRaises(ProtocolError) as ctx:
            await Dispatcher().dispatch(
                Request(
                    id="1",
                    method="transcription.start",
                    params={"session_id": "s1", "sample_rate_hz": 16000, "channels": 1, "encoding": "pcm_s16le"},
                ),
                context,
            )
        self.assertEqual(ctx.exception.code, ErrorCode.SESSION_NOT_FOUND)

    async def test_rejects_non_mono_channels(self):
        context, _ = make_context()
        dispatcher = Dispatcher()
        await dispatcher.dispatch(Request(id="1", method="session.start", params={"session_id": "s1"}), context)
        with self.assertRaises(ProtocolError) as ctx:
            await dispatcher.dispatch(
                Request(
                    id="2",
                    method="transcription.start",
                    params={"session_id": "s1", "sample_rate_hz": 16000, "channels": 2, "encoding": "pcm_s16le"},
                ),
                context,
            )
        self.assertEqual(ctx.exception.code, ErrorCode.INVALID_PARAMS)

    async def test_rejects_unsupported_encoding(self):
        context, _ = make_context()
        dispatcher = Dispatcher()
        await dispatcher.dispatch(Request(id="1", method="session.start", params={"session_id": "s1"}), context)
        with self.assertRaises(ProtocolError) as ctx:
            await dispatcher.dispatch(
                Request(
                    id="2",
                    method="transcription.start",
                    params={"session_id": "s1", "sample_rate_hz": 16000, "channels": 1, "encoding": "pcm_f32le"},
                ),
                context,
            )
        self.assertEqual(ctx.exception.code, ErrorCode.INVALID_PARAMS)

    async def test_engine_factory_failure_becomes_transcription_unavailable(self):
        def failing_factory():
            raise TranscriptionSetupError("whisper binary not configured")

        context, _ = make_context(engine_factory=failing_factory)
        dispatcher = Dispatcher()
        await dispatcher.dispatch(Request(id="1", method="session.start", params={"session_id": "s1"}), context)
        with self.assertRaises(ProtocolError) as ctx:
            await dispatcher.dispatch(
                Request(
                    id="2",
                    method="transcription.start",
                    params={"session_id": "s1", "sample_rate_hz": 16000, "channels": 1, "encoding": "pcm_s16le"},
                ),
                context,
            )
        self.assertEqual(ctx.exception.code, ErrorCode.TRANSCRIPTION_UNAVAILABLE)
        self.assertIsNone(context.transcription_session)

    async def test_starting_twice_is_already_running(self):
        context, _ = make_context()
        dispatcher = Dispatcher()
        await dispatcher.dispatch(Request(id="1", method="session.start", params={"session_id": "s1"}), context)
        await dispatcher.dispatch(
            Request(
                id="2",
                method="transcription.start",
                params={"session_id": "s1", "sample_rate_hz": 16000, "channels": 1, "encoding": "pcm_s16le"},
            ),
            context,
        )
        with self.assertRaises(ProtocolError) as ctx:
            await dispatcher.dispatch(
                Request(
                    id="3",
                    method="transcription.start",
                    params={"session_id": "s1", "sample_rate_hz": 16000, "channels": 1, "encoding": "pcm_s16le"},
                ),
                context,
            )
        self.assertEqual(ctx.exception.code, ErrorCode.ALREADY_RUNNING)
        await context.close_transcription_session_if_running()


class TranscriptionAudioChunkTests(unittest.IsolatedAsyncioTestCase):
    async def _start_session_and_transcription(self, dispatcher: Dispatcher, context: WorkerContext) -> None:
        await dispatcher.dispatch(Request(id="1", method="session.start", params={"session_id": "s1"}), context)
        await dispatcher.dispatch(
            Request(
                id="2",
                method="transcription.start",
                params={"session_id": "s1", "sample_rate_hz": 16000, "channels": 1, "encoding": "pcm_s16le"},
            ),
            context,
        )

    async def test_without_a_transcription_session_is_not_running(self):
        context, _ = make_context()
        with self.assertRaises(ProtocolError) as ctx:
            await Dispatcher().dispatch(
                Request(
                    id="1",
                    method="transcription.audio_chunk",
                    params={
                        "session_id": "s1",
                        "sequence": 0,
                        "started_at_seconds": 0.0,
                        "duration_seconds": 0.5,
                        "audio_base64": make_pcm_base64(),
                    },
                ),
                context,
            )
        self.assertEqual(ctx.exception.code, ErrorCode.NOT_RUNNING)

    async def test_valid_chunk_is_accepted(self):
        context, _ = make_context()
        dispatcher = Dispatcher()
        await self._start_session_and_transcription(dispatcher, context)

        result = await dispatcher.dispatch(
            Request(
                id="3",
                method="transcription.audio_chunk",
                params={
                    "session_id": "s1",
                    "sequence": 0,
                    "started_at_seconds": 0.0,
                    "duration_seconds": 0.5,
                    "audio_base64": make_pcm_base64(),
                },
            ),
            context,
        )
        self.assertEqual(result, {"ok": True})
        await context.close_transcription_session_if_running()

    async def test_invalid_base64_is_invalid_params(self):
        context, _ = make_context()
        dispatcher = Dispatcher()
        await self._start_session_and_transcription(dispatcher, context)

        with self.assertRaises(ProtocolError) as ctx:
            await dispatcher.dispatch(
                Request(
                    id="3",
                    method="transcription.audio_chunk",
                    params={
                        "session_id": "s1",
                        "sequence": 0,
                        "started_at_seconds": 0.0,
                        "duration_seconds": 0.5,
                        "audio_base64": "not valid base64!!!",
                    },
                ),
                context,
            )
        self.assertEqual(ctx.exception.code, ErrorCode.INVALID_PARAMS)
        await context.close_transcription_session_if_running()

    async def test_oversized_chunk_is_rejected(self):
        context, _ = make_context()
        dispatcher = Dispatcher()
        await self._start_session_and_transcription(dispatcher, context)

        oversized = base64.b64encode(b"\x00" * (MAX_AUDIO_CHUNK_BYTES + 1)).decode("ascii")
        with self.assertRaises(ProtocolError) as ctx:
            await dispatcher.dispatch(
                Request(
                    id="3",
                    method="transcription.audio_chunk",
                    params={
                        "session_id": "s1",
                        "sequence": 0,
                        "started_at_seconds": 0.0,
                        "duration_seconds": 0.5,
                        "audio_base64": oversized,
                    },
                ),
                context,
            )
        self.assertEqual(ctx.exception.code, ErrorCode.INVALID_PARAMS)
        await context.close_transcription_session_if_running()

    async def test_out_of_order_sequence_is_rejected(self):
        context, _ = make_context()
        dispatcher = Dispatcher()
        await self._start_session_and_transcription(dispatcher, context)

        params = {
            "session_id": "s1",
            "sequence": 5,
            "started_at_seconds": 0.0,
            "duration_seconds": 0.5,
            "audio_base64": make_pcm_base64(),
        }
        await dispatcher.dispatch(Request(id="3", method="transcription.audio_chunk", params=params), context)

        with self.assertRaises(ProtocolError) as ctx:
            await dispatcher.dispatch(Request(id="4", method="transcription.audio_chunk", params=params), context)
        self.assertEqual(ctx.exception.code, ErrorCode.INVALID_PARAMS)
        await context.close_transcription_session_if_running()

    async def test_missing_required_field_is_invalid_params(self):
        context, _ = make_context()
        dispatcher = Dispatcher()
        await self._start_session_and_transcription(dispatcher, context)

        with self.assertRaises(ProtocolError) as ctx:
            await dispatcher.dispatch(
                Request(
                    id="3",
                    method="transcription.audio_chunk",
                    params={"session_id": "s1", "sequence": 0, "started_at_seconds": 0.0, "duration_seconds": 0.5},
                ),
                context,
            )
        self.assertEqual(ctx.exception.code, ErrorCode.INVALID_PARAMS)
        await context.close_transcription_session_if_running()


class TranscriptionStopTests(unittest.IsolatedAsyncioTestCase):
    async def test_without_a_transcription_session_is_not_running(self):
        context, _ = make_context()
        with self.assertRaises(ProtocolError) as ctx:
            await Dispatcher().dispatch(
                Request(id="1", method="transcription.stop", params={"session_id": "s1"}), context
            )
        self.assertEqual(ctx.exception.code, ErrorCode.NOT_RUNNING)

    async def test_stop_clears_the_transcription_session(self):
        context, _ = make_context()
        dispatcher = Dispatcher()
        await dispatcher.dispatch(Request(id="1", method="session.start", params={"session_id": "s1"}), context)
        await dispatcher.dispatch(
            Request(
                id="2",
                method="transcription.start",
                params={"session_id": "s1", "sample_rate_hz": 16000, "channels": 1, "encoding": "pcm_s16le"},
            ),
            context,
        )
        self.assertIsNotNone(context.transcription_session)

        result = await dispatcher.dispatch(
            Request(id="3", method="transcription.stop", params={"session_id": "s1"}), context
        )
        self.assertEqual(result, {"ok": True})
        self.assertIsNone(context.transcription_session)

    async def test_session_stop_also_closes_an_active_transcription_session(self):
        context, _ = make_context()
        dispatcher = Dispatcher()
        await dispatcher.dispatch(Request(id="1", method="session.start", params={"session_id": "s1"}), context)
        await dispatcher.dispatch(
            Request(
                id="2",
                method="transcription.start",
                params={"session_id": "s1", "sample_rate_hz": 16000, "channels": 1, "encoding": "pcm_s16le"},
            ),
            context,
        )

        await dispatcher.dispatch(Request(id="3", method="session.stop", params={"session_id": "s1"}), context)

        self.assertIsNone(context.transcription_session)


class _AvailableLLMProvider:
    """A fake `llm_provider_factory` target that always reports itself
    available — no real network I/O."""

    async def check_availability(self) -> None:
        return None


class _FastAnswerProvider:
    """A fake `llm_provider_factory` target that both reports itself
    available and answers instantly — no real network I/O, no delay."""

    async def check_availability(self) -> None:
        return None

    async def generate_stream(self, prompt, *, timeout):
        yield "ANSWER: Six weeks, staged rollout.\nPOINTS:\n- staged rollout\n"


class _QuestionEngine:
    """Always transcribes to a real (short) interview question — a
    leading interrogative, so the deterministic gate accepts it without
    needing a real/fake semantic-classification round trip."""

    def transcribe_pcm(self, pcm_s16le: bytes, sample_rate_hz: int) -> str:
        return "Why did the migration take six weeks"


class SessionStopFlushesTrailingTurnIntoARealAnswerTests(unittest.IsolatedAsyncioTestCase):
    """Regression test for a review finding: `close_transcription_session_if_running`
    used to close the `ConversationOrchestrator` *before* `TranscriptionSession`,
    so a turn finalized by `TranscriptionSession.close()`'s own trailing-audio/
    VAD force-finalize flush could start an answer generation nothing then
    waited for — its events could arrive after Swift had already detached
    and be silently dropped. Exercises the exact scenario: the user is
    still mid-speech (VAD never reached a silence endpoint) when the
    session is stopped, through the real `Dispatcher`, asserting the
    resulting answer is actually present in the emitted events by the
    time `transcription.stop` returns — not merely fired-and-forgotten
    in the background."""

    async def test_a_trailing_turn_still_in_speech_at_stop_produces_a_delivered_answer(self):
        context, emitter = make_context(engine_factory=_QuestionEngine, llm_provider_factory=_FastAnswerProvider)
        dispatcher = Dispatcher()
        await dispatcher.dispatch(Request(id="1", method="session.start", params={"session_id": "s1"}), context)
        await dispatcher.dispatch(
            Request(
                id="2", method="transcription.start",
                # A low sample rate keeps one full 4s window well under
                # the per-chunk IPC size cap, so a single chunk both
                # completes a window (real transcript.final) and is loud
                # enough to put VAD into "speech" — deliberately never
                # sending a quiet/silent chunk afterward, so VAD never
                # reaches a silence endpoint on its own.
                params={"session_id": "s1", "sample_rate_hz": 4000, "channels": 1, "encoding": "pcm_s16le"},
            ),
            context,
        )

        import struct

        window_bytes = 4 * 4000 * 2  # RollingWindowConfig's default window_seconds=4.0
        loud_sample = struct.pack("<hh", 6000, -6000)
        loud_pcm = (loud_sample * (window_bytes // 4 + 1))[:window_bytes]
        await dispatcher.dispatch(
            Request(
                id="3", method="transcription.audio_chunk",
                params={
                    "session_id": "s1", "sequence": 0, "started_at_seconds": 0.0, "duration_seconds": 4.0,
                    "audio_base64": base64.b64encode(loud_pcm).decode("ascii"),
                },
            ),
            context,
        )

        # Speech is still "in progress" from VAD's point of view — no
        # silence endpoint has been reached, and no question.detected has
        # fired yet.
        self.assertNotIn("question.detected", [name for name, _ in emitter.events])

        await dispatcher.dispatch(Request(id="4", method="transcription.stop", params={"session_id": "s1"}), context)

        names = [name for name, _ in emitter.events]
        self.assertIn("question.detected", names)
        self.assertIn("answer.completed", names)
        completed = next(data for name, data in emitter.events if name == "answer.completed")
        self.assertIn("staged rollout", completed["talking_points"])


class SessionContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_session_context_is_captured_from_session_start(self):
        context, _ = make_context()
        await Dispatcher().dispatch(
            Request(
                id="1",
                method="session.start",
                params={
                    "session_id": "s1",
                    "title": "Migration Recap",
                    "company": "Acme Corp",
                    "role_or_topic": "Staff Engineer",
                    "session_description": "Q3 recap",
                    "notes": "backend audience",
                    "preferred_answer_style": "concise",
                    "preferred_programming_language": "Swift",
                    "custom_instructions": "keep it short",
                },
            ),
            context,
        )
        self.assertEqual(context.session_context.title, "Migration Recap")
        self.assertEqual(context.session_context.company, "Acme Corp")
        self.assertEqual(context.session_context.role_or_topic, "Staff Engineer")
        self.assertEqual(context.session_context.description, "Q3 recap")
        self.assertEqual(context.session_context.notes, "backend audience")
        self.assertEqual(context.session_context.preferred_answer_style, "concise")
        self.assertEqual(context.session_context.preferred_programming_language, "Swift")
        self.assertEqual(context.session_context.custom_instructions, "keep it short")

    async def test_session_start_with_only_session_id_defaults_every_context_field_to_empty(self):
        context, _ = make_context()
        await Dispatcher().dispatch(Request(id="1", method="session.start", params={"session_id": "s1"}), context)
        self.assertEqual(context.session_context.title, "")
        self.assertEqual(context.session_context.company, "")


class AnswerIntelligenceAvailabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_transcription_start_reports_answer_intelligence_unavailable_when_llm_check_fails(self):
        context, _ = make_context()  # default llm_provider_factory always raises LLMUnavailableError
        dispatcher = Dispatcher()
        await dispatcher.dispatch(Request(id="1", method="session.start", params={"session_id": "s1"}), context)

        result = await dispatcher.dispatch(
            Request(
                id="2",
                method="transcription.start",
                params={"session_id": "s1", "sample_rate_hz": 16000, "channels": 1, "encoding": "pcm_s16le"},
            ),
            context,
        )

        self.assertEqual(result, {"ok": True, "answer_intelligence_available": False})
        await context.close_transcription_session_if_running()

    async def test_transcription_start_reports_answer_intelligence_available_when_llm_check_succeeds(self):
        context, _ = make_context(llm_provider_factory=_AvailableLLMProvider)
        dispatcher = Dispatcher()
        await dispatcher.dispatch(Request(id="1", method="session.start", params={"session_id": "s1"}), context)

        result = await dispatcher.dispatch(
            Request(
                id="2",
                method="transcription.start",
                params={"session_id": "s1", "sample_rate_hz": 16000, "channels": 1, "encoding": "pcm_s16le"},
            ),
            context,
        )

        self.assertEqual(result, {"ok": True, "answer_intelligence_available": True})
        await context.close_transcription_session_if_running()

    async def test_transcription_still_succeeds_when_ollama_is_unavailable(self):
        # The core Section 8 fallback guarantee: Whisper being available
        # is sufficient for transcription.start to succeed — Ollama being
        # down must never turn this into a TRANSCRIPTION_UNAVAILABLE error.
        context, _ = make_context()
        dispatcher = Dispatcher()
        await dispatcher.dispatch(Request(id="1", method="session.start", params={"session_id": "s1"}), context)

        result = await dispatcher.dispatch(
            Request(
                id="2",
                method="transcription.start",
                params={"session_id": "s1", "sample_rate_hz": 16000, "channels": 1, "encoding": "pcm_s16le"},
            ),
            context,
        )
        self.assertTrue(result["ok"])
        self.assertIsNotNone(context.transcription_session)
        await context.close_transcription_session_if_running()


class AnswerCancelTests(unittest.IsolatedAsyncioTestCase):
    async def test_without_an_active_conversation_orchestrator_is_not_running(self):
        context, _ = make_context()
        with self.assertRaises(ProtocolError) as ctx:
            await Dispatcher().dispatch(Request(id="1", method="answer.cancel", params={"session_id": "s1"}), context)
        self.assertEqual(ctx.exception.code, ErrorCode.NOT_RUNNING)

    async def test_cancel_with_no_active_answer_is_a_harmless_no_op(self):
        context, _ = make_context(llm_provider_factory=_AvailableLLMProvider)
        dispatcher = Dispatcher()
        await dispatcher.dispatch(Request(id="1", method="session.start", params={"session_id": "s1"}), context)
        await dispatcher.dispatch(
            Request(
                id="2",
                method="transcription.start",
                params={"session_id": "s1", "sample_rate_hz": 16000, "channels": 1, "encoding": "pcm_s16le"},
            ),
            context,
        )

        result = await dispatcher.dispatch(Request(id="3", method="answer.cancel", params={"session_id": "s1"}), context)

        self.assertEqual(result, {"ok": True})
        await context.close_transcription_session_if_running()

    async def test_wrong_session_id_is_not_running(self):
        context, _ = make_context(llm_provider_factory=_AvailableLLMProvider)
        dispatcher = Dispatcher()
        await dispatcher.dispatch(Request(id="1", method="session.start", params={"session_id": "s1"}), context)
        await dispatcher.dispatch(
            Request(
                id="2",
                method="transcription.start",
                params={"session_id": "s1", "sample_rate_hz": 16000, "channels": 1, "encoding": "pcm_s16le"},
            ),
            context,
        )

        with self.assertRaises(ProtocolError) as ctx:
            await dispatcher.dispatch(Request(id="3", method="answer.cancel", params={"session_id": "wrong-session"}), context)
        self.assertEqual(ctx.exception.code, ErrorCode.NOT_RUNNING)
        await context.close_transcription_session_if_running()

    async def test_worker_shutdown_also_closes_an_active_transcription_session(self):
        context, _ = make_context()
        dispatcher = Dispatcher()
        await dispatcher.dispatch(Request(id="1", method="session.start", params={"session_id": "s1"}), context)
        await dispatcher.dispatch(
            Request(
                id="2",
                method="transcription.start",
                params={"session_id": "s1", "sample_rate_hz": 16000, "channels": 1, "encoding": "pcm_s16le"},
            ),
            context,
        )

        await dispatcher.dispatch(Request(id="3", method="worker.shutdown", params={}), context)

        self.assertIsNone(context.transcription_session)
        self.assertTrue(context.shutdown_event.is_set())
