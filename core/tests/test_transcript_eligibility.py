import unittest

from veya.conversation.transcript_eligibility import (
    TranscriptRejectionReason,
    classify_transcript_text,
    classify_turn_quality,
    is_credible_turn,
    is_eligible_transcript_text,
)


class ClassifyTranscriptTextTests(unittest.TestCase):
    def test_every_listed_non_speech_marker_is_rejected(self):
        markers = [
            "[BLANK_AUDIO]",
            "[inaudible]",
            "[silence]",
            "(silence)",
            "(wind blowing)",
            "(soft music)",
            "(mouse clicking)",
            "(keyboard clicking)",
        ]
        for marker in markers:
            with self.subTest(marker=marker):
                self.assertEqual(classify_transcript_text(marker), TranscriptRejectionReason.NON_SPEECH_MARKER)
                self.assertFalse(is_eligible_transcript_text(marker))

    def test_case_and_separator_variants_are_still_recognized(self):
        for marker in ["[Blank_Audio]", "[BLANK AUDIO]", "(Wind Blowing)", "[INAUDIBLE]"]:
            with self.subTest(marker=marker):
                self.assertFalse(is_eligible_transcript_text(marker))

    def test_repeated_markers_mixed_with_only_whitespace_are_rejected(self):
        self.assertFalse(is_eligible_transcript_text("[BLANK_AUDIO] [BLANK_AUDIO]"))
        self.assertFalse(is_eligible_transcript_text("(silence) (silence) (silence)"))
        self.assertFalse(is_eligible_transcript_text("[BLANK_AUDIO]   (soft music)"))

    def test_empty_and_whitespace_only_text_is_rejected(self):
        self.assertEqual(classify_transcript_text(""), TranscriptRejectionReason.EMPTY)
        self.assertEqual(classify_transcript_text("   "), TranscriptRejectionReason.EMPTY)

    def test_a_real_sentence_containing_a_literal_parenthetical_is_preserved(self):
        text = "I said, and I mean this, that we shipped it on time"
        self.assertTrue(is_eligible_transcript_text(text))
        text_with_brackets = "The config value (which defaults to ten) was too low"
        self.assertTrue(is_eligible_transcript_text(text_with_brackets))

    def test_short_legitimate_prompts_remain_eligible(self):
        for text in ["Tell me about yourself", "Why you?", "Why us?"]:
            with self.subTest(text=text):
                self.assertTrue(is_eligible_transcript_text(text))

    def test_a_compound_question_is_fully_eligible(self):
        text = "What was the bottleneck, and how did you reduce the latency from 35% to 20%?"
        self.assertTrue(is_eligible_transcript_text(text))


class ClassifyTurnQualityTests(unittest.TestCase):
    def test_marker_only_turns_are_rejected(self):
        self.assertEqual(classify_turn_quality("[BLANK_AUDIO]"), TranscriptRejectionReason.NON_SPEECH_MARKER)
        self.assertFalse(is_credible_turn("(soft music)"))

    def test_too_short_turns_are_rejected_as_low_quality(self):
        self.assertFalse(is_credible_turn("ok"))
        self.assertEqual(classify_turn_quality("ok"), TranscriptRejectionReason.LOW_QUALITY)

    def test_repeated_asr_garbage_is_rejected(self):
        self.assertFalse(is_credible_turn("the the the the the"))
        self.assertFalse(is_credible_turn("yeah yeah yeah yeah yeah yeah"))

    def test_credible_short_prompts_are_not_rejected(self):
        self.assertTrue(is_credible_turn("Tell me about yourself"))
        self.assertTrue(is_credible_turn("Why you?"))

    def test_a_compound_question_is_a_credible_turn(self):
        self.assertTrue(is_credible_turn("What was the bottleneck, and how did you reduce the latency from 35% to 20%?"))
