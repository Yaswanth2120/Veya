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

    def __init__(self, deltas: list[str] = None, delay: float = 0.0):
        self._deltas = deltas or ["ANSWER: ok\nPOINTS:\n- a point\n"]
        self._delay = delay
        self.prompts: list[str] = []

    async def generate_stream(self, prompt, *, timeout):
        self.prompts.append(prompt)
        for delta in self._deltas:
            await asyncio.sleep(self._delay)
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
        self.assertIn("answer.speakable_delta", names)
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

        answer_events = [data for name, data in emitter.events if name in ("answer.started", "answer.speakable_delta", "answer.completed")]
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

    async def test_a_new_question_is_queued_not_cancelled_while_the_previous_answer_is_still_generating(self):
        """Section 17: replaces the old behavior (a newer question
        cancelled whatever was still generating) — the reported failure
        mode. The first answer must reach `answer.completed`, and the
        second must only start generating after it does."""
        emitter = RecordingEmitter()
        provider = SlowFakeProvider(["chunk-1", "chunk-2", "chunk-3", "chunk-4", "chunk-5"], delay=0.03)
        orchestrator = ConversationOrchestrator(
            session_id="s1", session_context=SessionContext(), emit_event=emitter, llm_provider=provider
        )

        await finalize_turn(orchestrator, "Why did the first thing happen?", 0.0, 4.0)
        await emitter.wait_for_event("answer.started")  # sequence 1 actually started generating

        await finalize_turn(orchestrator, "How did the second thing happen?", 4.0, 8.0)
        queued = await emitter.wait_for_event("answer.queued")
        self.assertEqual(queued["queue_position"], 1)
        self.assertEqual(queued["text"], "How did the second thing happen?")

        # The second question must NOT start generating while the first
        # is still in flight.
        self.assertNotIn("answer.dequeued", emitter.names())
        first_started = [d for n, d in emitter.events if n == "answer.started"]
        self.assertEqual(len(first_started), 1)

        first_completed = await emitter.wait_for_event("answer.completed")
        self.assertEqual(first_completed["sequence"], 1)
        self.assertFalse(first_completed["is_failed"])

        await emitter.wait_for_event("answer.dequeued")
        second_started = await emitter.wait_for_nth_event("answer.started", 2)
        self.assertEqual(second_started["sequence"], 2)

        second_completed = await emitter.wait_for_nth_event("answer.completed", 2)
        self.assertEqual(second_completed["sequence"], 2)

        await orchestrator.close()

    async def test_a_third_and_fourth_question_preserve_queue_order_and_bounded_capacity(self):
        emitter = RecordingEmitter()
        provider = SlowFakeProvider(["a", "b"], delay=0.03)
        orchestrator = ConversationOrchestrator(
            session_id="s1", session_context=SessionContext(), emit_event=emitter, llm_provider=provider
        )

        await finalize_turn(orchestrator, "Question one?", 0.0, 1.0)
        await emitter.wait_for_event("answer.started")
        await finalize_turn(orchestrator, "Question two?", 1.0, 2.0)
        await finalize_turn(orchestrator, "Question three?", 2.0, 3.0)
        await finalize_turn(orchestrator, "Question four?", 3.0, 4.0)

        queued_events = [d for n, d in emitter.events if n == "answer.queued"]
        self.assertEqual([e["text"] for e in queued_events], ["Question two?", "Question three?", "Question four?"])
        self.assertEqual([e["queue_position"] for e in queued_events], [1, 2, 3])

        # A fifth, over-capacity question is never silently dropped —
        # it's explicitly reported as not queued.
        await finalize_turn(orchestrator, "Question five?", 4.0, 5.0)
        overflow = await emitter.wait_for_event("answer.queue_overflow")
        self.assertEqual(overflow["text"], "Question five?")
        self.assertNotIn("Question five?", [e["text"] for e in queued_events])

        await orchestrator.close()

    async def test_skip_active_answer_is_never_automatic_but_advances_the_queue_when_called(self):
        emitter = RecordingEmitter()
        provider = SlowFakeProvider(["a", "b", "c", "d", "e"], delay=0.05)
        orchestrator = ConversationOrchestrator(
            session_id="s1", session_context=SessionContext(), emit_event=emitter, llm_provider=provider
        )

        await finalize_turn(orchestrator, "Question one?", 0.0, 1.0)
        await emitter.wait_for_event("answer.started")
        await finalize_turn(orchestrator, "Question two?", 1.0, 2.0)
        await emitter.wait_for_event("answer.queued")

        await orchestrator.skip_active_answer()

        # The skipped answer never completes normally...
        completed_sequences = [d["sequence"] for n, d in emitter.events if n == "answer.completed"]
        self.assertNotIn(1, completed_sequences)
        # ...and the queued question starts right away instead of waiting
        # for the (now-abandoned) first answer.
        await emitter.wait_for_event("answer.dequeued")
        second_started = await emitter.wait_for_nth_event("answer.started", 2)
        self.assertEqual(second_started["sequence"], 2)

        await orchestrator.close()

    async def test_duplicate_finalization_of_the_same_turn_does_not_produce_a_duplicate_answer(self):
        emitter = RecordingEmitter()
        provider = SlowFakeProvider(["ANSWER: ok"], delay=0.01)
        orchestrator = ConversationOrchestrator(
            session_id="s1", session_context=SessionContext(), emit_event=emitter, llm_provider=provider
        )

        text = "Why did the first thing happen?"
        await orchestrator.handle_final_transcript(text, 0.0, 4.0)
        await orchestrator.handle_turn_boundary(4.0)
        # A second, redundant finalize signal for the exact same turn
        # (e.g. a race between a VAD boundary and the speculative-finalize
        # debounce) must be a no-op, not a second answer.
        await orchestrator.handle_turn_boundary(4.0)

        await emitter.wait_for_event("answer.completed")
        await asyncio.sleep(0.05)

        finalized_events = [d for n, d in emitter.events if n == "question.finalized"]
        self.assertEqual(len(finalized_events), 1)
        started_events = [d for n, d in emitter.events if n == "answer.started"]
        self.assertEqual(len(started_events), 1)

        await orchestrator.close()

    async def test_a_material_extension_of_the_open_turn_replaces_only_that_turns_draft(self):
        """A materially different partial for the SAME still-open turn
        replaces its own speculative draft (unchanged, pre-existing
        behavior) — must not be confused with the new queueing behavior
        for genuinely different, already-finalized questions."""
        emitter = RecordingEmitter()
        provider = SlowFakeProvider(["a", "b", "c", "d"], delay=0.03)
        orchestrator = ConversationOrchestrator(
            session_id="s1", session_context=SessionContext(), emit_event=emitter, llm_provider=provider
        )

        await orchestrator.handle_partial_transcript("What was the bottleneck in the pipeline", 1.0)
        draft_started = await emitter.wait_for_event("answer.draft_started")
        first_question_id = draft_started["question_id"]

        await orchestrator.handle_partial_transcript("Tell me about a time you led a project", 2.0)
        draft_replaced = await emitter.wait_for_event("answer.draft_replaced")
        self.assertEqual(draft_replaced["question_id"], first_question_id)

        # Still exactly one question_id in play — never queued behind
        # itself.
        self.assertNotIn("answer.queued", emitter.names())

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
        await emitter.wait_for_event("answer.speakable_draft_delta")
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


