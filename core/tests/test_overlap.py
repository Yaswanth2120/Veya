import unittest

from veya.transcription.overlap import dedupe_overlap


class DedupeOverlapTests(unittest.TestCase):
    def test_no_previous_text_returns_new_text_unchanged(self):
        self.assertEqual(dedupe_overlap("", "hello world"), "hello world")

    def test_no_overlap_returns_new_text_unchanged(self):
        previous = "thanks everyone for joining"
        new = "let's get started with the recap"
        self.assertEqual(dedupe_overlap(previous, new), new)

    def test_exact_word_overlap_is_stripped(self):
        previous = "we moved the auth service first"
        new = "auth service first since everything depended on it"
        self.assertEqual(dedupe_overlap(previous, new), "since everything depended on it")

    def test_longest_overlap_is_preferred_over_a_shorter_coincidental_match(self):
        previous = "the migration took six weeks in total"
        new = "six weeks in total because of staged rollout"
        self.assertEqual(dedupe_overlap(previous, new), "because of staged rollout")

    def test_full_new_text_matches_end_of_previous_returns_empty_string(self):
        previous = "so why did the migration take six weeks"
        new = "six weeks"
        self.assertEqual(dedupe_overlap(previous, new), "")

    def test_empty_new_text_returns_empty_string(self):
        self.assertEqual(dedupe_overlap("previous text", ""), "")
