import asyncio
import unittest

from veya.conversation.models import SessionContext
from veya.conversation.orchestrator import ConversationOrchestrator
from veya.llm.errors import LLMProviderError


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

    def names(self) -> list[str]:
        return [name for name, _ in self.events]


async def finalize_turn(orchestrator: ConversationOrchestrator, text: str, started_at: float, ended_at: float) -> None:
    """Test helper representing one complete spoken turn: a single
    `transcript.final` fragment immediately followed by a VAD silence
    endpoint at its own end — the common case most of these tests exercise.
    Fragmented-turn assembly (multiple fragments before one boundary) is
    covered separately in the fragmented-prompt/turn-assembler-specific
    tests below."""
    await orchestrator.handle_final_transcript(text, started_at, ended_at)
    await orchestrator.handle_turn_boundary(ended_at)


class SlowFakeProvider:
    """Yields deltas with a small delay per chunk — enough to give
    `cancel_active_answer()` a real window to interrupt an in-flight
    generation deterministically."""

    def __init__(self, deltas: list[str], delay: float = 0.02):
        self._deltas = deltas
        self._delay = delay

    async def generate_stream(self, prompt, *, timeout):
        for delta in self._deltas:
            await asyncio.sleep(self._delay)
            yield delta


class FailingProvider:
    async def generate_stream(self, prompt, *, timeout):
        yield "partial response before it breaks"
        raise LLMProviderError("provider broke mid-stream")


class PromptCapturingProvider:
    """Records the exact prompt each generation round received — lets
    tests assert whether/how retrieved document context made it into the
    prompt without needing a real LLM."""

    def __init__(self, deltas: list[str] = None):
        self._deltas = deltas or ["ANSWER: ok\nPOINTS:\n- a point\n"]
        self.prompts: list[str] = []

    async def generate_stream(self, prompt, *, timeout):
        self.prompts.append(prompt)
        for delta in self._deltas:
            await asyncio.sleep(0)
            yield delta


class ConversationOrchestratorTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_llm_provider_means_no_question_detection_at_all(self):
        emitter = RecordingEmitter()
        orchestrator = ConversationOrchestrator(
            session_id="s1", session_context=SessionContext(), emit_event=emitter, llm_provider=None
        )
        self.assertFalse(orchestrator.answer_intelligence_available)

        await finalize_turn(orchestrator, "Why did the migration take six weeks?", 0.0, 4.0)
        await asyncio.sleep(0.05)

        self.assertEqual(emitter.events, [])
        await orchestrator.close()

    async def test_non_question_transcript_triggers_nothing(self):
        emitter = RecordingEmitter()
        provider = SlowFakeProvider(["ANSWER: hi\nPOINTS:\n- a\n"])
        orchestrator = ConversationOrchestrator(
            session_id="s1", session_context=SessionContext(), emit_event=emitter, llm_provider=provider
        )
        await finalize_turn(orchestrator, "We moved the auth service first since it mattered a lot to everyone.", 0.0, 4.0)
        await asyncio.sleep(0.05)
        self.assertEqual(emitter.events, [])
        await orchestrator.close()

    async def test_greeting_triggers_nothing(self):
        emitter = RecordingEmitter()
        provider = SlowFakeProvider(["ANSWER: hi\nPOINTS:\n- a\n"])
        orchestrator = ConversationOrchestrator(
            session_id="s1", session_context=SessionContext(), emit_event=emitter, llm_provider=provider
        )
        await finalize_turn(orchestrator, "Hi, thanks for having me today.", 0.0, 2.0)
        await asyncio.sleep(0.05)
        self.assertEqual(emitter.events, [])
        await orchestrator.close()

    async def test_question_triggers_full_event_sequence_with_matching_sequence_numbers(self):
        emitter = RecordingEmitter()
        provider = SlowFakeProvider(["ANSWER: The rollout was staged.\n", "POINTS:\n- staged rollout\n"])
        orchestrator = ConversationOrchestrator(
            session_id="s1", session_context=SessionContext(), emit_event=emitter, llm_provider=provider
        )

        await finalize_turn(orchestrator, "Why did the migration take six weeks?", 0.0, 4.0)
        await emitter.wait_for_count(4)  # question.detected, answer.started, 2x answer.delta, answer.completed

        names = emitter.names()
        self.assertEqual(names[0], "question.detected")
        self.assertEqual(names[1], "answer.started")
        self.assertIn("answer.delta", names)
        self.assertEqual(names[-1], "answer.completed")

        question_data = emitter.events[0][1]
        self.assertEqual(question_data["session_id"], "s1")
        self.assertGreaterEqual(question_data["confidence"], 0.6)
        self.assertIn("question_id", question_data)

        answer_events = [data for name, data in emitter.events if name.startswith("answer.")]
        sequences = {data["sequence"] for data in answer_events}
        self.assertEqual(sequences, {1})

        completed = emitter.events[-1][1]
        self.assertEqual(completed["talking_points"], ["staged rollout"])
        self.assertEqual(completed["sources"], [])

        await orchestrator.close()

    async def test_a_turn_split_across_multiple_fragments_is_assembled_before_detection_runs(self):
        # The exact regression the Section 14 build prompt describes: each
        # fragment judged independently used to either miss the question
        # entirely or fire multiple times on partial fragments. Now the
        # fragments must be assembled into one turn (no boundary between
        # them) before question detection/classification ever sees them.
        emitter = RecordingEmitter()
        provider = PromptCapturingProvider(["ANSWER: concise answer\nPOINTS:\n- point\n"])
        orchestrator = ConversationOrchestrator(
            session_id="s1", session_context=SessionContext(), emit_event=emitter, llm_provider=provider
        )

        await orchestrator.handle_final_transcript("Q1, explain the deployment risk scoring algorithm in DeployGuard AI", 0.0, 4.0)
        await orchestrator.handle_final_transcript("and what inputs, weighting, and output threshold", 4.0, 8.0)
        await orchestrator.handle_final_transcript("would you use", 8.0, 9.0)
        # No question.detected yet — the turn hasn't been finalized.
        await asyncio.sleep(0.05)
        self.assertEqual(emitter.events, [])

        # A real silence endpoint after the last fragment finalizes it.
        await orchestrator.handle_turn_boundary(9.0)
        await emitter.wait_for_count(1)

        self.assertEqual(emitter.events[0][0], "question.detected")
        detected_text = emitter.events[0][1]["text"]
        self.assertIn("deployment risk scoring algorithm", detected_text)
        self.assertIn("inputs, weighting, and output threshold", detected_text)
        self.assertIn("would you use", detected_text)

        await orchestrator.close()

    async def test_fragmented_interview_prompt_starts_an_answer_and_keeps_prior_speech_as_context(self):
        emitter = RecordingEmitter()
        provider = PromptCapturingProvider(["ANSWER: concise answer\nPOINTS:\n- point\n"])
        orchestrator = ConversationOrchestrator(
            session_id="s1", session_context=SessionContext(), emit_event=emitter, llm_provider=provider
        )

        await finalize_turn(orchestrator, "We use a deployment risk score before releases.", 0.0, 4.0)
        await finalize_turn(orchestrator, "Q1, explain the deployment risk scoring algorithm", 4.0, 8.0)
        await emitter.wait_for_count(4)

        self.assertEqual(emitter.events[0][0], "question.detected")
        self.assertEqual(emitter.events[0][1]["text"], "Q1, explain the deployment risk scoring algorithm")
        self.assertIn("We use a deployment risk score before releases.", provider.prompts[0])
        await orchestrator.close()

    async def test_a_brief_pause_mid_turn_does_not_split_one_question(self):
        # A boundary request that arrives *before* the fragment covering it
        # (VAD's fast chunk-level signal outracing the slower ~4s Whisper
        # window) must not prematurely finalize a half-spoken question —
        # only once the fragment reaching that boundary time actually
        # arrives does the turn finalize.
        emitter = RecordingEmitter()
        provider = PromptCapturingProvider(["ANSWER: ok\nPOINTS:\n- a\n"])
        orchestrator = ConversationOrchestrator(
            session_id="s1", session_context=SessionContext(), emit_event=emitter, llm_provider=provider
        )

        # A boundary far in the future than anything received so far must
        # not finalize an empty/partial turn.
        await orchestrator.handle_turn_boundary(100.0)
        await orchestrator.handle_final_transcript("Explain the caching layer", 0.0, 4.0)
        await asyncio.sleep(0.05)
        self.assertEqual(emitter.events, [])

        await orchestrator.handle_final_transcript("and its eviction policy", 4.0, 8.0)
        await asyncio.sleep(0.05)
        self.assertEqual(emitter.events, [])  # boundary at 100.0 still not reached

        await orchestrator.handle_turn_boundary(8.0)
        await emitter.wait_for_count(1)
        self.assertEqual(emitter.events[0][0], "question.detected")
        self.assertIn("eviction policy", emitter.events[0][1]["text"])

        await orchestrator.close()

    async def test_session_stop_flushes_the_final_pending_turn(self):
        emitter = RecordingEmitter()
        provider = PromptCapturingProvider(["ANSWER: ok\nPOINTS:\n- a\n"])
        orchestrator = ConversationOrchestrator(
            session_id="s1", session_context=SessionContext(), emit_event=emitter, llm_provider=provider
        )

        # Speech arrives but no silence endpoint/turn boundary is ever
        # reported before the session ends.
        await orchestrator.handle_final_transcript("Tell me about yourself", 0.0, 3.0)
        await asyncio.sleep(0.05)
        self.assertEqual(emitter.events, [])

        await orchestrator.close()
        await emitter.wait_for_count(1)
        self.assertEqual(emitter.events[0][0], "question.detected")

    async def test_a_new_question_cancels_a_still_running_previous_answer(self):
        emitter = RecordingEmitter()
        provider = SlowFakeProvider(["chunk-1", "chunk-2", "chunk-3", "chunk-4", "chunk-5"], delay=0.05)
        orchestrator = ConversationOrchestrator(
            session_id="s1", session_context=SessionContext(), emit_event=emitter, llm_provider=provider
        )

        await finalize_turn(orchestrator, "Why did the first thing happen?", 0.0, 4.0)
        await emitter.wait_for_count(2)  # question.detected, answer.started for sequence 1

        await finalize_turn(orchestrator, "How did the second thing happen?", 4.0, 8.0)
        await emitter.wait_for_count(4)  # + question.detected, answer.started for sequence 2

        # The first answer never reaches answer.completed — it was
        # superseded, not left to finish in the background.
        first_question_events = [d for n, d in emitter.events if n == "answer.started"]
        self.assertEqual(len(first_question_events), 2)
        self.assertEqual(first_question_events[0]["sequence"], 1)
        self.assertEqual(first_question_events[1]["sequence"], 2)

        completed_sequences = [d["sequence"] for n, d in emitter.events if n == "answer.completed"]
        self.assertNotIn(1, completed_sequences)

        await orchestrator.close()

    async def test_cancel_active_answer_stops_generation_without_emitting_completed(self):
        emitter = RecordingEmitter()
        provider = SlowFakeProvider(["a", "b", "c", "d", "e", "f", "g", "h"], delay=0.03)
        orchestrator = ConversationOrchestrator(
            session_id="s1", session_context=SessionContext(), emit_event=emitter, llm_provider=provider
        )

        await finalize_turn(orchestrator, "Why did this happen?", 0.0, 4.0)
        await emitter.wait_for_count(2)  # question.detected, answer.started

        await orchestrator.cancel_active_answer()
        await asyncio.sleep(0.1)

        names = emitter.names()
        self.assertNotIn("answer.completed", names)

    async def test_provider_failure_mid_stream_still_emits_a_completed_event_so_ui_never_hangs(self):
        emitter = RecordingEmitter()
        orchestrator = ConversationOrchestrator(
            session_id="s1", session_context=SessionContext(), emit_event=emitter, llm_provider=FailingProvider()
        )

        await finalize_turn(orchestrator, "Why did this fail?", 0.0, 4.0)
        await emitter.wait_for_count(3)  # question.detected, answer.started, answer.completed (degraded)

        names = emitter.names()
        self.assertEqual(names[-1], "answer.completed")
        completed = emitter.events[-1][1]
        self.assertTrue(completed["talking_points"])  # a status message, not empty/hung
        self.assertEqual(completed["sources"], [])

        await orchestrator.close()


