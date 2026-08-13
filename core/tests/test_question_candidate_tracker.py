import unittest

from veya.conversation.question_candidate_tracker import CandidateState, QuestionCandidateTracker
from veya.conversation.question_detector import QuestionDetector


class QuestionCandidateTrackerTests(unittest.TestCase):
    def setUp(self):
        self.tracker = QuestionCandidateTracker(QuestionDetector())

    def test_a_strong_prompt_moves_straight_to_drafting_on_the_first_fragment(self):
        decision = self.tracker.on_pending_text_changed("Tell me about yourself")
        self.assertEqual(decision.state, CandidateState.DRAFTING)
        self.assertTrue(decision.emit_candidate)
        self.assertTrue(decision.start_or_replace_draft)
        self.assertFalse(decision.is_replace)
        self.assertEqual(self.tracker.state, CandidateState.DRAFTING)

    def test_a_weak_fragment_only_becomes_a_candidate_not_a_draft(self):
        # "We use a deployment risk score" has no interrogative/spoken-
        # prompt signal at all — scores 0.0, so it must not even become a
        # candidate.
        decision = self.tracker.on_pending_text_changed("We use a deployment risk score")
        self.assertEqual(decision.state, CandidateState.IDLE)
        self.assertFalse(decision.emit_candidate)
        self.assertFalse(decision.start_or_replace_draft)

    def test_a_fragment_extending_an_already_drafting_candidate_retains_the_stream(self):
        first = self.tracker.on_pending_text_changed("What was the bottleneck causing latency")
        self.assertTrue(first.start_or_replace_draft)

        second = self.tracker.on_pending_text_changed(
            "What was the bottleneck causing latency in your YOLOv5 inference pipeline"
        )
        self.assertEqual(second.state, CandidateState.DRAFTING)
        self.assertTrue(second.emit_updated)
        self.assertFalse(second.start_or_replace_draft)  # no restart on a pure extension

    def test_a_materially_different_fragment_while_drafting_replaces_the_draft(self):
        self.tracker.on_pending_text_changed("Tell me about yourself")
        self.assertEqual(self.tracker.state, CandidateState.DRAFTING)

        decision = self.tracker.on_pending_text_changed("What time is the meeting tomorrow")
        self.assertTrue(decision.start_or_replace_draft)
        self.assertTrue(decision.is_replace)

    def test_finalize_after_an_unchanged_extension_does_not_regenerate(self):
        self.tracker.on_pending_text_changed("Tell me about yourself")
        decision = self.tracker.on_finalize("Tell me about yourself")
        self.assertEqual(decision.state, CandidateState.FINALIZED)
        self.assertFalse(decision.start_or_replace_draft)  # same text the draft already used

    def test_finalize_with_materially_more_text_than_the_draft_regenerates_as_a_replacement(self):
        self.tracker.on_pending_text_changed("What was the bottleneck causing latency")
        decision = self.tracker.on_finalize(
            "What was the bottleneck causing latency in your YOLOv5 inference pipeline?"
        )
        self.assertEqual(decision.state, CandidateState.FINALIZED)
        self.assertTrue(decision.start_or_replace_draft)
        self.assertTrue(decision.is_replace)

    def test_finalize_with_no_prior_draft_starts_one_fresh(self):
        self.tracker.on_pending_text_changed("the caching layer, how does that scale")  # ambiguous, not drafting yet
        self.assertEqual(self.tracker.state, CandidateState.CANDIDATE)
        decision = self.tracker.on_finalize("the caching layer, how does that scale")
        self.assertTrue(decision.start_or_replace_draft)
        self.assertFalse(decision.is_replace)

    def test_reject_then_a_new_fragment_starts_a_fresh_candidate(self):
        self.tracker.on_pending_text_changed("Thanks everyone for joining")
        self.tracker.on_reject()
        self.assertEqual(self.tracker.state, CandidateState.REJECTED)

        decision = self.tracker.on_pending_text_changed("Walk me through your resume")
        self.assertEqual(decision.state, CandidateState.DRAFTING)
        self.assertTrue(decision.start_or_replace_draft)
        self.assertFalse(decision.is_replace)

    def test_finalize_then_a_new_unrelated_fragment_starts_a_fresh_candidate_not_an_extension(self):
        self.tracker.on_pending_text_changed("Explain the deployment risk scoring algorithm")
        self.tracker.on_finalize("Explain the deployment risk scoring algorithm")
        self.assertEqual(self.tracker.state, CandidateState.FINALIZED)

        decision = self.tracker.on_pending_text_changed("What inputs and weighting would you use")
        self.assertEqual(decision.state, CandidateState.DRAFTING)
        self.assertTrue(decision.emit_candidate)
        self.assertFalse(decision.emit_updated)

    def test_mark_stabilizing_is_reversible_by_a_new_extending_fragment(self):
        self.tracker.on_pending_text_changed("What was the bottleneck")
        self.tracker.mark_stabilizing()
        self.assertEqual(self.tracker.state, CandidateState.STABILIZING)

        decision = self.tracker.on_pending_text_changed("What was the bottleneck causing latency")
        self.assertEqual(decision.state, CandidateState.DRAFTING)  # resumes from the pre-stabilize state
        self.assertNotEqual(self.tracker.state, CandidateState.STABILIZING)

    def test_empty_text_is_a_no_op(self):
        decision = self.tracker.on_pending_text_changed("   ")
        self.assertEqual(decision.state, CandidateState.IDLE)
        self.assertFalse(decision.emit_candidate)
        self.assertFalse(decision.start_or_replace_draft)


if __name__ == "__main__":
    unittest.main()
