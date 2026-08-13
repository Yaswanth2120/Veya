import asyncio
import base64
import os
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


# `transcription.start` always resolves approved memory (Section 13),
# lazily opening a real (held-open, `check_same_thread=False`) sqlite
# connection via `WorkerContext.memory_store` — every one of this file's
# ~35 `make_context()` calls exercises that path. Rather than requiring
# every single test to remember `self.addCleanup(context.close)`,
# `make_context` tracks every context it creates here, and
# `tearDownModule` (run once, automatically, after every test in this
# file) closes them all — the systemic fix for the "unclosed database"
# `ResourceWarning`s this file used to produce in bulk.
_created_contexts: list[WorkerContext] = []


def tearDownModule() -> None:
    for context in _created_contexts:
        context.close()
    _created_contexts.clear()


def make_context(
    engine_factory=FakeEngine,
    llm_provider_factory=_UnavailableLLMProvider,
    embedding_provider_factory=_unavailable_embedding_provider_factory,
    knowledge_index_directory: Path = None,
) -> tuple[WorkerContext, RecordingEmitter]:
    emitter = RecordingEmitter()
    context = WorkerContext(
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
    )
    _created_contexts.append(context)
    return context, emitter


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


# A tiny real executable standing in for `whisper-stream-stdin` — real
# subprocess plumbing (argv, stdin/stdout pipes, EOF handling), no real
# Whisper model required. Emits a scripted partial-then-final sequence
# after receiving the first byte of real audio, ignoring content —
# exercises the *real* event chain (Dispatcher -> StreamingTranscriptionSession
# -> ConversationOrchestrator -> QuestionCandidateTracker) end to end,
# with only the ASR subprocess's own transcription output faked.
_FAKE_STREAMING_BINARY_SOURCE = """#!/usr/bin/env python3
import sys, json, time

data = sys.stdin.buffer.read(1)
if not data:
    sys.exit(0)

for text in ["Tell me about", "Tell me about yourself"]:
    print(json.dumps({"type": "partial", "text": text}))
    sys.stdout.flush()
    time.sleep(0.05)

# Drain the rest of stdin (real audio still arriving) without reacting to it.
while sys.stdin.buffer.read(4096):
    pass

print(json.dumps({"type": "final", "text": "Tell me about yourself."}))
sys.stdout.flush()
"""


def _make_fake_streaming_binary(tmp_dir) -> str:
    import stat
    from pathlib import Path

    path = Path(tmp_dir) / "fake-whisper-stream-stdin"
    path.write_text(_FAKE_STREAMING_BINARY_SOURCE)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


