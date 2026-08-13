import unittest

from veya.conversation.question_detector import QuestionDetectionConfig, QuestionDetector
from veya.conversation.semantic_classifier import classify_turn


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

    async def test_ambiguous_turn_with_no_llm_provider_configured_falls_back_to_rejection(self):
        detector = QuestionDetector(QuestionDetectionConfig(contains_question_mark_score=0.45))
        result = await classify_turn("the caching strategy? explain more", detector, None)
        self.assertFalse(result.is_answer_request)
        self.assertFalse(result.used_semantic_stage)

    async def test_no_sensitive_content_reaches_logs_on_malformed_classifier_output(self):
        class BrokenProvider:
            async def generate_stream(self, prompt, *, timeout):
                yield "not json"

        detector = QuestionDetector(QuestionDetectionConfig(contains_question_mark_score=0.45))
        sensitive_turn_text = "the caching strategy? my SSN is 123-45-6789, explain more"
        with self.assertLogs("veya.classifier", level="INFO") as logs:
            await classify_turn(sensitive_turn_text, detector, BrokenProvider())
        logged_text = "\n".join(logs.output)
        self.assertNotIn("123-45-6789", logged_text)
        self.assertNotIn(sensitive_turn_text, logged_text)