# A genuinely realistic ambiguous turn under the *default* scoring
# config: a mid-sentence interrogative ("how") that isn't the leading
# word, so it scores via `mid_sentence_interrogative_score` (0.4) alone —
# landing in the classifier's ambiguous band without any custom detector
# configuration. Real spoken interview follow-ups look exactly like this
# ("the caching layer, how does that scale" / "you mentioned retries,
# what's the backoff policy").
_AMBIGUOUS_TEXT = "the caching layer, how does that scale"


class SemanticClassifierFallbackTests(unittest.IsolatedAsyncioTestCase):
    """Ambiguous-band turns (deterministic score neither clearly a
    question nor clearly not) route through the semantic classifier —
    these tests exercise that path's malformed/unavailable/timeout
    fallback behavior end-to-end through the orchestrator."""

    async def test_semantic_classifier_confirms_an_ambiguous_turn(self):
        class JSONProvider:
            async def generate_stream(self, prompt, *, timeout):
                yield '{"is_answer_request": true, "confidence": 0.9, "normalized_question": "What would you improve about the caching strategy?", "reason_category": "follow_up"}'

        emitter = RecordingEmitter()
        orchestrator = ConversationOrchestrator(
            session_id="s1", session_context=SessionContext(), emit_event=emitter,
            llm_provider=JSONProvider(),
        )
        await finalize_turn(orchestrator, _AMBIGUOUS_TEXT, 0.0, 2.0)
        await emitter.wait_for_count(2)  # question.classifying, question.detected
        self.assertEqual(emitter.names()[0], "question.classifying")
        self.assertEqual(emitter.events[1][0], "question.detected")
        self.assertEqual(emitter.events[1][1]["text"], "What would you improve about the caching strategy?")
        await orchestrator.close()

    async def test_malformed_semantic_response_falls_back_safely_without_crashing(self):
        class BrokenJSONProvider:
            async def generate_stream(self, prompt, *, timeout):
                yield "not valid json at all"

        emitter = RecordingEmitter()
        orchestrator = ConversationOrchestrator(
            session_id="s1", session_context=SessionContext(), emit_event=emitter,
            llm_provider=BrokenJSONProvider(),
        )
        await finalize_turn(orchestrator, _AMBIGUOUS_TEXT, 0.0, 2.0)
        await asyncio.sleep(0.2)
        # Falls back to the deterministic gate's own (sub-threshold)
        # verdict — no crash, no question detected from unparseable output.
        self.assertNotIn("question.detected", emitter.names())
        await orchestrator.close()

    async def test_semantic_classifier_timeout_falls_back_safely(self):
        class HangingProvider:
            async def generate_stream(self, prompt, *, timeout):
                await asyncio.sleep(30)
                yield "too late"
                return  # pragma: no cover - unreachable within the classifier's own timeout

        emitter = RecordingEmitter()
        orchestrator = ConversationOrchestrator(
            session_id="s1", session_context=SessionContext(), emit_event=emitter,
            llm_provider=HangingProvider(),
        )
        # `finalize_turn` blocks for the classifier's own bounded timeout
        # (a handful of seconds) before returning — a genuine timeout,
        # not a sleep the test adds on top.
        await asyncio.wait_for(finalize_turn(orchestrator, _AMBIGUOUS_TEXT, 0.0, 2.0), timeout=15)
        self.assertNotIn("question.detected", emitter.names())
        await orchestrator.close()

    async def test_ambiguous_turn_emits_classifying_then_rejected_when_declined(self):
        class RejectingProvider:
            async def generate_stream(self, prompt, *, timeout):
                yield '{"is_answer_request": false, "confidence": 0.8, "normalized_question": "", "reason_category": "not_a_request"}'

        emitter = RecordingEmitter()
        orchestrator = ConversationOrchestrator(
            session_id="s1", session_context=SessionContext(), emit_event=emitter,
            llm_provider=RejectingProvider(),
        )
        await finalize_turn(orchestrator, _AMBIGUOUS_TEXT, 0.0, 2.0)
        await emitter.wait_for_count(2)
        self.assertEqual(emitter.names(), ["question.classifying", "question.rejected"])
        await orchestrator.close()