class _HangingRetriever:
    """Never resolves within the orchestrator's retrieval timeout —
    lets `test_a_slow_retriever_never_blocks_answer_generation` prove
    generation still starts and completes on a bounded schedule."""

    async def retrieve(self, session_id, query_text):
        await asyncio.sleep(30.0)
        return []

    def build_context_block(self, retrieved):
        return ""


class TurnSchedulingRobustnessTests(unittest.IsolatedAsyncioTestCase):
    """Section 17: latency and failure-handling guarantees around one
    answer generation round, independent of the multi-turn queueing
    behavior covered above."""

    async def test_first_delta_is_emitted_before_answer_completed(self):
        emitter = RecordingEmitter()
        provider = SlowFakeProvider(["ANSWER: ", "The pipeline was", " optimized by batching."], delay=0.02)
        orchestrator = ConversationOrchestrator(
            session_id="s1", session_context=SessionContext(), emit_event=emitter, llm_provider=provider
        )

        await finalize_turn(orchestrator, "Tell me about yourself.", 0.0, 2.0)
        await emitter.wait_for_event("answer.speakable_delta")
        await emitter.wait_for_event("answer.completed")
        # The delta must have arrived strictly before completion, not
        # merely both eventually present in the event log.
        names = emitter.names()
        self.assertLess(names.index("answer.speakable_delta"), names.index("answer.completed"))

        await orchestrator.close()

    async def test_a_slow_retriever_never_blocks_answer_generation(self):
        emitter = RecordingEmitter()
        provider = SlowFakeProvider(["ANSWER: ok"], delay=0.01)
        orchestrator = ConversationOrchestrator(
            session_id="s1", session_context=SessionContext(), emit_event=emitter, llm_provider=provider,
            retriever=_HangingRetriever(),
        )

        await finalize_turn(orchestrator, "Tell me about yourself.", 0.0, 2.0)
        # Bounded well under the retriever's 30s hang — proves the
        # orchestrator's own retrieval timeout, not the test's patience,
        # is what unblocked this.
        completed = await emitter.wait_for_event("answer.completed", timeout=5.0)
        self.assertEqual(completed["sources"], [])
        self.assertFalse(completed["is_failed"])

        await orchestrator.close()

    async def test_timing_diagnostics_are_off_by_default(self):
        emitter = RecordingEmitter()
        provider = SlowFakeProvider(["ANSWER: ok"], delay=0.01)
        orchestrator = ConversationOrchestrator(
            session_id="s1", session_context=SessionContext(), emit_event=emitter, llm_provider=provider
        )

        await finalize_turn(orchestrator, "Tell me about yourself.", 0.0, 2.0)
        await emitter.wait_for_event("answer.completed")
        await asyncio.sleep(0.02)

        self.assertNotIn("answer.timing", emitter.names())
        await orchestrator.close()

    async def test_timing_diagnostics_report_real_ordered_timestamps_when_enabled(self):
        emitter = RecordingEmitter()
        provider = SlowFakeProvider(["ANSWER: ", "ok"], delay=0.02)
        orchestrator = ConversationOrchestrator(
            session_id="s1", session_context=SessionContext(), emit_event=emitter, llm_provider=provider,
            emit_timing_diagnostics=True,
        )

        await finalize_turn(orchestrator, "Tell me about yourself.", 0.0, 2.0)
        timing = await emitter.wait_for_event("answer.timing")

        self.assertLessEqual(timing["stabilized_at"], timing["generation_request_start"])
        self.assertLessEqual(timing["generation_request_start"], timing["first_speakable_char_at"])
        self.assertLessEqual(timing["first_speakable_char_at"], timing["completed_at"])

        await orchestrator.close()

    async def test_generation_failure_emits_a_typed_terminal_state_without_destroying_prior_context(self):
        from veya.llm.errors import LLMProviderError

        class _FailsOnSecondCall:
            def __init__(self):
                self.calls = 0

            async def generate_stream(self, prompt, *, timeout):
                self.calls += 1
                if self.calls == 1:
                    yield "ANSWER: The first answer, spoken naturally."
                    return
                yield "partial before it breaks"
                raise LLMProviderError("provider broke mid-stream")

        emitter = RecordingEmitter()
        provider = _FailsOnSecondCall()
        orchestrator = ConversationOrchestrator(
            session_id="s1", session_context=SessionContext(), emit_event=emitter, llm_provider=provider
        )

        await finalize_turn(orchestrator, "Tell me about yourself.", 0.0, 2.0)
        first_completed = await emitter.wait_for_event("answer.completed")
        self.assertFalse(first_completed["is_failed"])

        await finalize_turn(orchestrator, "What did you mean by that?", 2.0, 4.0)
        second_completed = await emitter.wait_for_nth_event("answer.completed", 2)
        self.assertTrue(second_completed["is_failed"])
        # The prior turn's real answer is still in the event log,
        # untouched by the later failure — Swift's job is to keep
        # rendering it rather than overwrite it with the failure text.
        self.assertIn("The first answer, spoken naturally.", first_completed["answer_text"])

        await orchestrator.close()


