import unittest

from veya.conversation.question_detector import QuestionDetectionConfig, QuestionDetector


class QuestionDetectorTests(unittest.TestCase):
    def test_interrogative_punctuation_is_detected(self):
        detector = QuestionDetector()
        result = detector.detect("So why did the migration take six weeks?")
        self.assertIsNotNone(result)
        self.assertEqual(result.text, "So why did the migration take six weeks?")
        self.assertGreaterEqual(result.confidence, 0.6)

    def test_spoken_question_without_punctuation_is_detected(self):
        detector = QuestionDetector()
        result = detector.detect("What time does the migration actually finish")
        self.assertIsNotNone(result)

    def test_declarative_sentence_is_rejected(self):
        detector = QuestionDetector()
        result = detector.detect("We moved the auth service first since everything else depended on it.")
        self.assertIsNone(result)

    def test_not_every_sentence_is_treated_as_a_question(self):
        detector = QuestionDetector()
        sentences = [
            "Thanks everyone for joining, let's get started with the recap.",
            "That's a fair question, let me walk through the timeline.",
            "We rolled it out in stages to keep backward compatibility.",
        ]
        for sentence in sentences:
            with self.subTest(sentence=sentence):
                self.assertIsNone(detector.detect(sentence))

    def test_empty_and_whitespace_text_is_rejected(self):
        detector = QuestionDetector()
        self.assertIsNone(detector.detect(""))
        self.assertIsNone(detector.detect("   "))

    def test_exact_duplicate_from_overlapping_windows_is_suppressed(self):
        detector = QuestionDetector()
        first = detector.detect("Why did the migration take six weeks?")
        second = detector.detect("Why did the migration take six weeks?")
        self.assertIsNotNone(first)
        self.assertIsNone(second)

    def test_near_duplicate_substring_from_overlapping_windows_is_suppressed(self):
        detector = QuestionDetector()
        first = detector.detect("Why did the migration take six weeks to complete?")
        # A window boundary variant that's a substring of the first.
        second = detector.detect("did the migration take six weeks to complete?")
        self.assertIsNotNone(first)
        self.assertIsNone(second)

    def test_a_genuinely_different_question_after_a_previous_one_is_still_detected(self):
        detector = QuestionDetector()
        first = detector.detect("Why did the migration take six weeks?")
        second = detector.detect("How many engineers worked on the rollout?")
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)

    def test_confidence_threshold_rejects_weak_signals(self):
        config = QuestionDetectionConfig(confidence_threshold=0.9)
        detector = QuestionDetector(config)
        # Interrogative start alone scores 0.65 by default — below a 0.9
        # threshold, so a spoken question without "?" should be rejected
        # at a stricter threshold even though it would pass the default.
        result = detector.detect("What time does the meeting start")
        self.assertIsNone(result)

    def test_confidence_threshold_can_be_loosened(self):
        config = QuestionDetectionConfig(confidence_threshold=0.1)
        detector = QuestionDetector(config)
        # Bare "?" appearing mid-sentence (not at the end) only scores the
        # weaker contains_question_mark_score — should pass a loose
        # threshold but not the default 0.6.
        default_detector = QuestionDetector()
        text = "Wait? that doesn't sound right at all honestly"
        self.assertIsNone(default_detector.detect(text))
        self.assertIsNotNone(detector.detect(text))

    def test_recent_question_tracking_is_bounded(self):
        config = QuestionDetectionConfig(max_recent_questions_tracked=2)
        detector = QuestionDetector(config)
        detector.detect("Why is this happening first?")
        detector.detect("How does this work second?")
        # A third distinct question evicts the oldest tracked entry, so
        # re-asking the *first* question again is no longer recognized as
        # a duplicate.
        detector.detect("When will this ship third?")
        result = detector.detect("Why is this happening first?")
        self.assertIsNotNone(result)
