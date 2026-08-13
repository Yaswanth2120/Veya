import asyncio
import tempfile
import unittest
from pathlib import Path

from veya.coding.workspace import CodeWorkspaceStore
from veya.ipc.errors import ErrorCode, ProtocolError
from veya.ipc.dispatcher import Dispatcher, WorkerContext
from veya.ipc.protocol import Request


class CodeWorkspaceStoreTests(unittest.TestCase):
    def test_versioned_incremental_edits_persist(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = CodeWorkspaceStore(Path(temporary))
            file = store.upsert_file("session-1", "main.py", "python", "print('one')", None)
            updated = store.apply_edits("session-1", "main.py", file.version, [{"start": 7, "end": 10, "replacement": "two"}])
            self.assertEqual(updated.content, "print('two')")
            self.assertEqual(updated.version, 2)
            with self.assertRaises(ProtocolError):
                store.apply_edits("session-1", "main.py", 1, [])

    def test_path_traversal_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(ProtocolError) as raised:
                CodeWorkspaceStore(Path(temporary)).upsert_file("session-1", "../secret.py", "python", "", None)
            self.assertEqual(raised.exception.code, ErrorCode.INVALID_PARAMS)


class CodingDispatcherTests(unittest.IsolatedAsyncioTestCase):
    async def test_upsert_and_analyze_round_trip(self):
        with tempfile.TemporaryDirectory() as temporary:
            context = WorkerContext(emit_event=_ignore_event, coding_workspace_directory=Path(temporary))
            dispatcher = Dispatcher()
            stored = await dispatcher.dispatch(Request(id="1", method="coding.upsert_file", params={"session_id": "s-1", "name": "main.py", "language": "python", "content": "def f(x):\n    if x:\n        return 1\n"}), context)
            self.assertEqual(stored["version"], 1)
            analysis = await dispatcher.dispatch(Request(id="2", method="coding.analyze", params={"session_id": "s-1", "name": "main.py"}), context)
            self.assertTrue(analysis["syntax_ok"])
            self.assertEqual(analysis["complexity"], 2)


async def _ignore_event(name, data):
    return None
