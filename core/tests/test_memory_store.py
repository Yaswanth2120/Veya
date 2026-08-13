import tempfile
import unittest
from pathlib import Path

from veya.ipc.dispatcher import Dispatcher, WorkerContext
from veya.ipc.errors import ErrorCode, ProtocolError
from veya.ipc.protocol import Request
from veya.memory.store import MemoryStore, STATUS_APPROVED, STATUS_PROPOSED


async def _ignore_event(name, data):
    return None


class MemoryStoreTests(unittest.TestCase):
    def test_candidate_lifecycle_and_durability_across_new_connections(self):
        with tempfile.TemporaryDirectory() as temporary:
            db_path = Path(temporary) / "memory.sqlite"
            store = MemoryStore(db_path)
            candidate = store.create_candidate("s-1", "Prefers concise answers")
            self.assertEqual(candidate.status, STATUS_PROPOSED)
            self.assertEqual(store.approved_texts(), [])

            approved = store.approve(candidate.id)
            self.assertEqual(approved.status, STATUS_APPROVED)
            self.assertEqual(store.approved_texts(), ["Prefers concise answers"])

            # Simulates an app restart: a brand new `MemoryStore` opened
            # against the same file must see the same durable state.
            reopened = MemoryStore(db_path)
            self.assertEqual(reopened.approved_texts(), ["Prefers concise answers"])

    def test_rejected_candidate_is_never_retrievable(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = MemoryStore(Path(temporary) / "memory.sqlite")
            candidate = store.create_candidate("s-1", "some inferred fact")
            store.reject(candidate.id)
            self.assertEqual(store.approved_texts(), [])
            self.assertEqual(store.list(), [])

    def test_update_and_delete_persist(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = MemoryStore(Path(temporary) / "memory.sqlite")
            candidate = store.create_candidate("s-1", "original text")
            store.approve(candidate.id)
            store.update(candidate.id, "updated text")
            self.assertEqual(store.approved_texts(), ["updated text"])
            store.delete(candidate.id)
            self.assertEqual(store.approved_texts(), [])

    def test_operating_on_a_missing_memory_id_raises(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = MemoryStore(Path(temporary) / "memory.sqlite")
            with self.assertRaises(ProtocolError):
                store.approve("does-not-exist")
            with self.assertRaises(ProtocolError):
                store.reject("does-not-exist")


class MemoryDispatcherTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_rpc_lifecycle(self):
        with tempfile.TemporaryDirectory() as temporary:
            context = WorkerContext(emit_event=_ignore_event, memory_database_path=Path(temporary) / "memory.sqlite")
            dispatcher = Dispatcher()

            # Seed a candidate the way `session.analyze` would.
            from veya.ipc.dispatcher import _get_or_create_memory_store
            candidate = _get_or_create_memory_store(context).create_candidate("s-1", "Works on the payments team")

            approved = await dispatcher.dispatch(Request(id="1", method="memory.approve", params={"memory_id": candidate.id}), context)
            self.assertEqual(approved["status"], "APPROVED")

            listed = await dispatcher.dispatch(Request(id="2", method="memory.list", params={}), context)
            self.assertEqual(len(listed["memories"]), 1)

            updated = await dispatcher.dispatch(Request(id="3", method="memory.update", params={"memory_id": candidate.id, "text": "Works on the payments platform team"}), context)
            self.assertEqual(updated["text"], "Works on the payments platform team")

            await dispatcher.dispatch(Request(id="4", method="memory.delete", params={"memory_id": candidate.id}), context)
            listed_after_delete = await dispatcher.dispatch(Request(id="5", method="memory.list", params={}), context)
            self.assertEqual(listed_after_delete["memories"], [])

    async def test_approved_memory_is_included_in_future_answer_prompts(self):
        with tempfile.TemporaryDirectory() as temporary:
            from veya.ipc.dispatcher import _get_or_create_memory_store
            context = WorkerContext(emit_event=_ignore_event, memory_database_path=Path(temporary) / "memory.sqlite")
            candidate = _get_or_create_memory_store(context).create_candidate("s-1", "Prefers concise answers")
            _get_or_create_memory_store(context).approve(candidate.id)

            from veya.conversation.context_builder import render_prompt
            from veya.conversation.models import SessionContext
            prompt = render_prompt(SessionContext(), "What should I focus on?", memory_context_block="- Prefers concise answers")
            self.assertIn("Prefers concise answers", prompt)
