import unittest

from veya.conversation.question_detector import QuestionDetector
from veya.conversation.semantic_classifier import classify_turn

# A genuinely realistic ambiguous turn under the *default* scoring config
# — see question_detector.py's `mid_sentence_interrogative_score`. No
# custom detector configuration needed to land in the ambiguous band.
_AMBIGUOUS_TEXT = "the caching layer, how does that scale"


class SemanticClassifierUnitTests(unittest.IsolatedAsyncioTestCase):
    async def test_clear_deterministic_question_never_calls_the_provider(self):
        class BoomIfCalledProvider:
            async def generate_stream(self, prompt, *, timeout):
                raise AssertionError("must not be called for a clear deterministic case")
                yield ""  # pragma: no cover

        result = await classify_turn("Why did the migration take six weeks?", QuestionDetector(), BoomIfCalledProvider())
        self.assertTrue(result.is_answer_request)
        self.assertFalse(result.used_semantic_stage)

    async def test_clear_deterministic_rejection_never_calls_the_provider(self):
        class BoomIfCalledProvider:
            async def generate_stream(self, prompt, *, timeout):
                raise AssertionError("must not be called for a clear deterministic case")
                yield ""  # pragma: no cover

        result = await classify_turn("Hi, good morning everyone.", QuestionDetector(), BoomIfCalledProvider())
        self.assertFalse(result.is_answer_request)
        self.assertFalse(result.used_semantic_stage)

    async def test_a_realistic_ambiguous_turn_actually_reaches_the_ambiguous_band(self):
        # Direct proof against the *default*, un-configured detector: a
        # realistic mid-sentence-interrogative follow-up must score inside
        # [LOW_CONFIDENCE_REJECT_BOUND, confidence_threshold), not land on
        # one of the "big" signals' discrete values.
        from veya.conversation.semantic_classifier import LOW_CONFIDENCE_REJECT_BOUND

        detector = QuestionDetector()
        score = detector.score(_AMBIGUOUS_TEXT)
        self.assertGreaterEqual(score, LOW_CONFIDENCE_REJECT_BOUND)
        self.assertLess(score, detector.confidence_threshold)

    async def test_ambiguous_turn_with_no_llm_provider_configured_falls_back_to_rejection(self):
        result = await classify_turn(_AMBIGUOUS_TEXT, QuestionDetector(), None)
        self.assertFalse(result.is_answer_request)
        self.assertFalse(result.used_semantic_stage)

    async def test_no_sensitive_content_reaches_logs_on_malformed_classifier_output(self):
        class BrokenProvider:
            async def generate_stream(self, prompt, *, timeout):
                yield "not json"

        sensitive_turn_text = "the caching layer, how does that scale — my SSN is 123-45-6789"
        with self.assertLogs("veya.classifier", level="INFO") as logs:
            await classify_turn(sensitive_turn_text, QuestionDetector(), BrokenProvider())
        logged_text = "\n".join(logs.output)
        self.assertNotIn("123-45-6789", logged_text)
        self.assertNotIn(sensitive_turn_text, logged_text)
