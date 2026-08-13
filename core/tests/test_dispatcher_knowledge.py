import tempfile
import unittest
from pathlib import Path

from veya.ipc.dispatcher import Dispatcher, WorkerContext
from veya.ipc.errors import ErrorCode, ProtocolError
from veya.ipc.protocol import Request
from veya.knowledge.embeddings import FakeEmbeddingProvider
from veya.knowledge.models import IngestionStatus


class RecordingEmitter:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    async def __call__(self, event_name: str, data: dict) -> None:
        self.events.append((event_name, data))

    def names(self) -> list[str]:
        return [name for name, _ in self.events]


class DispatcherKnowledgeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.documents_dir = Path(self._tmp.name) / "SessionDocuments"
        self.documents_dir.mkdir()
        self.knowledge_dir = Path(self._tmp.name) / "KnowledgeIndex"
        self.emitter = RecordingEmitter()
        self.context = WorkerContext(
            emit_event=self.emitter,
            documents_directory=self.documents_dir,
            knowledge_index_directory=self.knowledge_dir,
            embedding_provider_factory=FakeEmbeddingProvider,
        )
        self.dispatcher = Dispatcher()

    def tearDown(self):
        if self.context.vector_store is not None:
            self.context.vector_store.close()
        self._tmp.cleanup()

    def _write_document(self, relative_path: str, content: str) -> Path:
        path = self.documents_dir / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return path

    async def _wait_for_event(self, name: str, timeout: float = 2.0) -> dict:
        import asyncio
        import time

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for event_name, data in self.emitter.events:
                if event_name == name:
                    return data
            await asyncio.sleep(0.01)
        raise AssertionError(f"event {name!r} was not emitted within {timeout}s")

    async def test_ingest_requires_all_params(self):
        with self.assertRaises(ProtocolError) as ctx:
            await self.dispatcher.dispatch(
                Request(id="1", method="knowledge.ingest", params={"session_id": "s1"}), self.context
            )
        self.assertEqual(ctx.exception.code, ErrorCode.INVALID_PARAMS)

    async def test_ingest_of_a_supported_document_eventually_becomes_ready(self):
        path = self._write_document("s1/notes.txt", "The migration took six weeks. " * 20)

        result = await self.dispatcher.dispatch(
            Request(
                id="1",
                method="knowledge.ingest",
                params={
                    "session_id": "s1",
                    "document_id": "doc1",
                    "file_name": "notes.txt",
                    "file_extension": "txt",
                    "file_path": str(path),
                },
            ),
            self.context,
        )
        self.assertEqual(result, {"ok": True})

        await self._wait_for_event("knowledge.ingestion_completed")

        status_result = await self.dispatcher.dispatch(
            Request(id="2", method="knowledge.status", params={"document_id": "doc1"}), self.context
        )
        self.assertEqual(status_result, {"status": IngestionStatus.READY.value})

    async def test_ingest_of_an_unsupported_file_becomes_unsupported_not_ready(self):
        path = self._write_document("s1/notes.exe", "not a document")

        await self.dispatcher.dispatch(
            Request(
                id="1",
                method="knowledge.ingest",
                params={
                    "session_id": "s1",
                    "document_id": "doc1",
                    "file_name": "notes.exe",
                    "file_extension": "exe",
                    "file_path": str(path),
                },
            ),
            self.context,
        )

        await self._wait_for_event("knowledge.ingestion_failed")
        status_result = await self.dispatcher.dispatch(
            Request(id="2", method="knowledge.status", params={"document_id": "doc1"}), self.context
        )
        self.assertEqual(status_result, {"status": IngestionStatus.UNSUPPORTED.value})

    async def test_status_of_unknown_document_is_not_indexed(self):
        result = await self.dispatcher.dispatch(
            Request(id="1", method="knowledge.status", params={"document_id": "never-seen"}), self.context
        )
        self.assertEqual(result, {"status": IngestionStatus.NOT_INDEXED.value})

    async def test_remove_requires_document_id(self):
        with self.assertRaises(ProtocolError) as ctx:
            await self.dispatcher.dispatch(Request(id="1", method="knowledge.remove", params={}), self.context)
        self.assertEqual(ctx.exception.code, ErrorCode.INVALID_PARAMS)

    async def test_remove_clears_ready_status(self):
        path = self._write_document("s1/notes.txt", "The migration took six weeks. " * 20)
        await self.dispatcher.dispatch(
            Request(
                id="1",
                method="knowledge.ingest",
                params={
                    "session_id": "s1",
                    "document_id": "doc1",
                    "file_name": "notes.txt",
                    "file_extension": "txt",
                    "file_path": str(path),
                },
            ),
            self.context,
        )
        await self._wait_for_event("knowledge.ingestion_completed")

        await self.dispatcher.dispatch(Request(id="2", method="knowledge.remove", params={"document_id": "doc1"}), self.context)

        status_result = await self.dispatcher.dispatch(
            Request(id="3", method="knowledge.status", params={"document_id": "doc1"}), self.context
        )
        self.assertEqual(status_result, {"status": IngestionStatus.NOT_INDEXED.value})

    async def test_retrieve_requires_session_id_and_query(self):
        with self.assertRaises(ProtocolError) as ctx:
            await self.dispatcher.dispatch(
                Request(id="1", method="knowledge.retrieve", params={"session_id": "s1"}), self.context
            )
        self.assertEqual(ctx.exception.code, ErrorCode.INVALID_PARAMS)

    async def test_retrieve_returns_no_sources_before_anything_is_ingested(self):
        result = await self.dispatcher.dispatch(
            Request(id="1", method="knowledge.retrieve", params={"session_id": "s1", "query": "why?"}), self.context
        )
        self.assertEqual(result, {"sources": []})

    async def test_retrieve_returns_real_sources_after_ingestion(self):
        path = self._write_document("s1/notes.txt", "The migration took six weeks because of a staged rollout. " * 10)
        await self.dispatcher.dispatch(
            Request(
                id="1",
                method="knowledge.ingest",
                params={
                    "session_id": "s1",
                    "document_id": "doc1",
                    "file_name": "notes.txt",
                    "file_extension": "txt",
                    "file_path": str(path),
                },
            ),
            self.context,
        )
        await self._wait_for_event("knowledge.ingestion_completed")

        result = await self.dispatcher.dispatch(
            Request(
                id="2",
                method="knowledge.retrieve",
                params={"session_id": "s1", "query": "why did the migration take six weeks"},
            ),
            self.context,
        )

        self.assertTrue(result["sources"])
        self.assertEqual(result["sources"][0]["document_id"], "doc1")
        self.assertEqual(result["sources"][0]["file_name"], "notes.txt")

    async def test_retrieve_never_crosses_sessions(self):
        path = self._write_document("s1/notes.txt", "The migration took six weeks because of a staged rollout. " * 10)
        await self.dispatcher.dispatch(
            Request(
                id="1",
                method="knowledge.ingest",
                params={
                    "session_id": "s1",
                    "document_id": "doc1",
                    "file_name": "notes.txt",
                    "file_extension": "txt",
                    "file_path": str(path),
                },
            ),
            self.context,
        )
        await self._wait_for_event("knowledge.ingestion_completed")

        result = await self.dispatcher.dispatch(
            Request(
                id="2",
                method="knowledge.retrieve",
                params={"session_id": "different-session", "query": "why did the migration take six weeks"},
            ),
            self.context,
        )
        self.assertEqual(result, {"sources": []})
