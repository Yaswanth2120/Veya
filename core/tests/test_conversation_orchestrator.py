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

    async def wait_for_event(self, name: str, timeout: float = 2.0) -> dict:
        """Waits until an event named `name` has been recorded and
        returns its data — robust against how many *other* events
        (`question.candidate`, `answer.draft_*`, etc.) fire before or
        after it, unlike asserting an exact position/count."""
        async def _wait():
            while True:
                match = next((data for n, data in self.events if n == name), None)
                if match is not None:
                    return match
                self._new_event.clear()
                await self._new_event.wait()

        return await asyncio.wait_for(_wait(), timeout=timeout)

    async def wait_for_nth_event(self, name: str, n: int, timeout: float = 2.0) -> dict:
        """Waits until the `n`-th (1-indexed) occurrence of `name` has
        been recorded and returns its data."""
        async def _wait():
            while True:
                matches = [data for evt_name, data in self.events if evt_name == name]
                if len(matches) >= n:
                    return matches[n - 1]
                self._new_event.clear()
                await self._new_event.wait()

        return await asyncio.wait_for(_wait(), timeout=timeout)

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
        await emitter.wait_for_event("answer.completed")

        names = emitter.names()
        self.assertIn("question.detected", names)
        self.assertIn("answer.started", names)
        self.assertIn("answer.delta", names)
        self.assertEqual(names[-1], "answer.completed")
        # A strong prompt like this drafts speculatively before the turn
        # even finalizes (Section 15) — finalize then finds the draft's
        # text already matches and does not restart generation, so only
        # one `question.detected` is emitted despite the earlier candidate.
        self.assertEqual(names.count("question.detected"), 1)
        self.assertEqual(names.count("answer.started"), 1)

        question_data = next(data for name, data in emitter.events if name == "question.detected")
        self.assertEqual(question_data["session_id"], "s1")
        self.assertGreaterEqual(question_data["confidence"], 0.6)
        self.assertIn("question_id", question_data)

        answer_events = [data for name, data in emitter.events if name in ("answer.started", "answer.delta", "answer.completed")]
        sequences = {data["sequence"] for data in answer_events}
        self.assertEqual(sequences, {1})

        completed = next(data for name, data in emitter.events if name == "answer.completed")
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
        # A strong spoken prompt like this now drafts speculatively before
        # the turn finalizes (Section 15) — but it must not be treated as
        # a *finalized* question yet, and later fragments extending it
        # must not start a second, competing draft.
        await asyncio.sleep(0.05)
        self.assertNotIn("question.detected", emitter.names())
        self.assertIn("question.candidate", emitter.names())

        await orchestrator.handle_final_transcript("and what inputs, weighting, and output threshold", 4.0, 8.0)
        await orchestrator.handle_final_transcript("would you use", 8.0, 9.0)
        await asyncio.sleep(0.05)
        self.assertNotIn("question.detected", emitter.names())
        # Extensions of the same evolving prompt must never start a second
        # draft — at most one `answer.draft_started` for the whole turn.
        self.assertEqual(emitter.names().count("answer.draft_started"), 1)

        # A real silence endpoint after the last fragment finalizes it.
        await orchestrator.handle_turn_boundary(9.0)
        detected = await emitter.wait_for_event("question.detected")

        detected_text = detected["text"]
        self.assertIn("deployment risk scoring algorithm", detected_text)
        self.assertIn("inputs, weighting, and output threshold", detected_text)
        self.assertIn("would you use", detected_text)

        await emitter.wait_for_event("answer.completed")
        # The fuller finalized text differs from what the speculative
        # draft was originally started on, so finalize correctly replaces
        # it with one regeneration over the complete question — not two
        # independent, competing answers.
        self.assertEqual(emitter.names().count("answer.completed"), 1)

        await orchestrator.close()

    async def test_fragmented_interview_prompt_starts_an_answer_and_keeps_prior_speech_as_context(self):
        emitter = RecordingEmitter()
        provider = PromptCapturingProvider(["ANSWER: concise answer\nPOINTS:\n- point\n"])
        orchestrator = ConversationOrchestrator(
            session_id="s1", session_context=SessionContext(), emit_event=emitter, llm_provider=provider
        )

        await finalize_turn(orchestrator, "We use a deployment risk score before releases.", 0.0, 4.0)
        await finalize_turn(orchestrator, "Q1, explain the deployment risk scoring algorithm", 4.0, 8.0)
        detected = await emitter.wait_for_event("question.detected")
        await emitter.wait_for_event("answer.started")

        self.assertEqual(detected["text"], "Q1, explain the deployment risk scoring algorithm")
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
        # A strong spoken prompt drafts speculatively (Section 15) even
        # though no real turn boundary has been reached yet — but it must
        # not be treated as finalized.
        self.assertNotIn("question.detected", emitter.names())

        await orchestrator.handle_final_transcript("and its eviction policy", 4.0, 8.0)
        await asyncio.sleep(0.05)
        self.assertNotIn("question.detected", emitter.names())  # boundary at 100.0 still not reached

        await orchestrator.handle_turn_boundary(8.0)
        detected = await emitter.wait_for_event("question.detected")
        self.assertIn("eviction policy", detected["text"])

        await orchestrator.close()

    async def test_session_stop_flushes_the_final_pending_turn(self):
        emitter = RecordingEmitter()
        provider = PromptCapturingProvider(["ANSWER: ok\nPOINTS:\n- a\n"])
        orchestrator = ConversationOrchestrator(
            session_id="s1", session_context=SessionContext(), emit_event=emitter, llm_provider=provider
        )

        # Speech arrives but no silence endpoint/turn boundary is ever
        # reported before the session ends. A strong prompt like this
        # drafts speculatively (Section 15), but must not be treated as
        # finalized until a real endpoint (here, session close) confirms it.
        await orchestrator.handle_final_transcript("Tell me about yourself", 0.0, 3.0)
        await asyncio.sleep(0.05)
        self.assertNotIn("question.detected", emitter.names())

        await orchestrator.close()
        await emitter.wait_for_event("question.detected")

    async def test_a_new_question_cancels_a_still_running_previous_answer(self):
        emitter = RecordingEmitter()
        provider = SlowFakeProvider(["chunk-1", "chunk-2", "chunk-3", "chunk-4", "chunk-5"], delay=0.05)
        orchestrator = ConversationOrchestrator(
            session_id="s1", session_context=SessionContext(), emit_event=emitter, llm_provider=provider
        )

        await finalize_turn(orchestrator, "Why did the first thing happen?", 0.0, 4.0)
        await emitter.wait_for_event("answer.started")  # sequence 1 actually started generating

        await finalize_turn(orchestrator, "How did the second thing happen?", 4.0, 8.0)
        await emitter.wait_for_nth_event("answer.started", 2)

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

    async def test_a_strong_prompt_is_answered_even_when_no_vad_boundary_ever_arrives(self):
        # A review found that a real, obviously-complete interview prompt
        # ("Tell me about yourself.") could sit unanswered indefinitely if
        # continuous background noise/an interviewer who keeps talking/an
        # RMS-threshold merge meant VAD's 1.2s silence endpoint never
        # arrived — `handle_turn_boundary` is simply never called here,
        # exactly reproducing that condition. The strong-prompt debounce
        # must still answer it without any VAD boundary at all.
        emitter = RecordingEmitter()
        provider = SlowFakeProvider(["ANSWER: about me\nPOINTS:\n- a point\n"])
        orchestrator = ConversationOrchestrator(
            session_id="s1", session_context=SessionContext(), emit_event=emitter, llm_provider=provider
        )

        await orchestrator.handle_final_transcript("Tell me about yourself.", 0.0, 3.0)
        # No handle_turn_boundary call anywhere in this test.
        detected = await emitter.wait_for_event("question.detected", timeout=3.0)
        self.assertEqual(detected["text"], "Tell me about yourself.")
        await orchestrator.close()

    async def test_speculative_debounce_is_reset_by_a_new_fragment_extending_the_same_turn(self):
        # A strong prompt that keeps being extended by further speech
        # (still no VAD boundary) must not be cut off mid-thought by the
        # debounce firing on stale, incomplete text.
        emitter = RecordingEmitter()
        provider = PromptCapturingProvider(["ANSWER: ok\nPOINTS:\n- a\n"])
        orchestrator = ConversationOrchestrator(
            session_id="s1", session_context=SessionContext(), emit_event=emitter, llm_provider=provider
        )

        await orchestrator.handle_final_transcript("What was the specific bottleneck", 0.0, 2.0)
        await asyncio.sleep(0.4)  # well under the debounce window
        self.assertNotIn("question.detected", emitter.names())
        self.assertEqual(emitter.names().count("answer.draft_started"), 1)  # drafted speculatively, exactly once
        await orchestrator.handle_final_transcript("causing latency in the checkout service", 2.0, 4.0)

        detected = await emitter.wait_for_event("question.detected", timeout=3.0)
        self.assertIn("bottleneck", detected["text"])
        self.assertIn("checkout service", detected["text"])
        # A pure extension of the same evolving prompt must never start a
        # second, competing draft.
        self.assertEqual(emitter.names().count("answer.draft_started"), 1)
        await orchestrator.close()

    async def test_provider_failure_mid_stream_still_emits_a_completed_event_so_ui_never_hangs(self):
        emitter = RecordingEmitter()
        orchestrator = ConversationOrchestrator(
            session_id="s1", session_context=SessionContext(), emit_event=emitter, llm_provider=FailingProvider()
        )

        await finalize_turn(orchestrator, "Why did this fail?", 0.0, 4.0)
        completed = await emitter.wait_for_event("answer.completed")
        self.assertTrue(completed["answer_text"])  # a status message, not empty/hung
        self.assertEqual(completed["sources"], [])

        await orchestrator.close()