class NoiseRejectionTests(unittest.IsolatedAsyncioTestCase):
    """Section 19: non-speech markers and low-quality ASR garbage must
    never create a candidate, a finalized question, a queue entry, or an
    answer — and must never be remembered as conversation context."""

    async def test_a_marker_only_turn_never_creates_a_question_or_answer(self):
        emitter = RecordingEmitter()
        provider = SlowFakeProvider(["ANSWER: ok"], delay=0.01)
        orchestrator = ConversationOrchestrator(
            session_id="s1", session_context=SessionContext(), emit_event=emitter, llm_provider=provider
        )

        await finalize_turn(orchestrator, "[BLANK_AUDIO]", 0.0, 2.0)
        await asyncio.sleep(0.05)

        self.assertEqual(emitter.names(), ["transcript.rejected"])
        rejected = next(data for name, data in emitter.events if name == "transcript.rejected")
        self.assertEqual(rejected["reason"], "transcript_rejected_non_speech_marker")

        await orchestrator.close()

    async def test_mixed_noise_markers_never_create_a_question(self):
        emitter = RecordingEmitter()
        provider = SlowFakeProvider(["ANSWER: ok"], delay=0.01)
        orchestrator = ConversationOrchestrator(
            session_id="s1", session_context=SessionContext(), emit_event=emitter, llm_provider=provider
        )

        await finalize_turn(orchestrator, "(soft music) (wind blowing)", 0.0, 2.0)
        await asyncio.sleep(0.05)

        self.assertNotIn("question.finalized", emitter.names())
        self.assertNotIn("answer.started", emitter.names())

        await orchestrator.close()

    async def test_a_valid_transcript_with_a_literal_non_marker_bracket_is_preserved(self):
        emitter = RecordingEmitter()
        provider = SlowFakeProvider(["ANSWER: The config value was ten.\n"], delay=0.01)
        orchestrator = ConversationOrchestrator(
            session_id="s1", session_context=SessionContext(), emit_event=emitter, llm_provider=provider
        )

        await finalize_turn(orchestrator, "What was the config value (which defaults to ten) set to in production?", 0.0, 3.0)
        completed = await emitter.wait_for_event("answer.completed")
        self.assertFalse(completed["is_failed"])

        await orchestrator.close()

    async def test_repeated_asr_garbage_never_creates_a_question(self):
        emitter = RecordingEmitter()
        provider = SlowFakeProvider(["ANSWER: ok"], delay=0.01)
        orchestrator = ConversationOrchestrator(
            session_id="s1", session_context=SessionContext(), emit_event=emitter, llm_provider=provider
        )

        await finalize_turn(orchestrator, "the the the the the the the", 0.0, 2.0)
        await asyncio.sleep(0.05)

        self.assertNotIn("question.finalized", emitter.names())
        rejected = next(data for name, data in emitter.events if name == "transcript.rejected")
        self.assertEqual(rejected["reason"], "turn_rejected_low_quality")

        await orchestrator.close()


