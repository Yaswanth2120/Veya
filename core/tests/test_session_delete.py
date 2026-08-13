import tempfile
import unittest
from pathlib import Path

from veya.ipc.dispatcher import Dispatcher, WorkerContext, _get_or_create_memory_store, _get_or_create_report_store
from veya.ipc.protocol import Request
from veya.knowledge.models import DocumentChunk, IngestionStatus


async def _ignore_event(name, data):
    return None


class SessionDeleteDataTests(unittest.IsolatedAsyncioTestCase):
    async def test_deletes_coding_workspace_architecture_and_report_but_keeps_approved_memory(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            context = WorkerContext(
                emit_event=_ignore_event,
                coding_workspace_directory=base / "coding",
                architecture_state_directory=base / "arch",
                knowledge_index_directory=base / "knowledge",
                memory_database_path=base / "memory.sqlite",
                report_store_directory=base / "reports",
            )
            self.addCleanup(context.close)
            dispatcher = Dispatcher()

            await dispatcher.dispatch(Request(id="1", method="coding.upsert_file", params={"session_id": "s-1", "name": "main.py", "language": "python", "content": "x = 1"}), context)
            await dispatcher.dispatch(Request(id="2", method="design.replace", params={"session_id": "s-1", "title": "T", "nodes": [], "edges": [], "base_version": None}), context)

            memory_store = _get_or_create_memory_store(context)
            proposed = memory_store.create_candidate("s-1", "proposed fact")
            approved = memory_store.create_candidate("s-1", "approved fact")
            memory_store.approve(approved.id)

            from veya.conversation.report import SessionReport
            _get_or_create_report_store(context).save(SessionReport(session_id="s-1", summary="a report"))

            result = await dispatcher.dispatch(Request(id="3", method="session.delete_data", params={"session_id": "s-1"}), context)
            self.assertEqual(result, {"ok": True})

            files = await dispatcher.dispatch(Request(id="4", method="coding.list_files", params={"session_id": "s-1"}), context)
            self.assertEqual(files["files"], [])

            design = await dispatcher.dispatch(Request(id="5", method="design.get", params={"session_id": "s-1"}), context)
            self.assertEqual(design["version"], 1)  # back to a fresh, never-persisted state

            remaining = memory_store.list()
            remaining_ids = {m.id for m in remaining}
            self.assertNotIn(proposed.id, remaining_ids)
            self.assertIn(approved.id, remaining_ids)  # approved memory outlives the session

            self.assertIsNone(_get_or_create_report_store(context).get("s-1"))

    async def test_deletes_knowledge_index_documents_for_the_session(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            context = WorkerContext(emit_event=_ignore_event, knowledge_index_directory=base / "knowledge")
            self.addCleanup(context.close)
            from veya.knowledge.vector_store import VectorStore

            vector_store = VectorStore(base / "knowledge" / "knowledge.sqlite")
            vector_store.upsert_document(document_id="d1", session_id="s-1", file_name="notes.txt", status=IngestionStatus.READY)
            vector_store.replace_chunks(
                document_id="d1", session_id="s-1", file_name="notes.txt",
                chunks=[DocumentChunk(chunk_id="c1", document_id="d1", session_id="s-1", file_name="notes.txt", chunk_index=0, text="hello", excerpt="hello", char_start=0, char_end=5)],
                embeddings=[[0.1, 0.2]],
            )
            context.vector_store = vector_store

            dispatcher = Dispatcher()
            await dispatcher.dispatch(Request(id="1", method="session.delete_data", params={"session_id": "s-1"}), context)

            status = await dispatcher.dispatch(Request(id="2", method="knowledge.status", params={"document_id": "d1"}), context)
            self.assertEqual(status["status"], "not_indexed")