class RealStreamingEventChainTests(unittest.IsolatedAsyncioTestCase):
    """Regression test for a review finding: the streaming ASR provider
    emitted `transcript.partial`, but Python routed it *only* to Swift
    display — `QuestionCandidateTracker` ran solely off `transcript.final`,
    so a strong spoken prompt could sit fully transcribed on screen with
    no answer forming. Drives the real `Dispatcher` -> real
    `StreamingTranscriptionSession` -> real `ConversationOrchestrator`
    chain (only the ASR subprocess itself is a small fake binary, not a
    mocked Python class) and asserts the actual required event order:
    transcript.partial -> question.candidate -> answer.draft_started ->
    answer.draft_delta, with no `transcript.final`/VAD boundary involved
    at any point."""

    async def test_a_strong_partial_alone_produces_the_full_draft_event_chain(self):
        with tempfile.TemporaryDirectory(prefix="veya-streaming-chain-") as tmp:
            binary_path = _make_fake_streaming_binary(tmp)
            model_path = Path(tmp) / "fake-model.bin"
            model_path.write_bytes(b"not a real model, never read by the fake binary")

            old_stream_bin = os.environ.get("VEYA_WHISPER_STREAM_BIN")
            old_model = os.environ.get("VEYA_WHISPER_MODEL")
            os.environ["VEYA_WHISPER_STREAM_BIN"] = binary_path
            os.environ["VEYA_WHISPER_MODEL"] = str(model_path)
            try:
                context, emitter = make_context(llm_provider_factory=_FastAnswerProvider)
                dispatcher = Dispatcher()
                await dispatcher.dispatch(Request(id="1", method="session.start", params={"session_id": "s1"}), context)
                start_result = await dispatcher.dispatch(
                    Request(
                        id="2", method="transcription.start",
                        params={"session_id": "s1", "sample_rate_hz": 16000, "channels": 1, "encoding": "pcm_s16le"},
                    ),
                    context,
                )
                self.assertEqual(start_result["asr_provider"], "streaming")

                await dispatcher.dispatch(
                    Request(
                        id="3", method="transcription.audio_chunk",
                        params={
                            "session_id": "s1", "sequence": 0, "started_at_seconds": 0.0, "duration_seconds": 0.5,
                            "audio_base64": make_pcm_base64(1000),
                        },
                    ),
                    context,
                )

                names_at = lambda: [name for name, _ in emitter.events]  # noqa: E731

                deadline = asyncio.get_event_loop().time() + 5.0
                while "answer.draft_delta" not in names_at() and asyncio.get_event_loop().time() < deadline:
                    await asyncio.sleep(0.02)

                names = names_at()
                self.assertIn("transcript.partial", names)
                self.assertIn("question.candidate", names)
                self.assertIn("answer.draft_started", names)
                self.assertIn("answer.draft_delta", names)

                # The exact required ordering, ignoring interleaved
                # unrelated events (turn.state, etc.).
                relevant = [n for n in names if n in ("transcript.partial", "question.candidate", "answer.draft_started", "answer.draft_delta")]
                self.assertEqual(relevant.index("transcript.partial") < relevant.index("question.candidate"), True)
                self.assertEqual(relevant.index("question.candidate") < relevant.index("answer.draft_started"), True)
                self.assertEqual(relevant.index("answer.draft_started") < relevant.index("answer.draft_delta"), True)

                # No `transcript.final`/VAD boundary was needed for any of this.
                self.assertNotIn("transcript.final", names[: names.index("answer.draft_delta") + 1])

                await context.close_transcription_session_if_running()
            finally:
                if old_stream_bin is None:
                    os.environ.pop("VEYA_WHISPER_STREAM_BIN", None)
                else:
                    os.environ["VEYA_WHISPER_STREAM_BIN"] = old_stream_bin
                if old_model is None:
                    os.environ.pop("VEYA_WHISPER_MODEL", None)
                else:
                    os.environ["VEYA_WHISPER_MODEL"] = old_model


class _UserAnswerEngine:
    """Always transcribes to the user's own spoken answer — deliberately
    *question-shaped* text ("Tell me about yourself" is a leading
    interrogative match) to prove separated-track attribution actually
    matters: if this were misattributed as interviewer speech, it would
    trigger a draft."""

    def transcribe_pcm(self, pcm_s16le: bytes, sample_rate_hz: int) -> str:
        return "Tell me about yourself"