class CompoundQuestionTests(unittest.IsolatedAsyncioTestCase):
    """Section 19: a compound interviewer question must remain exactly
    one finalized turn/question/answer — a natural mid-question pause
    (well under real VAD silence duration) must never be mistaken for
    the end of the turn by the speculative-finalize debounce."""

    async def test_a_pause_at_an_internal_conjunction_does_not_split_the_turn(self):
        from unittest.mock import patch

        emitter = RecordingEmitter()
        provider = SlowFakeProvider(["ANSWER: The bottleneck was the DB, fixed by caching.\n"], delay=0.01)
        orchestrator = ConversationOrchestrator(
            session_id="s1", session_context=SessionContext(), emit_event=emitter, llm_provider=provider
        )

        with patch("veya.conversation.orchestrator._SPECULATIVE_FINALIZE_DEBOUNCE_SECONDS", 0.05):
            # First clause arrives, then a short pause — well under a
            # real VAD silence boundary, but long enough to trip the
            # (patched, fast) speculative-finalize debounce once.
            await orchestrator.handle_final_transcript("What was the bottleneck, and", 0.0, 2.0)
            await asyncio.sleep(0.08)
            # No premature finalize — the dangling "and" must have
            # deferred it.
            self.assertNotIn("question.finalized", emitter.names())

            await orchestrator.handle_final_transcript("how did you reduce the latency from 35% to 20%?", 2.0, 4.5)
            await orchestrator.handle_turn_boundary(4.5)

        completed = await emitter.wait_for_event("answer.completed")
        self.assertFalse(completed["is_failed"])

        finalized_events = [data for name, data in emitter.events if name == "question.finalized"]
        self.assertEqual(len(finalized_events), 1)
        self.assertIn("bottleneck", finalized_events[0]["text"])
        self.assertIn("35%", finalized_events[0]["text"])
        self.assertEqual([name for name, _ in emitter.events].count("answer.started"), 1)

        await orchestrator.close()

    async def test_compound_question_rabbitmq_example_remains_one_question(self):
        emitter = RecordingEmitter()
        provider = SlowFakeProvider(["ANSWER: We failed over to the backup broker.\n"], delay=0.01)
        orchestrator = ConversationOrchestrator(
            session_id="s1", session_context=SessionContext(), emit_event=emitter, llm_provider=provider
        )

        await finalize_turn(
            orchestrator,
            "What happened when RabbitMQ went down, and how did the system recover?",
            0.0, 4.0,
        )
        await emitter.wait_for_event("answer.completed")

        self.assertEqual(len([n for n in emitter.names() if n == "question.finalized"]), 1)
        await orchestrator.close()

    async def test_compound_question_project_role_example_remains_one_question(self):
        emitter = RecordingEmitter()
        provider = SlowFakeProvider(["ANSWER: I led the project end to end.\n"], delay=0.01)
        orchestrator = ConversationOrchestrator(
            session_id="s1", session_context=SessionContext(), emit_event=emitter, llm_provider=provider
        )

        await finalize_turn(
            orchestrator,
            "Tell me about the project, what your role was, and how you measured success.",
            0.0, 4.0,
        )
        await emitter.wait_for_event("answer.completed")

        self.assertEqual(len([n for n in emitter.names() if n == "question.finalized"]), 1)
        await orchestrator.close()

    async def test_compound_question_difficulty_example_remains_one_question(self):
        emitter = RecordingEmitter()
        provider = SlowFakeProvider(["ANSWER: It was difficult because of the tight deadline.\n"], delay=0.01)
        orchestrator = ConversationOrchestrator(
            session_id="s1", session_context=SessionContext(), emit_event=emitter, llm_provider=provider
        )

        await finalize_turn(
            orchestrator,
            "What was difficult, why was it difficult, and what did you do about it?",
            0.0, 4.0,
        )
        await emitter.wait_for_event("answer.completed")

        self.assertEqual(len([n for n in emitter.names() if n == "question.finalized"]), 1)
        await orchestrator.close()

    async def test_two_questions_separated_by_real_endpoint_silence_create_two_ordered_questions(self):
        emitter = RecordingEmitter()
        provider = SlowFakeProvider(["ANSWER: ok"], delay=0.01)
        orchestrator = ConversationOrchestrator(
            session_id="s1", session_context=SessionContext(), emit_event=emitter, llm_provider=provider
        )

        await finalize_turn(orchestrator, "Why did the migration take six weeks?", 0.0, 4.0)
        await emitter.wait_for_event("answer.completed")
        await finalize_turn(orchestrator, "What would you do differently next time?", 4.0, 8.0)
        await emitter.wait_for_nth_event("answer.completed", 2)

        finalized = [data["text"] for name, data in emitter.events if name == "question.finalized"]
        self.assertEqual(len(finalized), 2)
        self.assertIn("six weeks", finalized[0])
        self.assertIn("differently", finalized[1])

        await orchestrator.close()


