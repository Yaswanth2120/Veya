import unittest

from veya.conversation.speakable_stream import SpeakableAnswerStream


def _run(chunks: list[str]) -> tuple[str, str]:
    stream = SpeakableAnswerStream()
    speakable_parts = [stream.feed(chunk) for chunk in chunks]
    return "".join(speakable_parts), stream.clean_text()


class SpeakableAnswerStreamTests(unittest.TestCase):
    def test_think_tags_split_over_multiple_chunks_never_reach_speakable_output(self):
        speakable, _ = _run(["<thi", "nk>hidden reasoning", " text</th", "ink>ANSWER: Hello there.\n"])
        self.assertEqual(speakable, "Hello there.\n")
        self.assertNotIn("hidden reasoning", speakable)
        self.assertNotIn("<think", speakable)
        self.assertNotIn("</think", speakable)

    def test_raw_reasoning_never_reaches_clean_text_used_for_final_parsing(self):
        _, clean = _run(["<think>secret plan</think>ANSWER: The visible answer.\n"])
        self.assertNotIn("secret plan", clean)
        self.assertIn("The visible answer.", clean)

    def test_a_model_with_no_think_tags_streams_cleanly(self):
        speakable, clean = _run(["ANSWER: I led the migration", " and cut latency significantly.\n"])
        self.assertEqual(speakable, "I led the migration and cut latency significantly.\n")
        self.assertEqual(clean, "ANSWER: I led the migration and cut latency significantly.\n")

    def test_an_unclosed_think_block_suppresses_everything_after_it_without_leaking(self):
        speakable, clean = _run(["<think>this reasoning block", " never closes", " and just keeps going"])
        self.assertEqual(speakable, "")
        self.assertEqual(clean, "")

    def test_a_malformed_unrecognized_tag_is_not_treated_as_a_reasoning_block(self):
        # `<thought>` is not a recognized reasoning tag — must not be
        # silently swallowed as if it were `<think>`.
        speakable, _ = _run(["ANSWER: <thought>this is literal text</thought> continues.\n"])
        self.assertIn("thought", speakable)

    def test_model_output_that_ignores_the_requested_format_streams_as_plain_speakable_prose(self):
        speakable, clean = _run(["I'm a backend engineer", " with distributed systems experience."])
        self.assertEqual(speakable, "I'm a backend engineer with distributed systems experience.")
        self.assertEqual(clean, speakable)

    def test_an_empty_stream_produces_empty_speakable_and_clean_text(self):
        speakable, clean = _run([])
        self.assertEqual(speakable, "")
        self.assertEqual(clean, "")

    def test_a_stream_of_only_a_think_block_and_nothing_else_produces_empty_output(self):
        speakable, clean = _run(["<think>only reasoning, no real answer follows</think>"])
        self.assertEqual(speakable, "")
        self.assertEqual(clean, "")

    def test_points_and_caveat_sections_are_suppressed_from_speakable_output(self):
        speakable, clean = _run(
            ["ANSWER: The natural answer.\nPOINTS:\n- detail one\n- detail two\nCAVEAT: assumes defaults\n"]
        )
        self.assertEqual(speakable, "The natural answer.\n")
        self.assertNotIn("detail one", speakable)
        self.assertNotIn("assumes defaults", speakable)
        # The full clean text (headers intact) still carries everything,
        # for `parse_answer_text` to extract talking points/caveat from.
        self.assertIn("POINTS:", clean)
        self.assertIn("detail one", clean)
        self.assertIn("CAVEAT: assumes defaults", clean)

    def test_points_and_caveat_headers_are_suppressed_even_split_character_by_character(self):
        speakable, _ = _run(list("ANSWER: Hi there.\nPOINTS:\n- a point\nCAVEAT: none\n"))
        self.assertEqual(speakable, "Hi there.\n")

    def test_the_answer_header_label_and_its_leading_whitespace_never_appear_in_speakable_output(self):
        speakable, _ = _run(["ANSWER:   Clean text right away.\n"])
        self.assertEqual(speakable, "Clean text right away.\n")

    def test_the_answer_header_split_from_its_trailing_space_still_strips_cleanly(self):
        speakable, _ = _run(["ANSWER:", "   ", "No leading space here.\n"])
        self.assertEqual(speakable, "No leading space here.\n")

    def test_a_json_style_response_with_no_answer_header_streams_as_fallback_prose(self):
        speakable, _ = _run(['{"answer": "hello"}'])
        # No format was followed at all — this is exactly what
        # `parse_answer_text`'s own fallback treats as the answer
        # verbatim, so the live stream must match that, not silently
        # drop it.
        self.assertEqual(speakable, '{"answer": "hello"}')

    def test_multi_paragraph_answers_preserve_blank_lines_within_the_answer_section(self):
        speakable, _ = _run(["ANSWER: First paragraph.\n\nSecond paragraph.\nPOINTS:\n"])
        self.assertEqual(speakable, "First paragraph.\n\nSecond paragraph.\n")

    def test_reasoning_tag_appearing_mid_answer_is_still_stripped(self):
        speakable, _ = _run(["ANSWER: Part one. <think>wait, let me reconsider</think> Part two.\n"])
        self.assertNotIn("reconsider", speakable)
        self.assertIn("Part one.", speakable)
        self.assertIn("Part two.", speakable)
