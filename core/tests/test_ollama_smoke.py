"""Optional, manual-only smoke test against a REAL local Ollama instance.
Skipped by default — this repo's ordinary test run (`python3 -m unittest
discover`) must never require Ollama or network access. To run it
deliberately:

    VEYA_RUN_OLLAMA_SMOKE_TEST=1 \\
    VEYA_OLLAMA_URL=http://localhost:11434 \\
    VEYA_OLLAMA_MODEL=<a model you have pulled> \\
    python3 -m unittest tests.test_ollama_smoke -v

This proves the full question-detection + answer-generation pipeline
against a real local model; it does not measure answer quality or
guarantee any particular latency. See
docs/QUESTION_AND_ANSWER_INTELLIGENCE.md.
"""

from __future__ import annotations

import os
import unittest

from veya.conversation.models import SessionContext
from veya.conversation.orchestrator import ConversationOrchestrator
from veya.llm.ollama_provider import OllamaProvider

RUN_SMOKE_TEST = os.environ.get("VEYA_RUN_OLLAMA_SMOKE_TEST") == "1"


@unittest.skipUnless(RUN_SMOKE_TEST, "set VEYA_RUN_OLLAMA_SMOKE_TEST=1 to run against a real local Ollama instance")
class OllamaSmokeTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_detected_question_produces_a_real_completed_answer(self):
        provider = OllamaProvider()
        await provider.check_availability()  # raises LLMUnavailableError if not actually configured/reachable

        events: list[tuple[str, dict]] = []

        async def emit_event(name: str, data: dict) -> None:
            events.append((name, data))

        orchestrator = ConversationOrchestrator(
            session_id="smoke-test",
            session_context=SessionContext(title="Smoke Test", preferred_answer_style="concise"),
            emit_event=emit_event,
            llm_provider=provider,
        )
        try:
            await orchestrator.handle_final_transcript("So why did the migration take six weeks?", 0.0, 4.0)
            task = orchestrator._active_answer_task
            if task is not None:
                await task
        finally:
            await orchestrator.close()

        names = [name for name, _ in events]
        self.assertIn("question.detected", names)
        self.assertIn("answer.started", names)
        self.assertEqual(names[-1], "answer.completed")

        completed = events[-1][1]
        self.assertTrue(completed["talking_points"] or completed["caveat"])
        self.assertEqual(completed["sources"], [])