class AnswerContextIsolationTests(unittest.IsolatedAsyncioTestCase):
    """Section 19: a queued turn's eventual answer prompt must reflect
    only the conversation as of when *that* turn finalized — not other,
    unrelated turns that finalized later while it was still waiting."""

    async def test_a_queued_questions_prompt_excludes_a_later_unrelated_question_that_finalized_while_it_waited(self):
        emitter = RecordingEmitter()
        provider = PromptCapturingProvider(["ANSWER: ", "ok\n"], delay=0.05)
        orchestrator = ConversationOrchestrator(
            session_id="s1", session_context=SessionContext(), emit_event=emitter, llm_provider=provider
        )

        # Q1 starts generating — deliberately slow, so Q2/Q3 both
        # finalize while it's still active.
        await finalize_turn(orchestrator, "Tell me about the RabbitMQ outage you mentioned.", 0.0, 2.0)
        await emitter.wait_for_event("answer.started")

        # Q2 finalizes while Q1 is still active — gets queued.
        await finalize_turn(orchestrator, "What was your phone number for the on-call rotation?", 2.0, 4.0)
        await emitter.wait_for_event("answer.queued")

        # Q3 and Q4, both unrelated, also finalize while Q1 is still
        # active and Q2 is still queued (queue depth 3, within the cap of
        # 3) — Q4 finalizing *after* Q3 means naively excluding only the
        # single most-recently-remembered fragment at dequeue time would
        # still leave Q3's text contaminating Q2's prompt.
        await finalize_turn(orchestrator, "How do you evaluate the YHANA project's success metrics?", 4.0, 6.0)
        await finalize_turn(orchestrator, "What is your favorite pizza topping?", 6.0, 8.0)

        await emitter.wait_for_event("answer.completed")  # Q1 completes
        await emitter.wait_for_nth_event("answer.started", 2)  # Q2 dequeues and starts

        self.assertEqual(len(provider.prompts), 2)
        q2_prompt = provider.prompts[1]
        # Q2's own prompt must never absorb Q3's or Q4's unrelated text
        # just because they finalized while Q2 was waiting in queue.
        self.assertNotIn("YHANA", q2_prompt)
        self.assertNotIn("pizza", q2_prompt)
        # Q1's text is legitimate prior context for Q2.
        self.assertIn("RabbitMQ", q2_prompt)

        await orchestrator.close()


class GroundednessGuardIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """Section 19: the numeric-contradiction/unsupported-claim guard,
    exercised end-to-end through a real generation round."""

    async def test_a_self_contradictory_percentage_never_reaches_the_completed_answer(self):
        emitter = RecordingEmitter()
        provider = SlowFakeProvider(["ANSWER: We reduced it from 35% to 35% by caching.\n"], delay=0.01)
        orchestrator = ConversationOrchestrator(
            session_id="s1", session_context=SessionContext(), emit_event=emitter, llm_provider=provider
        )

        await finalize_turn(orchestrator, "How much did you reduce the latency by?", 0.0, 3.0)
        completed = await emitter.wait_for_event("answer.completed")

        self.assertFalse(completed["is_failed"])
        self.assertNotIn("35% to 35%", completed["answer_text"])
        self.assertEqual(completed["talking_points"], [])

        await orchestrator.close()

    async def test_an_unsupported_specific_percentage_is_replaced_with_a_safe_answer(self):
        emitter = RecordingEmitter()
        provider = SlowFakeProvider(["ANSWER: We improved throughput by exactly 42% after the rewrite.\n"], delay=0.01)
        orchestrator = ConversationOrchestrator(
            session_id="s1", session_context=SessionContext(), emit_event=emitter, llm_provider=provider
        )

        await finalize_turn(orchestrator, "How much did throughput improve after the rewrite?", 0.0, 3.0)
        completed = await emitter.wait_for_event("answer.completed")

        self.assertNotIn("42%", completed["answer_text"])
        self.assertIn("don't have enough verified", completed["answer_text"])

        await orchestrator.close()

    async def test_a_percentage_grounded_in_retrieved_document_context_is_not_flagged(self):
        from veya.knowledge.models import RetrievedChunk, DocumentChunk

        chunk = DocumentChunk(
            chunk_id="c1", document_id="d1", session_id="s1", file_name="resume.txt", chunk_index=0,
            text="Reduced latency from 35% to 20% via caching.", excerpt="Reduced latency from 35% to 20% via caching.",
            char_start=0, char_end=10,
        )

        class _StubRetriever:
            def build_context_block(self, retrieved):
                return "Resume: Reduced latency from 35% to 20% via caching."

            async def retrieve(self, session_id, query_text):
                return [RetrievedChunk(chunk=chunk, score=1.0)]

        emitter = RecordingEmitter()
        provider = SlowFakeProvider(["ANSWER: We reduced latency from 35% to 20% via caching.\n"], delay=0.01)
        orchestrator = ConversationOrchestrator(
            session_id="s1", session_context=SessionContext(), emit_event=emitter, llm_provider=provider,
            retriever=_StubRetriever(),
        )

        await finalize_turn(orchestrator, "How much did you reduce latency by?", 0.0, 3.0)
        completed = await emitter.wait_for_event("answer.completed")

        self.assertIn("35% to 20%", completed["answer_text"])

        await orchestrator.close()