class MultiTrackInterviewAudioDispatcherTests(unittest.IsolatedAsyncioTestCase):
    """Section 16: the dispatcher-level meeting-audio track lifecycle and
    source attribution — real `Dispatcher`/`WorkerContext`, real
    `ConversationOrchestrator`, only the ASR engine and LLM provider are
    fakes (matching every other test in this module)."""

    @staticmethod
    async def _wait_for_event(emitter, name: str, timeout: float = 2.0) -> None:
        """The batch-CLI transcription path transcribes a completed window
        on a background consumer task — `handle_chunk` only enqueues it —
        so a real yield is needed before its events exist, not just an
        immediate post-dispatch assertion."""
        deadline = asyncio.get_event_loop().time() + timeout
        while name not in [n for n, _ in emitter.events] and asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(0.02)

    async def _start_both_tracks(self, dispatcher, context, sample_rate_hz=16000):
        await dispatcher.dispatch(Request(id="1", method="session.start", params={"session_id": "s1"}), context)
        await dispatcher.dispatch(
            Request(
                id="2", method="transcription.start",
                params={"session_id": "s1", "sample_rate_hz": sample_rate_hz, "channels": 1, "encoding": "pcm_s16le"},
            ),
            context,
        )
        result = await dispatcher.dispatch(
            Request(
                id="3", method="transcription.start_meeting_audio",
                params={"session_id": "s1", "sample_rate_hz": sample_rate_hz, "channels": 1, "encoding": "pcm_s16le"},
            ),
            context,
        )
        return result

    async def test_meeting_audio_track_requires_microphone_track_first(self):
        context, _ = make_context(llm_provider_factory=_FastAnswerProvider)
        dispatcher = Dispatcher()
        await dispatcher.dispatch(Request(id="1", method="session.start", params={"session_id": "s1"}), context)
        with self.assertRaises(ProtocolError) as ctx:
            await dispatcher.dispatch(
                Request(
                    id="2", method="transcription.start_meeting_audio",
                    params={"session_id": "s1", "sample_rate_hz": 16000, "channels": 1, "encoding": "pcm_s16le"},
                ),
                context,
            )
        self.assertEqual(ctx.exception.code, ErrorCode.NOT_RUNNING)

    async def test_meeting_audio_track_already_running_is_rejected(self):
        context, _ = make_context(llm_provider_factory=_FastAnswerProvider)
        dispatcher = Dispatcher()
        await self._start_both_tracks(dispatcher, context)
        with self.assertRaises(ProtocolError) as ctx:
            await dispatcher.dispatch(
                Request(
                    id="4", method="transcription.start_meeting_audio",
                    params={"session_id": "s1", "sample_rate_hz": 16000, "channels": 1, "encoding": "pcm_s16le"},
                ),
                context,
            )
        self.assertEqual(ctx.exception.code, ErrorCode.ALREADY_RUNNING)
        await context.close_transcription_session_if_running()

    async def test_meeting_audio_chunk_without_an_active_track_is_not_running(self):
        context, _ = make_context()
        dispatcher = Dispatcher()
        await dispatcher.dispatch(Request(id="1", method="session.start", params={"session_id": "s1"}), context)
        with self.assertRaises(ProtocolError) as ctx:
            await dispatcher.dispatch(
                Request(
                    id="2", method="transcription.meeting_audio_chunk",
                    params={
                        "session_id": "s1", "sequence": 0, "started_at_seconds": 0.0, "duration_seconds": 0.5,
                        "audio_base64": make_pcm_base64(),
                    },
                ),
                context,
            )
        self.assertEqual(ctx.exception.code, ErrorCode.NOT_RUNNING)

    async def test_meeting_audio_prompt_is_attributed_interviewer_and_produces_a_question(self):
        context, emitter = make_context(engine_factory=_QuestionEngine, llm_provider_factory=_FastAnswerProvider)
        dispatcher = Dispatcher()
        await self._start_both_tracks(dispatcher, context, sample_rate_hz=4000)

        window_bytes = 4 * 4000 * 2
        await dispatcher.dispatch(
            Request(
                id="5", method="transcription.meeting_audio_chunk",
                params={
                    "session_id": "s1", "sequence": 0, "started_at_seconds": 0.0, "duration_seconds": 4.0,
                    "audio_base64": base64.b64encode(b"\x00" * window_bytes).decode("ascii"),
                },
            ),
            context,
        )
        await self._wait_for_event(emitter, "question.detected")

        names = [n for n, _ in emitter.events]
        self.assertIn("question.detected", names)
        detected = next(d for n, d in emitter.events if n == "question.detected")
        self.assertEqual(detected["text"], "Why did the migration take six weeks")
        # The interviewer-track transcript event itself is honestly tagged.
        final_event = next(d for n, d in emitter.events if n == "transcript.final")
        self.assertEqual(final_event["source"], "meeting_audio")
        self.assertEqual(final_event["speaker_role"], "interviewer")
        await context.close_transcription_session_if_running()

    async def test_microphone_speech_in_separated_mode_is_attributed_user_and_never_produces_a_question(self):
        context, emitter = make_context(engine_factory=_UserAnswerEngine, llm_provider_factory=_FastAnswerProvider)
        dispatcher = Dispatcher()
        await self._start_both_tracks(dispatcher, context, sample_rate_hz=4000)

        window_bytes = 4 * 4000 * 2
        await dispatcher.dispatch(
            Request(
                id="5", method="transcription.audio_chunk",
                params={
                    "session_id": "s1", "sequence": 0, "started_at_seconds": 0.0, "duration_seconds": 4.0,
                    "audio_base64": base64.b64encode(b"\x00" * window_bytes).decode("ascii"),
                },
            ),
            context,
        )
        await self._wait_for_event(emitter, "transcript.final")

        names = [n for n, _ in emitter.events]
        self.assertNotIn("question.detected", names)
        self.assertNotIn("answer.draft_started", names)
        final_event = next(d for n, d in emitter.events if n == "transcript.final")
        self.assertEqual(final_event["source"], "microphone")
        self.assertEqual(final_event["speaker_role"], "user")
        await context.close_transcription_session_if_running()

    async def test_stop_meeting_audio_only_stops_that_track(self):
        context, _ = make_context(llm_provider_factory=_FastAnswerProvider)
        dispatcher = Dispatcher()
        await self._start_both_tracks(dispatcher, context)

        await dispatcher.dispatch(Request(id="6", method="transcription.stop_meeting_audio", params={"session_id": "s1"}), context)
        self.assertIsNone(context.meeting_audio_session)
        self.assertIsNotNone(context.transcription_session)
        self.assertIsNotNone(context.conversation_orchestrator)
        await context.close_transcription_session_if_running()

    async def test_transcription_stop_closes_both_tracks(self):
        context, _ = make_context(llm_provider_factory=_FastAnswerProvider)
        dispatcher = Dispatcher()
        await self._start_both_tracks(dispatcher, context)

        await dispatcher.dispatch(Request(id="6", method="transcription.stop", params={"session_id": "s1"}), context)
        self.assertIsNone(context.meeting_audio_session)
        self.assertIsNone(context.transcription_session)
        self.assertIsNone(context.conversation_orchestrator)

    async def test_set_user_speaking_suppresses_a_mixed_mode_draft(self):
        context, emitter = make_context(engine_factory=_UserAnswerEngine, llm_provider_factory=_FastAnswerProvider)
        dispatcher = Dispatcher()
        await dispatcher.dispatch(Request(id="1", method="session.start", params={"session_id": "s1"}), context)
        await dispatcher.dispatch(
            Request(
                id="2", method="transcription.start",
                params={"session_id": "s1", "sample_rate_hz": 4000, "channels": 1, "encoding": "pcm_s16le"},
            ),
            context,
        )
        await dispatcher.dispatch(
            Request(id="3", method="conversation.set_user_speaking", params={"session_id": "s1", "active": True}),
            context,
        )

        window_bytes = 4 * 4000 * 2
        await dispatcher.dispatch(
            Request(
                id="4", method="transcription.audio_chunk",
                params={
                    "session_id": "s1", "sequence": 0, "started_at_seconds": 0.0, "duration_seconds": 4.0,
                    "audio_base64": base64.b64encode(b"\x00" * window_bytes).decode("ascii"),
                },
            ),
            context,
        )
        await self._wait_for_event(emitter, "transcript.final")

        names = [n for n, _ in emitter.events]
        self.assertNotIn("question.detected", names)
        self.assertNotIn("answer.draft_started", names)
        await context.close_transcription_session_if_running()

    async def test_ordinary_single_track_audio_chunk_is_tagged_mixed_by_default(self):
        # Backward compatibility: a caller that never mentions the
        # meeting-audio track at all (every pre-Section-16 test in this
        # file) still gets a well-formed, honestly-labeled event.
        context, emitter = make_context()
        dispatcher = Dispatcher()
        await dispatcher.dispatch(Request(id="1", method="session.start", params={"session_id": "s1"}), context)
        await dispatcher.dispatch(
            Request(
                id="2", method="transcription.start",
                params={"session_id": "s1", "sample_rate_hz": 4000, "channels": 1, "encoding": "pcm_s16le"},
            ),
            context,
        )
        await dispatcher.dispatch(
            Request(
                id="3", method="transcription.audio_chunk",
                params={
                    "session_id": "s1", "sequence": 0, "started_at_seconds": 0.0, "duration_seconds": 4.0,
                    "audio_base64": base64.b64encode(b"\x00" * (4 * 4000 * 2)).decode("ascii"),
                },
            ),
            context,
        )
        await context.close_transcription_session_if_running()

        final_events = [d for n, d in emitter.events if n == "transcript.final"]
        for data in final_events:
            self.assertEqual(data["source"], "mixed")
            self.assertEqual(data["speaker_role"], "unknown")


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