class PartialTranscriptDrivenDraftingTests(unittest.IsolatedAsyncioTestCase):
    """Section 15B: `handle_partial_transcript` — the real streaming-ASR
    entry point — must be able to start a draft on its own, with no
    `transcript.final`/VAD boundary ever involved."""

    async def test_a_partial_hypothesis_alone_starts_a_draft_with_no_final_or_boundary(self):
        emitter = RecordingEmitter()
        provider = SlowFakeProvider(["ANSWER: about me\nPOINTS:\n- a point\n"])
        orchestrator = ConversationOrchestrator(
            session_id="s1", session_context=SessionContext(), emit_event=emitter, llm_provider=provider
        )

        await orchestrator.handle_partial_transcript("Tell me about yourself", 2.0)

        names = emitter.names()
        self.assertEqual(names[0], "question.candidate")
        self.assertIn("answer.draft_started", names)
        self.assertNotIn("question.detected", names)
        self.assertNotIn("question.finalized", names)
        await emitter.wait_for_event("answer.draft_delta")
        await orchestrator.close()

    async def test_walk_me_through_your_resume_also_drafts_from_a_partial_alone(self):
        emitter = RecordingEmitter()
        provider = SlowFakeProvider(["ANSWER: resume walkthrough\nPOINTS:\n- a point\n"])
        orchestrator = ConversationOrchestrator(
            session_id="s1", session_context=SessionContext(), emit_event=emitter, llm_provider=provider
        )

        await orchestrator.handle_partial_transcript("Walk me through your resume", 2.0)
        self.assertIn("answer.draft_started", emitter.names())
        self.assertNotIn("question.detected", emitter.names())
        await orchestrator.close()

    async def test_a_partial_extension_updates_the_same_candidate_without_a_duplicate_draft(self):
        emitter = RecordingEmitter()
        provider = PromptCapturingProvider(["ANSWER: ok\nPOINTS:\n- a\n"])
        orchestrator = ConversationOrchestrator(
            session_id="s1", session_context=SessionContext(), emit_event=emitter, llm_provider=provider
        )

        await orchestrator.handle_partial_transcript("What was the bottleneck causing latency", 2.0)
        await orchestrator.handle_partial_transcript("What was the bottleneck causing latency in your", 3.0)
        await orchestrator.handle_partial_transcript(
            "What was the bottleneck causing latency in your YOLOv5 inference pipeline", 4.0
        )

        names = emitter.names()
        self.assertEqual(names.count("answer.draft_started"), 1)
        self.assertEqual(names.count("answer.draft_replaced"), 0)
        self.assertIn("question.updated", names)
        await orchestrator.close()

    async def test_a_materially_different_partial_replaces_the_draft_not_duplicates_it(self):
        emitter = RecordingEmitter()
        provider = PromptCapturingProvider(["ANSWER: ok\nPOINTS:\n- a\n"])
        orchestrator = ConversationOrchestrator(
            session_id="s1", session_context=SessionContext(), emit_event=emitter, llm_provider=provider
        )

        await orchestrator.handle_partial_transcript("Tell me about yourself", 2.0)
        await orchestrator.handle_partial_transcript("What time is the meeting tomorrow", 4.0)
        await emitter.wait_for_nth_event("answer.completed", 1)

        names = emitter.names()
        self.assertEqual(names.count("answer.draft_started"), 1)
        self.assertEqual(names.count("answer.draft_replaced"), 1)
        # Exactly one answer ever completes — the replacement's, not a
        # leftover from the superseded first draft (which may not even
        # have started streaming before being cancelled).
        self.assertEqual(names.count("answer.completed"), 1)
        self.assertIn("meeting", provider.prompts[-1])
        await orchestrator.close()

    async def test_a_final_hypothesis_reconciles_with_an_active_partial_driven_draft_without_a_wasted_regeneration(self):
        emitter = RecordingEmitter()
        provider = PromptCapturingProvider(["ANSWER: ok\nPOINTS:\n- a\n"])
        orchestrator = ConversationOrchestrator(
            session_id="s1", session_context=SessionContext(), emit_event=emitter, llm_provider=provider
        )

        await orchestrator.handle_partial_transcript("Tell me about yourself", 2.0)
        await emitter.wait_for_event("answer.draft_started")

        await orchestrator.handle_final_transcript("Tell me about yourself", 0.0, 3.0)
        await orchestrator.handle_turn_boundary(3.0)
        detected = await emitter.wait_for_event("question.finalized")
        await emitter.wait_for_event("answer.completed")

        self.assertEqual(detected["text"], "Tell me about yourself")
        # The final text (once normalized) matches what the draft was
        # already generated from — no second generation round.
        self.assertEqual(emitter.names().count("answer.started"), 1)
        self.assertEqual(emitter.names().count("answer.draft_replaced"), 0)
        await orchestrator.close()

    async def test_a_final_hypothesis_with_materially_more_text_reconciles_via_one_replacement(self):
        emitter = RecordingEmitter()
        provider = PromptCapturingProvider(["ANSWER: ok\nPOINTS:\n- a\n"])
        orchestrator = ConversationOrchestrator(
            session_id="s1", session_context=SessionContext(), emit_event=emitter, llm_provider=provider
        )

        await orchestrator.handle_partial_transcript("What was the bottleneck", 2.0)
        await emitter.wait_for_event("answer.draft_started")

        await orchestrator.handle_final_transcript(
            "What was the bottleneck causing latency in your YOLOv5 inference pipeline?", 0.0, 4.0
        )
        await orchestrator.handle_turn_boundary(4.0)
        await emitter.wait_for_event("question.finalized")
        await emitter.wait_for_event("answer.completed")

        names = emitter.names()
        self.assertEqual(names.count("answer.draft_started"), 1)
        self.assertEqual(names.count("answer.draft_replaced"), 1)  # the finalize-triggered refinement
        self.assertEqual(names.count("answer.completed"), 1)  # never two competing answers
        await orchestrator.close()

    async def test_repeated_identical_partials_do_not_duplicate_anything(self):
        emitter = RecordingEmitter()
        provider = SlowFakeProvider(["ANSWER: ok\nPOINTS:\n- a\n"], delay=0.01)
        orchestrator = ConversationOrchestrator(
            session_id="s1", session_context=SessionContext(), emit_event=emitter, llm_provider=provider
        )

        await orchestrator.handle_partial_transcript("Tell me about yourself", 2.0)
        await orchestrator.handle_partial_transcript("Tell me about yourself", 2.5)  # identical text, re-sent
        await orchestrator.handle_partial_transcript("Tell me about yourself", 3.0)

        self.assertEqual(emitter.names().count("answer.draft_started"), 1)
        self.assertEqual(emitter.names().count("question.candidate"), 1)
        await orchestrator.close()

    async def test_ordinary_speech_via_partial_never_starts_a_draft_or_is_remembered(self):
        emitter = RecordingEmitter()
        provider = PromptCapturingProvider()
        orchestrator = ConversationOrchestrator(
            session_id="s1", session_context=SessionContext(), emit_event=emitter, llm_provider=provider
        )

        await orchestrator.handle_partial_transcript("we moved the auth service first", 2.0)
        await asyncio.sleep(0.05)

        self.assertEqual(emitter.events, [])
        # Never persisted into conversation memory unless/until finalized.
        self.assertEqual(orchestrator._recent_transcript_fragments, [])
        await orchestrator.close()

    async def test_an_unfinalized_partial_candidate_is_never_persisted_as_conversation_context(self):
        emitter = RecordingEmitter()
        provider = PromptCapturingProvider(["ANSWER: ok\nPOINTS:\n- a\n"])
        orchestrator = ConversationOrchestrator(
            session_id="s1", session_context=SessionContext(), emit_event=emitter, llm_provider=provider
        )

        # A strong partial starts a draft, but the turn never finalizes
        # (e.g. the interviewer trails off / the session simply moves on).
        await orchestrator.handle_partial_transcript("Tell me about yourself", 2.0)
        await emitter.wait_for_event("answer.draft_started")
        self.assertEqual(orchestrator._recent_transcript_fragments, [])
        await orchestrator.close()


