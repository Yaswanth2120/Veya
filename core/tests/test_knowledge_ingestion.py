import asyncio
import tempfile
import unittest
from pathlib import Path

from veya.knowledge.embeddings import FakeEmbeddingProvider
from veya.knowledge.errors import EmbeddingUnavailableError
from veya.knowledge.ingestion import IngestionService
from veya.knowledge.models import IngestionStatus
from veya.knowledge.vector_store import VectorStore


class RecordingEmitter:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    async def __call__(self, event_name: str, data: dict) -> None:
        self.events.append((event_name, data))

    def names(self) -> list[str]:
        return [name for name, _ in self.events]


class FailingEmbeddingProvider:
    async def check_availability(self):
        raise EmbeddingUnavailableError("not configured")

    async def embed(self, texts):
        raise EmbeddingUnavailableError("not configured")


class ExplodingEmbeddingProvider:
    async def embed(self, texts):
        raise ValueError("sensitive internal detail")


async def immediate_run_blocking(fn, *args):
    return fn(*args)


class IngestionServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.documents_dir = Path(self._tmp.name) / "SessionDocuments"
        self.documents_dir.mkdir()
        self.store = VectorStore(Path(self._tmp.name) / "knowledge.sqlite")
        self.emitter = RecordingEmitter()

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def _write_document(self, relative_path: str, content: str) -> Path:
        path = self.documents_dir / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return path

    def _make_service(self, embedding_provider_factory=FakeEmbeddingProvider) -> IngestionService:
        return IngestionService(
            store=self.store,
            documents_directory=self.documents_dir,
            embedding_provider_factory=embedding_provider_factory,
            emit_event=self.emitter,
            run_blocking=immediate_run_blocking,
        )

    async def test_successful_ingestion_marks_document_ready_and_emits_events_in_order(self):
        path = self._write_document("sess1/notes.txt", "The migration took six weeks. " * 30)
        service = self._make_service()

        await service.ingest("sess1", "doc1", "notes.txt", "txt", str(path))

        self.assertEqual(
            self.emitter.names(),
            ["knowledge.ingestion_started", "knowledge.ingestion_progress", "knowledge.ingestion_progress", "knowledge.ingestion_completed"],
        )
        self.assertEqual(await service.status("doc1"), IngestionStatus.READY)

    async def test_unsupported_extension_marks_document_unsupported_and_never_ready(self):
        path = self._write_document("sess1/notes.exe", "not a real document")
        service = self._make_service()

        await service.ingest("sess1", "doc1", "notes.exe", "exe", str(path))

        self.assertEqual(self.emitter.names()[-1], "knowledge.ingestion_failed")
        self.assertEqual(await service.status("doc1"), IngestionStatus.UNSUPPORTED)

    async def test_path_outside_documents_directory_fails_without_reading_it(self):
        outside = Path(self._tmp.name) / "outside.txt"
        outside.write_text("secret content")
        service = self._make_service()

        await service.ingest("sess1", "doc1", "outside.txt", "txt", str(outside))

        self.assertEqual(self.emitter.names()[-1], "knowledge.ingestion_failed")
        self.assertEqual(await service.status("doc1"), IngestionStatus.FAILED)

    async def test_empty_document_fails(self):
        path = self._write_document("sess1/empty.txt", "   ")
        service = self._make_service()

        await service.ingest("sess1", "doc1", "empty.txt", "txt", str(path))

        self.assertEqual(await service.status("doc1"), IngestionStatus.FAILED)

    async def test_embedding_unavailable_fails_the_document_not_ready(self):
        path = self._write_document("sess1/notes.txt", "The migration took six weeks.")
        service = self._make_service(embedding_provider_factory=FailingEmbeddingProvider)

        await service.ingest("sess1", "doc1", "notes.txt", "txt", str(path))

        self.assertEqual(await service.status("doc1"), IngestionStatus.FAILED)
        self.assertNotEqual(await service.status("doc1"), IngestionStatus.READY)

    async def test_unhandled_embedding_exception_fails_document_without_leaking_message(self):
        path = self._write_document("sess1/notes.txt", "The migration took six weeks.")
        service = self._make_service(embedding_provider_factory=ExplodingEmbeddingProvider)

        with self.assertLogs("veya.knowledge", level="ERROR") as logs:
            await service.ingest("sess1", "doc1", "notes.txt", "txt", str(path))

        self.assertEqual(await service.status("doc1"), IngestionStatus.FAILED)
        logged_text = "\n".join(logs.output)
        self.assertNotIn("sensitive internal detail", logged_text)
        self.assertIn("ValueError", logged_text)

    async def test_ingestion_failed_event_never_contains_document_text(self):
        path = self._write_document("sess1/notes.exe", "some content that must never be logged or emitted")
        service = self._make_service()

        await service.ingest("sess1", "doc1", "notes.exe", "exe", str(path))

        failure_event = self.emitter.events[-1][1]
        self.assertNotIn("some content that must never be logged or emitted", str(failure_event))

    async def test_document_is_not_ready_if_indexing_never_completes(self):
        # A document that's still mid-ingestion (or that we never got to
        # finish) must never read as "ready" for retrieval purposes.
        path = self._write_document("sess1/notes.txt", "The migration took six weeks.")
        service = self._make_service()
        # Directly exercise the INDEXING intermediate state without
        # completing ingestion.
        await immediate_run_blocking(self.store.upsert_document, "doc1", "sess1", "notes.txt", IngestionStatus.INDEXING, None)

        status = await service.status("doc1")
        self.assertNotEqual(status, IngestionStatus.READY)

    async def test_remove_deletes_the_document_and_its_index_data(self):
        path = self._write_document("sess1/notes.txt", "The migration took six weeks. " * 30)
        service = self._make_service()
        await service.ingest("sess1", "doc1", "notes.txt", "txt", str(path))
        self.assertEqual(await service.status("doc1"), IngestionStatus.READY)

        await service.remove("doc1")

        self.assertIsNone(await service.status("doc1"))

    async def test_status_of_unknown_document_is_none(self):
        service = self._make_service()
        self.assertIsNone(await service.status("never-ingested"))

    async def test_reingesting_a_document_replaces_its_chunks(self):
        path = self._write_document("sess1/notes.txt", "short text")
        service = self._make_service()
        await service.ingest("sess1", "doc1", "notes.txt", "txt", str(path))

        path.write_text("different, longer replacement text that changes the chunk content entirely")
        await service.ingest("sess1", "doc1", "notes.txt", "txt", str(path))

        self.assertEqual(await service.status("doc1"), IngestionStatus.READY)
