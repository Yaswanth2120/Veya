import unittest

from veya.conversation.groundedness import check_answer_groundedness


class CheckAnswerGroundednessTests(unittest.TestCase):
    def test_same_value_percentage_change_is_a_contradiction(self):
        result = check_answer_groundedness("We reduced latency from 35% to 35% by adding a cache.", "")
        self.assertFalse(result.is_grounded)
        self.assertEqual(result.reason, "self_contradictory_numeric_change")

    def test_same_value_change_is_caught_even_with_percent_only_on_the_second_number(self):
        # "35 to 35%" — the model dropping the first % sign must not
        # dodge the check.
        result = check_answer_groundedness("We went from 35 to 35% on that metric.", "")
        self.assertFalse(result.is_grounded)
        self.assertEqual(result.reason, "self_contradictory_numeric_change")

    def test_a_genuine_percentage_change_grounded_in_context_is_fine(self):
        result = check_answer_groundedness(
            "We reduced latency from 35% to 20% by adding a cache.",
            "Resume: reduced p99 latency from 35% to 20% via caching.",
        )
        self.assertTrue(result.is_grounded)

    def test_a_percentage_not_present_anywhere_in_grounding_context_is_flagged(self):
        result = check_answer_groundedness(
            "We improved throughput by 42% after the migration.",
            "Resume: led a migration to microservices.",
        )
        self.assertFalse(result.is_grounded)
        self.assertEqual(result.reason, "unsupported_numeric_claim")

    def test_a_percentage_present_in_the_question_text_counts_as_grounded(self):
        # The interviewer's own question mentioning a number is not the
        # answer inventing it.
        result = check_answer_groundedness(
            "We reduced it by roughly 20%, mainly through caching.",
            'A question was just asked in this live conversation:\n"How did you reduce latency by 20%?"',
        )
        self.assertTrue(result.is_grounded)

    def test_an_answer_with_no_numbers_is_always_grounded(self):
        result = check_answer_groundedness("I led the migration and improved reliability significantly.", "")
        self.assertTrue(result.is_grounded)

    def test_empty_answer_text_is_trivially_grounded(self):
        result = check_answer_groundedness("", "")
        self.assertTrue(result.is_grounded)
