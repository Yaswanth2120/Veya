import unittest

from veya.conversation.context_builder import render_prompt
from veya.conversation.models import SessionContext


class RenderPromptTests(unittest.TestCase):
    def test_empty_session_context_still_produces_a_valid_prompt(self):
        prompt = render_prompt(SessionContext(), "Why did the migration take six weeks?")
        self.assertIn("Why did the migration take six weeks?", prompt)
        self.assertIn("ANSWER:", prompt)
        self.assertIn("POINTS:", prompt)
        self.assertIn("CAVEAT:", prompt)

    def test_populated_session_context_fields_all_appear_in_the_prompt(self):
        context = SessionContext(
            title="Migration Recap",
            company="Acme Corp",
            role_or_topic="Staff Engineer",
            description="A recap of the Q3 auth migration.",
            notes="Audience is mostly backend engineers.",
            preferred_answer_style="concise",
            preferred_programming_language="Swift",
            custom_instructions="Keep answers under 3 sentences.",
        )
        prompt = render_prompt(context, "Why did it take six weeks?")

        self.assertIn("Migration Recap", prompt)
        self.assertIn("Acme Corp", prompt)
        self.assertIn("Staff Engineer", prompt)
        self.assertIn("A recap of the Q3 auth migration.", prompt)
        self.assertIn("Audience is mostly backend engineers.", prompt)
        self.assertIn("concise", prompt)
        self.assertIn("Swift", prompt)
        self.assertIn("Keep answers under 3 sentences.", prompt)
        self.assertIn("Why did it take six weeks?", prompt)

    def test_instructs_the_model_not_to_invent_sources(self):
        prompt = render_prompt(SessionContext(), "Any question?")
        self.assertIn("Do not invent citations, sources, or documents.", prompt)

    def test_user_answer_block_appears_distinctly_and_is_labeled_authoritative(self):
        # Section 16: a follow-up interviewer question must ground itself
        # in what the user actually said — this is the dedicated field
        # for that, distinct from the general recent-conversation block.
        prompt = render_prompt(
            SessionContext(),
            "What was the measured impact?",
            user_answer_block="I profiled YOLOv5 inference, then used TensorRT and batching",
        )
        self.assertIn("I profiled YOLOv5 inference, then used TensorRT and batching", prompt)
        self.assertIn("the user's own most recent actual answer", prompt.lower())

    def test_no_user_answer_block_produces_no_such_section(self):
        prompt = render_prompt(SessionContext(), "Tell me about yourself")
        self.assertNotIn("actual answer", prompt.lower())
