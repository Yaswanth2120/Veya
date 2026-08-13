import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from veya.ipc.dispatcher import Dispatcher, WorkerContext
from veya.ipc.errors import ErrorCode, ProtocolError
from veya.ipc.protocol import Request


class FakeProvider:
    def __init__(self, response):
        self._response = response

    async def check_availability(self):
        return None

    async def generate_stream(self, prompt, *, timeout):
        await asyncio.sleep(0)
        yield json.dumps(self._response)


async def _ignore_event(name, data):
    return None


_TRANSCRIPT = [{"text": "Let's discuss the migration plan.", "started_at": 0.0, "ended_at": 1.0, "is_final": True}]
_QUESTIONS = [
    {"id": "q1", "text": "How long will the migration take?", "detected_at": 0.0},
    {"id": "q2", "text": "What about rollback?", "detected_at": 1.0},
]
_ANSWERS = [
    {"question_id": "q1", "question": "How long will the migration take?", "talking_points": ["Six weeks"], "sources": [{"document_id": "d1", "file_name": "plan.pdf", "chunk_id": "c1", "excerpt": "six weeks"}]},
]


class SessionAnalyzeTests(unittest.IsolatedAsyncioTestCase):
    async def test_analyze_synthesizes_report_and_proposes_memory_candidates(self):
        provider = FakeProvider({
            "summary": "Discussed a six-week migration plan.",
            "topics": ["migration"],
            "decisions": ["Proceed with phased rollout"],
            "action_items": ["Draft rollback plan"],
            "preparation_gaps": ["Rollback process undefined"],
            "memory_candidates": ["Prefers concise answers"],
        })
        with tempfile.TemporaryDirectory() as temporary:
            context = WorkerContext(emit_event=_ignore_event, memory_database_path=Path(temporary) / "memory.sqlite", report_store_directory=Path(temporary) / "reports", llm_provider_factory=lambda: provider)
            self.addCleanup(context.close)
            dispatcher = Dispatcher()
            report = await dispatcher.dispatch(Request(id="1", method="session.analyze", params={"session_id": "s-1", "transcript": _TRANSCRIPT, "questions": _QUESTIONS, "answers": _ANSWERS}), context)

            self.assertEqual(report["summary"], "Discussed a six-week migration plan.")
            self.assertIn("Draft rollback plan", report["action_items"])
            self.assertEqual(report["unanswered_questions"], ["What about rollback?"])
            self.assertEqual(len(report["sources"]), 1)
            self.assertEqual(len(report["memory_candidate_ids"]), 1)

            # A candidate is created but not yet retrievable — never
            # silently saved as usable memory.
            memories = await dispatcher.dispatch(Request(id="2", method="memory.list", params={"status": "PROPOSED"}), context)
            self.assertEqual(len(memories["memories"]), 1)
            self.assertEqual(memories["memories"][0]["text"], "Prefers concise answers")

            # `session.report.get` returns the same cached report.
            fetched = await dispatcher.dispatch(Request(id="3", method="session.report.get", params={"session_id": "s-1"}), context)
            self.assertEqual(fetched["summary"], report["summary"])

    async def test_report_get_without_prior_analyze_is_not_found(self):
        with tempfile.TemporaryDirectory() as temporary:
            context = WorkerContext(emit_event=_ignore_event, report_store_directory=Path(temporary) / "reports")
            with self.assertRaises(ProtocolError) as raised:
                await Dispatcher().dispatch(Request(id="1", method="session.report.get", params={"session_id": "never-analyzed"}), context)
            self.assertEqual(raised.exception.code, ErrorCode.SESSION_NOT_FOUND)

    async def test_analyze_without_an_llm_provider_still_returns_data_only_report(self):
        class _UnavailableProvider:
            async def check_availability(self):
                raise RuntimeError("unavailable")

        with tempfile.TemporaryDirectory() as temporary:
            context = WorkerContext(emit_event=_ignore_event, memory_database_path=Path(temporary) / "memory.sqlite", report_store_directory=Path(temporary) / "reports", llm_provider_factory=_UnavailableProvider)
            self.addCleanup(context.close)
            report = await Dispatcher().dispatch(Request(id="1", method="session.analyze", params={"session_id": "s-1", "transcript": [], "questions": [], "answers": []}), context)
            self.assertEqual(report["memory_candidate_ids"], [])
            self.assertIn("No local LLM", report["summary"])

    async def test_report_survives_a_worker_restart(self):
        # Regression test: reports used to live only in
        # `WorkerContext.session_reports`, an in-memory dict lost on every
        # worker restart (crash, update, relaunch). Simulates a restart by
        # analyzing through one `WorkerContext`/`Dispatcher` pair, then
        # fetching through a completely fresh pair pointed at the same
        # on-disk directory — nothing in-process is shared between them.
        provider = FakeProvider({
            "summary": "Discussed a six-week migration plan.", "topics": [], "decisions": [],
            "action_items": [], "preparation_gaps": [], "memory_candidates": [],
        })
        with tempfile.TemporaryDirectory() as temporary:
            report_dir = Path(temporary) / "reports"
            first_context = WorkerContext(emit_event=_ignore_event, memory_database_path=Path(temporary) / "memory.sqlite", report_store_directory=report_dir, llm_provider_factory=lambda: provider)
            self.addCleanup(first_context.close)
            await Dispatcher().dispatch(Request(id="1", method="session.analyze", params={"session_id": "s-1", "transcript": _TRANSCRIPT, "questions": [], "answers": []}), first_context)

            second_context = WorkerContext(emit_event=_ignore_event, memory_database_path=Path(temporary) / "memory2.sqlite", report_store_directory=report_dir)
            self.addCleanup(second_context.close)
            fetched = await Dispatcher().dispatch(Request(id="2", method="session.report.get", params={"session_id": "s-1"}), second_context)
            self.assertEqual(fetched["summary"], "Discussed a six-week migration plan.")