class MultiTrackInterviewAudioTests(unittest.IsolatedAsyncioTestCase):
    """Section 16: dual-input interview audio. `source="meeting_audio"` is
    the interviewer channel, `source="microphone"` is the user's own
    channel (in separated-track mode); `source="mixed"` (the default) is
    today's single-track behavior, exercised exhaustively elsewhere in
    this file and deliberately left untouched by all of these tests."""

    async def test_meeting_audio_prompt_creates_a_candidate_and_a_draft(self):
        emitter = RecordingEmitter()
        provider = SlowFakeProvider(["ANSWER: about me\nPOINTS:\n"])
        orchestrator = ConversationOrchestrator(
            session_id="s1", session_context=SessionContext(), emit_event=emitter, llm_provider=provider
        )
        await orchestrator.handle_final_transcript("Tell me about yourself", 0.0, 3.0, source="meeting_audio")
        await orchestrator.handle_turn_boundary(3.0, source="meeting_audio")
        detected = await emitter.wait_for_event("question.detected")
        self.assertEqual(detected["text"], "Tell me about yourself")
        await orchestrator.close()

    async def test_microphone_speech_in_separated_mode_never_creates_a_draft(self):
        emitter = RecordingEmitter()
        provider = PromptCapturingProvider()
        orchestrator = ConversationOrchestrator(
            session_id="s1", session_context=SessionContext(), emit_event=emitter, llm_provider=provider
        )
        # Even an objectively strong-prompt-shaped sentence, spoken by the
        # *user*, must never be treated as something to answer.
        await orchestrator.handle_final_transcript("Tell me about yourself", 0.0, 3.0, source="microphone")
        await orchestrator.handle_turn_boundary(3.0, source="microphone")
        await asyncio.sleep(0.05)

        self.assertEqual(emitter.events, [])
        await orchestrator.close()

    async def test_microphone_speech_in_separated_mode_updates_authoritative_context(self):
        emitter = RecordingEmitter()
        provider = PromptCapturingProvider()
        orchestrator = ConversationOrchestrator(
            session_id="s1", session_context=SessionContext(), emit_event=emitter, llm_provider=provider
        )
        await orchestrator.handle_final_transcript(
            "I profiled YOLOv5 inference, then used TensorRT and batching", 0.0, 4.0, source="microphone",
        )
        await orchestrator.handle_turn_boundary(4.0, source="microphone")
        await asyncio.sleep(0.05)

        self.assertEqual(orchestrator._recent_user_answer_text, "I profiled YOLOv5 inference, then used TensorRT and batching")
        await orchestrator.close()

    async def test_interviewer_follow_up_grounds_in_the_users_actual_spoken_answer(self):
        emitter = RecordingEmitter()
        provider = PromptCapturingProvider(["ANSWER: ok\nPOINTS:\n"])
        orchestrator = ConversationOrchestrator(
            session_id="s1", session_context=SessionContext(), emit_event=emitter, llm_provider=provider
        )

        await orchestrator.handle_final_transcript("How did you reduce latency?", 0.0, 3.0, source="meeting_audio")
        await orchestrator.handle_turn_boundary(3.0, source="meeting_audio")
        await emitter.wait_for_event("answer.completed")

        await orchestrator.handle_final_transcript(
            "I profiled YOLOv5 inference, then used TensorRT and batching", 3.0, 7.0, source="microphone",
        )
        await orchestrator.handle_turn_boundary(7.0, source="microphone")

        await orchestrator.handle_final_transcript("What was the measured impact?", 7.0, 9.0, source="meeting_audio")
        await orchestrator.handle_turn_boundary(9.0, source="meeting_audio")
        await emitter.wait_for_nth_event("answer.completed", 2)

        self.assertIn("I profiled YOLOv5 inference, then used TensorRT and batching", provider.prompts[-1])
        await orchestrator.close()

    async def test_veyas_own_suggestion_is_never_treated_as_the_users_actual_answer(self):
        emitter = RecordingEmitter()
        provider = PromptCapturingProvider(["ANSWER: I would suggest profiling first.\nPOINTS:\n"])
        orchestrator = ConversationOrchestrator(
            session_id="s1", session_context=SessionContext(), emit_event=emitter, llm_provider=provider
        )

        await orchestrator.handle_final_transcript("How did you reduce latency?", 0.0, 3.0, source="meeting_audio")
        await orchestrator.handle_turn_boundary(3.0, source="meeting_audio")
        await emitter.wait_for_event("answer.completed")

        # The user never actually spoke — Veya's own suggestion text must
        # not have silently become the "authoritative" user answer.
        self.assertIsNone(orchestrator._recent_user_answer_text)
        await orchestrator.close()

    async def test_mixed_mode_im_answering_suppression_prevents_a_draft(self):
        emitter = RecordingEmitter()
        provider = PromptCapturingProvider()
        orchestrator = ConversationOrchestrator(
            session_id="s1", session_context=SessionContext(), emit_event=emitter, llm_provider=provider
        )

        orchestrator.set_user_speaking(True)
        await orchestrator.handle_final_transcript("Tell me about yourself", 0.0, 3.0)  # source="mixed" default
        await orchestrator.handle_turn_boundary(3.0)
        await asyncio.sleep(0.05)

        self.assertEqual(emitter.events, [])
        self.assertEqual(orchestrator._recent_user_answer_text, "Tell me about yourself")
        await orchestrator.close()

    async def test_mixed_mode_without_suppression_behaves_exactly_as_before(self):
        emitter = RecordingEmitter()
        provider = SlowFakeProvider(["ANSWER: about me\nPOINTS:\n"])
        orchestrator = ConversationOrchestrator(
            session_id="s1", session_context=SessionContext(), emit_event=emitter, llm_provider=provider
        )

        orchestrator.set_user_speaking(False)
        await orchestrator.handle_final_transcript("Tell me about yourself", 0.0, 3.0)
        await orchestrator.handle_turn_boundary(3.0)
        detected = await emitter.wait_for_event("question.detected")
        self.assertEqual(detected["text"], "Tell me about yourself")
        await orchestrator.close()

    async def test_toggling_im_answering_off_again_resumes_normal_candidate_tracking(self):
        emitter = RecordingEmitter()
        provider = SlowFakeProvider(["ANSWER: about me\nPOINTS:\n"])
        orchestrator = ConversationOrchestrator(
            session_id="s1", session_context=SessionContext(), emit_event=emitter, llm_provider=provider
        )

        orchestrator.set_user_speaking(True)
        await orchestrator.handle_final_transcript("We shipped it last quarter", 0.0, 3.0)
        await orchestrator.handle_turn_boundary(3.0)
        await asyncio.sleep(0.05)
        self.assertEqual(emitter.events, [])

        orchestrator.set_user_speaking(False)
        await orchestrator.handle_final_transcript("Tell me about yourself", 3.0, 6.0)
        await orchestrator.handle_turn_boundary(6.0)
        detected = await emitter.wait_for_event("question.detected")
        self.assertEqual(detected["text"], "Tell me about yourself")
        await orchestrator.close()

    async def test_session_end_flushes_a_trailing_user_answer_into_context_without_a_draft(self):
        emitter = RecordingEmitter()
        provider = PromptCapturingProvider()
        orchestrator = ConversationOrchestrator(
            session_id="s1", session_context=SessionContext(), emit_event=emitter, llm_provider=provider
        )

        # No VAD boundary ever arrives for the user's trailing answer.
        await orchestrator.handle_final_transcript("and that fixed the bottleneck", 0.0, 3.0, source="microphone")
        await orchestrator.close()

        self.assertEqual(orchestrator._recent_user_answer_text, "and that fixed the bottleneck")
        self.assertEqual(emitter.events, [])  # never generated an answer for the user's own speech


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
        detected = await emitter.wait_for_event("question.detected")
        # Sub-threshold on its own, so it's tracked as a candidate but
        # never drafted speculatively — classification is what confirms it.
        self.assertIn("question.candidate", emitter.names())
        self.assertNotIn("answer.draft_started", emitter.names())
        self.assertIn("question.classifying", emitter.names())
        self.assertEqual(detected["text"], "What would you improve about the caching strategy?")
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
        await emitter.wait_for_event("question.rejected")
        # Sub-threshold on its own — tracked as a candidate, never drafted.
        self.assertNotIn("answer.draft_started", emitter.names())
        self.assertEqual(
            [n for n in emitter.names() if n in ("question.classifying", "question.rejected")],
            ["question.classifying", "question.rejected"],
        )
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
        completed = await emitter.wait_for_event("answer.completed")
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
        completed = await emitter.wait_for_event("answer.completed")
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
        completed = await emitter.wait_for_event("answer.completed")
        self.assertEqual(completed["sources"], [])

        await orchestrator.close()

    async def test_no_retriever_configured_means_no_sources(self):
        emitter = RecordingEmitter()
        provider = PromptCapturingProvider()
        orchestrator = ConversationOrchestrator(
            session_id="s1", session_context=SessionContext(), emit_event=emitter, llm_provider=provider, retriever=None
        )

        await finalize_turn(orchestrator, "Why did the migration take six weeks?", 0.0, 4.0)
        completed = await emitter.wait_for_event("answer.completed")
        self.assertEqual(completed["sources"], [])

        await orchestrator.close()