class _ScreenshotPhrasesEngine:
    """Returns the exact phrases from a real live-session screenshot that
    a review found went unanswered, one per completed Whisper window, in
    order."""

    def __init__(self):
        self._responses = iter([
            "Tell me about yourself.",
            "What was the specific bottleneck causing latency in the checkout service",
        ])

    def transcribe_pcm(self, pcm_s16le: bytes, sample_rate_hz: int) -> str:
        return next(self._responses, "")


class StrongPromptIsAnsweredWithoutAnyVADSilenceOrStopTests(unittest.IsolatedAsyncioTestCase):
    """Regression test for a review finding: a real Live Session screenshot
    showed real transcript text ("Tell me about yourself.", "What was the
    specific bottleneck causing latency...") sitting on screen unanswered.
    The root cause: a finalized transcript only ever reached question
    classification once VAD reported a full silence endpoint — under
    continuous background noise, an interviewer who keeps talking, or an
    RMS threshold that merges speech, that endpoint can simply never
    arrive. This drives the real `Dispatcher` through continuous loud
    audio chunks only — never a quiet chunk, never `transcription.stop`,
    never any turn-boundary signal at all — reproducing the screenshot
    exactly, and asserts an answer is still produced via the strong-prompt
    speculative debounce alone."""

    async def test_a_strong_prompt_is_answered_via_debounce_with_continuous_loud_audio_and_no_stop(self):
        context, emitter = make_context(engine_factory=_ScreenshotPhrasesEngine, llm_provider_factory=_FastAnswerProvider)
        dispatcher = Dispatcher()
        await dispatcher.dispatch(Request(id="1", method="session.start", params={"session_id": "s1"}), context)
        await dispatcher.dispatch(
            Request(
                id="2", method="transcription.start",
                params={"session_id": "s1", "sample_rate_hz": 4000, "channels": 1, "encoding": "pcm_s16le"},
            ),
            context,
        )

        import struct

        window_bytes = 4 * 4000 * 2  # RollingWindowConfig's default window_seconds=4.0
        loud_sample = struct.pack("<hh", 6000, -6000)
        loud_pcm = (loud_sample * (window_bytes // 4 + 1))[:window_bytes]

        for sequence, started_at in enumerate([0.0, 4.0]):
            await dispatcher.dispatch(
                Request(
                    id=str(sequence + 3), method="transcription.audio_chunk",
                    params={
                        "session_id": "s1", "sequence": sequence, "started_at_seconds": started_at, "duration_seconds": 4.0,
                        "audio_base64": base64.b64encode(loud_pcm).decode("ascii"),
                    },
                ),
                context,
            )

        # No silence, no transcription.stop — the deterministic-gate
        # debounce (see orchestrator.py) is the only thing that can
        # finalize the turn here, and it fires ~0.7s after the last new
        # fragment with no further extension. A speculative draft answer
        # can (and here, does) complete *before* that finalize — the two
        # are no longer strictly coupled — so waiting on `answer.completed`
        # alone is not a reliable proxy for "the turn was finalized";
        # `question.detected` is what that actually requires.
        self.assertNotIn("question.detected", [name for name, _ in emitter.events])

        import asyncio

        deadline = asyncio.get_event_loop().time() + 3.0
        while "question.detected" not in [name for name, _ in emitter.events] and asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(0.05)

        names = [name for name, _ in emitter.events]
        self.assertIn("question.detected", names)
        self.assertIn("answer.completed", names)
        detected_text = next(data for name, data in emitter.events if name == "question.detected")["text"]
        self.assertIn("Tell me about yourself", detected_text)
        self.assertIn("bottleneck causing latency", detected_text)

        await context.close_transcription_session_if_running()


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

        self.assertEqual(result, {"ok": True, "answer_intelligence_available": False, "asr_provider": "degraded_batch"})
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

        self.assertEqual(result, {"ok": True, "answer_intelligence_available": True, "asr_provider": "degraded_batch"})
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