class GroundedAnswerTests(unittest.IsolatedAsyncioTestCase):
    """Exercises retrieval-grounded answers with a real (in-memory)
    `VectorStore` + `FakeEmbeddingProvider` — no real Ollama, no real
    embeddings, fully deterministic."""

    def setUp(self):
        import tempfile
        from pathlib import Path

        from veya.knowledge.embeddings import FakeEmbeddingProvider
        from veya.knowledge.models import DocumentChunk, IngestionStatus
        from veya.knowledge.retrieval import KnowledgeRetriever
        from veya.knowledge.vector_store import VectorStore

        self._tmp = tempfile.TemporaryDirectory()
        self.store = VectorStore(Path(self._tmp.name) / "knowledge.sqlite")
        self.embedding_provider = FakeEmbeddingProvider()
        self._DocumentChunk = DocumentChunk
        self._IngestionStatus = IngestionStatus
        self._KnowledgeRetriever = KnowledgeRetriever

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    async def _seed_document(self, session_id: str, text: str, document_id: str = "doc1", file_name: str = "notes.txt"):
        chunk = self._DocumentChunk(
            chunk_id=f"{document_id}-0",
            document_id=document_id,
            session_id=session_id,
            file_name=file_name,
            chunk_index=0,
            text=text,
            excerpt=text[:100],
            char_start=0,
            char_end=len(text),
        )
        [embedding] = await self.embedding_provider.embed([text])
        self.store.upsert_document(document_id, session_id, file_name, self._IngestionStatus.READY)
        self.store.replace_chunks(document_id, session_id, file_name, [chunk], [embedding])

    async def test_relevant_retrieved_chunks_appear_as_real_sources_on_the_completed_answer(self):
        from veya.knowledge.models import RetrievalConfig

        await self._seed_document("s1", "The migration took six weeks because of a staged rollout to preserve compatibility.")
        retriever = self._KnowledgeRetriever(self.store, self.embedding_provider, RetrievalConfig(similarity_threshold=0.1))

        emitter = RecordingEmitter()
        provider = PromptCapturingProvider(["ANSWER: staged rollout\nPOINTS:\n- a point\n"])
        orchestrator = ConversationOrchestrator(
            session_id="s1", session_context=SessionContext(), emit_event=emitter, llm_provider=provider, retriever=retriever
        )

        await finalize_turn(orchestrator, "Why did the migration take six weeks?", 0.0, 4.0)
        await emitter.wait_for_count(4)

        completed = emitter.events[-1][1]
        self.assertTrue(completed["sources"])
        self.assertEqual(completed["sources"][0]["document_id"], "doc1")
        self.assertEqual(completed["sources"][0]["file_name"], "notes.txt")

        # The retrieved context actually reached the prompt.
        self.assertIn("notes.txt", provider.prompts[0])

        await orchestrator.close()

    async def test_no_relevant_chunks_means_no_sources_and_ungrounded_prompt(self):
        from veya.knowledge.models import RetrievalConfig

        await self._seed_document("s1", "completely unrelated pizza recipe content with no relation to anything")
        retriever = self._KnowledgeRetriever(self.store, self.embedding_provider, RetrievalConfig(similarity_threshold=0.99))

        emitter = RecordingEmitter()
        provider = PromptCapturingProvider(["ANSWER: no context needed\nPOINTS:\n- a point\n"])
        orchestrator = ConversationOrchestrator(
            session_id="s1", session_context=SessionContext(), emit_event=emitter, llm_provider=provider, retriever=retriever
        )

        await finalize_turn(orchestrator, "Why did the migration take six weeks?", 0.0, 4.0)
        await emitter.wait_for_count(4)

        completed = emitter.events[-1][1]
        self.assertEqual(completed["sources"], [])
        self.assertNotIn("notes.txt", provider.prompts[0])

        await orchestrator.close()

    async def test_retrieval_never_crosses_sessions_in_a_grounded_answer(self):
        from veya.knowledge.models import RetrievalConfig

        await self._seed_document("other-session", "The migration took six weeks because of a staged rollout.")
        retriever = self._KnowledgeRetriever(self.store, self.embedding_provider, RetrievalConfig(similarity_threshold=0.0))

        emitter = RecordingEmitter()
        provider = PromptCapturingProvider()
        orchestrator = ConversationOrchestrator(
            session_id="s1", session_context=SessionContext(), emit_event=emitter, llm_provider=provider, retriever=retriever
        )

        await finalize_turn(orchestrator, "Why did the migration take six weeks?", 0.0, 4.0)
        await emitter.wait_for_count(4)

        completed = emitter.events[-1][1]
        self.assertEqual(completed["sources"], [])

        await orchestrator.close()

    async def test_no_retriever_configured_means_no_sources(self):
        emitter = RecordingEmitter()
        provider = PromptCapturingProvider()
        orchestrator = ConversationOrchestrator(
            session_id="s1", session_context=SessionContext(), emit_event=emitter, llm_provider=provider, retriever=None
        )

        await finalize_turn(orchestrator, "Why did the migration take six weeks?", 0.0, 4.0)
        await emitter.wait_for_count(4)

        completed = emitter.events[-1][1]
        self.assertEqual(completed["sources"], [])

        await orchestrator.close()
