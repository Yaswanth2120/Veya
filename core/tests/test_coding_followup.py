import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from veya.ipc.dispatcher import Dispatcher, WorkerContext
from veya.ipc.errors import ErrorCode, ProtocolError
from veya.ipc.protocol import Request


class FakeProvider:
    def __init__(self, responses):
        self._responses = list(responses)
        self.prompts = []

    async def check_availability(self):
        return None

    async def generate_stream(self, prompt, *, timeout):
        self.prompts.append(prompt)
        await asyncio.sleep(0)
        yield json.dumps(self._responses.pop(0))


async def _ignore_event(name, data):
    return None


class CodingFollowupRPCTests(unittest.IsolatedAsyncioTestCase):
    async def test_followup_retains_history_across_calls_and_is_included_in_the_next_prompt(self):
        with tempfile.TemporaryDirectory() as temporary:
            provider = FakeProvider([
                {"explanation": "Added a sliding window.", "edits": [], "tests": "", "complexity": "O(n)"},
                {"explanation": "Now handles emoji via grapheme clusters.", "edits": [], "tests": "", "complexity": "O(n)"},
            ])
            context = WorkerContext(emit_event=_ignore_event, coding_workspace_directory=Path(temporary), llm_provider_factory=lambda: provider)
            dispatcher = Dispatcher()
            await dispatcher.dispatch(Request(id="1", method="coding.upsert_file", params={"session_id": "s-1", "name": "main.py", "language": "python", "content": "def f(s): return s"}), context)

            first = await dispatcher.dispatch(Request(id="2", method="coding.followup", params={"session_id": "s-1", "name": "main.py", "request": "Solve longest substring without repeating characters."}), context)
            self.assertEqual(first["explanation"], "Added a sliding window.")

            second = await dispatcher.dispatch(Request(id="3", method="coding.followup", params={"session_id": "s-1", "name": "main.py", "request": "Make this work correctly with emoji and composed Unicode characters."}), context)
            self.assertEqual(second["explanation"], "Now handles emoji via grapheme clusters.")

            # The second prompt must carry the first exchange as context —
            # a genuine follow-up, not a restart from an empty prompt.
            self.assertIn("Added a sliding window.", provider.prompts[1])
            self.assertIn("Solve longest substring", provider.prompts[1])

    async def test_history_survives_an_apply_between_the_generate_and_the_unicode_followup(self):
        # Regression test: `CodeWorkspaceStore.upsert_file` used to build a
        # fresh `CodeFile` on every call (including the one `apply_edits`
        # makes internally) without carrying `previous.history` forward,
        # so applying a proposal silently wiped the follow-up context the
        # very next request needed. This exercises the exact acceptance
        # sequence: generate a solution, apply it, then ask a follow-up —
        # the follow-up's prompt must still see the first exchange.
        with tempfile.TemporaryDirectory() as temporary:
            provider = FakeProvider([
                {"explanation": "Added a sliding window over two pointers.", "edits": [{"start": 0, "end": 4, "replacement": "def solve"}], "tests": "", "complexity": "O(n)"},
                {"explanation": "Now handles emoji via grapheme clusters.", "edits": [], "tests": "", "complexity": "O(n)"},
            ])
            context = WorkerContext(emit_event=_ignore_event, coding_workspace_directory=Path(temporary), llm_provider_factory=lambda: provider)
            dispatcher = Dispatcher()
            stored = await dispatcher.dispatch(Request(id="1", method="coding.upsert_file", params={"session_id": "s-1", "name": "main.py", "language": "python", "content": "def f(s): return s"}), context)

            proposal = await dispatcher.dispatch(Request(id="2", method="coding.followup", params={"session_id": "s-1", "name": "main.py", "request": "Solve longest substring without repeating characters."}), context)

            applied = await dispatcher.dispatch(Request(id="3", method="coding.apply_edits", params={"session_id": "s-1", "name": "main.py", "base_version": stored["version"], "edits": proposal["edits"]}), context)
            self.assertTrue(applied["content"].startswith("def solve"))

            await dispatcher.dispatch(Request(id="4", method="coding.followup", params={"session_id": "s-1", "name": "main.py", "request": "Make this work correctly with emoji and composed Unicode characters."}), context)

            self.assertIn("Added a sliding window over two pointers.", provider.prompts[1])
            self.assertIn("Solve longest substring", provider.prompts[1])

    async def test_debug_generate_tests_and_explain_are_distinct_rpcs(self):
        with tempfile.TemporaryDirectory() as temporary:
            provider = FakeProvider([
                {"explanation": "e1", "edits": [], "tests": "", "complexity": ""},
                {"explanation": "e2", "edits": [], "tests": "assert True", "complexity": ""},
                {"explanation": "e3", "edits": [], "tests": "", "complexity": ""},
            ])
            context = WorkerContext(emit_event=_ignore_event, coding_workspace_directory=Path(temporary), llm_provider_factory=lambda: provider)
            dispatcher = Dispatcher()
            await dispatcher.dispatch(Request(id="1", method="coding.upsert_file", params={"session_id": "s-1", "name": "main.py", "language": "python", "content": "x = 1"}), context)

            debug_result = await dispatcher.dispatch(Request(id="2", method="coding.debug", params={"session_id": "s-1", "name": "main.py", "request": "why does this fail"}), context)
            self.assertEqual(debug_result["explanation"], "e1")
            tests_result = await dispatcher.dispatch(Request(id="3", method="coding.generate_tests", params={"session_id": "s-1", "name": "main.py", "request": "add tests"}), context)
            self.assertEqual(tests_result["tests"], "assert True")
            explain_result = await dispatcher.dispatch(Request(id="4", method="coding.explain", params={"session_id": "s-1", "name": "main.py", "request": "explain this"}), context)
            self.assertEqual(explain_result["explanation"], "e3")

    async def test_rejecting_a_proposal_never_mutates_workspace_content(self):
        with tempfile.TemporaryDirectory() as temporary:
            provider = FakeProvider([{"explanation": "would change things", "edits": [{"start": 0, "end": 1, "replacement": "y"}], "tests": "", "complexity": ""}])
            context = WorkerContext(emit_event=_ignore_event, coding_workspace_directory=Path(temporary), llm_provider_factory=lambda: provider)
            dispatcher = Dispatcher()
            await dispatcher.dispatch(Request(id="1", method="coding.upsert_file", params={"session_id": "s-1", "name": "main.py", "language": "python", "content": "x = 1"}), context)
            await dispatcher.dispatch(Request(id="2", method="coding.followup", params={"session_id": "s-1", "name": "main.py", "request": "change it"}), context)

            files = await dispatcher.dispatch(Request(id="3", method="coding.list_files", params={"session_id": "s-1"}), context)
            # Only `coding.apply_edits` mutates content — merely proposing
            # (and never applying) must leave the file untouched, which is
            # exactly what "reject" means from Swift's point of view: it
            # simply never calls apply_edits.
            self.assertEqual(files["files"][0]["content"], "x = 1")
            self.assertEqual(files["files"][0]["version"], 1)

    async def test_stale_version_apply_is_rejected_without_overwriting_code(self):
        with tempfile.TemporaryDirectory() as temporary:
            context = WorkerContext(emit_event=_ignore_event, coding_workspace_directory=Path(temporary))
            dispatcher = Dispatcher()
            stored = await dispatcher.dispatch(Request(id="1", method="coding.upsert_file", params={"session_id": "s-1", "name": "main.py", "language": "python", "content": "x = 1"}), context)
            await dispatcher.dispatch(Request(id="2", method="coding.upsert_file", params={"session_id": "s-1", "name": "main.py", "language": "python", "content": "x = 2", "base_version": stored["version"]}), context)

            with self.assertRaises(ProtocolError) as raised:
                await dispatcher.dispatch(Request(id="3", method="coding.apply_edits", params={"session_id": "s-1", "name": "main.py", "base_version": stored["version"], "edits": [{"start": 0, "end": 1, "replacement": "z"}]}), context)
            self.assertEqual(raised.exception.code, ErrorCode.INVALID_PARAMS)

            files = await dispatcher.dispatch(Request(id="4", method="coding.list_files", params={"session_id": "s-1"}), context)
            self.assertEqual(files["files"][0]["content"], "x = 2")
