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


class CodeWorkspaceFileManagementTests(unittest.TestCase):
    def test_delete_file_removes_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = CodeWorkspaceStore(Path(temporary))
            store.upsert_file("session-1", "main.py", "python", "x = 1", None)
            store.delete_file("session-1", "main.py")
            self.assertIsNone(store.get_file("session-1", "main.py"))

    def test_delete_missing_file_is_session_not_found(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = CodeWorkspaceStore(Path(temporary))
            with self.assertRaises(ProtocolError) as raised:
                store.delete_file("session-1", "missing.py")
            self.assertEqual(raised.exception.code, ErrorCode.SESSION_NOT_FOUND)

    def test_rename_file_preserves_content_version_and_history(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = CodeWorkspaceStore(Path(temporary))
            store.upsert_file("session-1", "main.py", "python", "x = 1", None)
            store.append_history("session-1", "main.py", "explain", "what does this do", "it sets x")
            renamed = store.rename_file("session-1", "main.py", "solution.py")

            self.assertEqual(renamed.name, "solution.py")
            self.assertEqual(renamed.content, "x = 1")
            self.assertEqual(renamed.version, 1)
            self.assertEqual(len(renamed.history), 1)
            self.assertIsNone(store.get_file("session-1", "main.py"))
            self.assertIsNotNone(store.get_file("session-1", "solution.py"))

    def test_rename_to_an_existing_name_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = CodeWorkspaceStore(Path(temporary))
            store.upsert_file("session-1", "a.py", "python", "1", None)
            store.upsert_file("session-1", "b.py", "python", "2", None)
            with self.assertRaises(ProtocolError) as raised:
                store.rename_file("session-1", "a.py", "b.py")
            self.assertEqual(raised.exception.code, ErrorCode.INVALID_PARAMS)


class CodingFileManagementRPCTests(unittest.IsolatedAsyncioTestCase):
    async def test_delete_and_rename_rpcs(self):
        with tempfile.TemporaryDirectory() as temporary:
            context = WorkerContext(emit_event=_ignore_event, coding_workspace_directory=Path(temporary))
            dispatcher = Dispatcher()
            await dispatcher.dispatch(Request(id="1", method="coding.upsert_file", params={"session_id": "s-1", "name": "main.py", "language": "python", "content": "x = 1"}), context)

            renamed = await dispatcher.dispatch(Request(id="2", method="coding.rename_file", params={"session_id": "s-1", "name": "main.py", "new_name": "solution.py"}), context)
            self.assertEqual(renamed["name"], "solution.py")

            await dispatcher.dispatch(Request(id="3", method="coding.delete_file", params={"session_id": "s-1", "name": "solution.py"}), context)
            files = await dispatcher.dispatch(Request(id="4", method="coding.list_files", params={"session_id": "s-1"}), context)
            self.assertEqual(files["files"], [])
