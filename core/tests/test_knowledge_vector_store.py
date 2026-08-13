import tempfile
import unittest
from pathlib import Path

from veya.knowledge.models import DocumentChunk, IngestionStatus
from veya.knowledge.vector_store import VectorStore, cosine_similarity


def make_chunk(document_id="doc1", session_id="sess1", index=0, chunk_id=None, file_name="notes.txt"):
    return DocumentChunk(
        chunk_id=chunk_id or f"{document_id}-chunk-{index}",
        document_id=document_id,
        session_id=session_id,
        file_name=file_name,
        chunk_index=index,
        text=f"chunk text {index}",
        excerpt=f"excerpt {index}",
        char_start=index * 10,
        char_end=index * 10 + 10,
    )


class CosineSimilarityTests(unittest.TestCase):
    def test_identical_vectors_have_similarity_one(self):
        self.assertAlmostEqual(cosine_similarity([1.0, 0.0], [1.0, 0.0]), 1.0)

    def test_orthogonal_vectors_have_similarity_zero(self):
        self.assertAlmostEqual(cosine_similarity([1.0, 0.0], [0.0, 1.0]), 0.0)

    def test_opposite_vectors_have_similarity_negative_one(self):
        self.assertAlmostEqual(cosine_similarity([1.0, 0.0], [-1.0, 0.0]), -1.0)

    def test_mismatched_lengths_return_zero(self):
        self.assertEqual(cosine_similarity([1.0, 0.0], [1.0]), 0.0)

    def test_empty_vectors_return_zero(self):
        self.assertEqual(cosine_similarity([], []), 0.0)

    def test_zero_vector_returns_zero(self):
        self.assertEqual(cosine_similarity([0.0, 0.0], [1.0, 0.0]), 0.0)


class VectorStoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = VectorStore(Path(self._tmp.name) / "knowledge.sqlite")

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def test_status_is_none_for_an_unknown_document(self):
        self.assertIsNone(self.store.get_status("nonexistent"))

    def test_upsert_document_sets_status(self):
        self.store.upsert_document("doc1", "sess1", "notes.txt", IngestionStatus.INDEXING)
        self.assertEqual(self.store.get_status("doc1"), IngestionStatus.INDEXING)

    def test_set_document_status_updates_an_existing_document(self):
        self.store.upsert_document("doc1", "sess1", "notes.txt", IngestionStatus.INDEXING)
        self.store.set_document_status("doc1", IngestionStatus.READY)
        self.assertEqual(self.store.get_status("doc1"), IngestionStatus.READY)

    def test_replace_chunks_requires_matching_lengths(self):
        chunks = [make_chunk(index=0)]
        with self.assertRaises(ValueError):
            self.store.replace_chunks("doc1", "sess1", "notes.txt", chunks, [])

    def test_replace_chunks_then_search_returns_them(self):
        self.store.upsert_document("doc1", "sess1", "notes.txt", IngestionStatus.READY)
        chunks = [make_chunk(index=0), make_chunk(index=1)]
        embeddings = [[1.0, 0.0], [0.0, 1.0]]
        self.store.replace_chunks("doc1", "sess1", "notes.txt", chunks, embeddings)

        results = self.store.search("sess1", [1.0, 0.0], top_k=5)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0][0].chunk_id, chunks[0].chunk_id)
        self.assertAlmostEqual(results[0][1], 1.0)

    def test_replace_chunks_is_a_clean_replace_not_an_accumulation(self):
        self.store.upsert_document("doc1", "sess1", "notes.txt", IngestionStatus.READY)
        self.store.replace_chunks("doc1", "sess1", "notes.txt", [make_chunk(index=0)], [[1.0, 0.0]])
        self.store.replace_chunks("doc1", "sess1", "notes.txt", [make_chunk(index=0)], [[1.0, 0.0]])

        results = self.store.search("sess1", [1.0, 0.0], top_k=10)
        self.assertEqual(len(results), 1)

    def test_search_only_returns_chunks_from_ready_documents(self):
        self.store.upsert_document("doc1", "sess1", "notes.txt", IngestionStatus.INDEXING)
        self.store.replace_chunks("doc1", "sess1", "notes.txt", [make_chunk(index=0)], [[1.0, 0.0]])

        results = self.store.search("sess1", [1.0, 0.0], top_k=5)
        self.assertEqual(results, [])

        self.store.set_document_status("doc1", IngestionStatus.READY)
        results = self.store.search("sess1", [1.0, 0.0], top_k=5)
        self.assertEqual(len(results), 1)

    def test_search_is_session_scoped(self):
        self.store.upsert_document("doc1", "sess1", "notes.txt", IngestionStatus.READY)
        self.store.replace_chunks("doc1", "sess1", "notes.txt", [make_chunk(document_id="doc1", session_id="sess1")], [[1.0, 0.0]])
        self.store.upsert_document("doc2", "sess2", "other.txt", IngestionStatus.READY)
        self.store.replace_chunks("doc2", "sess2", "other.txt", [make_chunk(document_id="doc2", session_id="sess2")], [[1.0, 0.0]])

        results_sess1 = self.store.search("sess1", [1.0, 0.0], top_k=10)
        results_sess2 = self.store.search("sess2", [1.0, 0.0], top_k=10)

        self.assertEqual(len(results_sess1), 1)
        self.assertEqual(results_sess1[0][0].document_id, "doc1")
        self.assertEqual(len(results_sess2), 1)
        self.assertEqual(results_sess2[0][0].document_id, "doc2")

    def test_search_respects_top_k(self):
        self.store.upsert_document("doc1", "sess1", "notes.txt", IngestionStatus.READY)
        chunks = [make_chunk(index=i) for i in range(5)]
        embeddings = [[1.0, 0.0]] * 5
        self.store.replace_chunks("doc1", "sess1", "notes.txt", chunks, embeddings)

        results = self.store.search("sess1", [1.0, 0.0], top_k=2)
        self.assertEqual(len(results), 2)

    def test_search_orders_by_similarity_descending(self):
        self.store.upsert_document("doc1", "sess1", "notes.txt", IngestionStatus.READY)
        chunks = [make_chunk(index=0, chunk_id="a"), make_chunk(index=1, chunk_id="b"), make_chunk(index=2, chunk_id="c")]
        embeddings = [[0.1, 0.99], [1.0, 0.0], [0.5, 0.5]]
        self.store.replace_chunks("doc1", "sess1", "notes.txt", chunks, embeddings)

        results = self.store.search("sess1", [1.0, 0.0], top_k=10)
        self.assertEqual([r[0].chunk_id for r in results], ["b", "c", "a"])

    def test_remove_document_deletes_document_and_its_chunks(self):
        self.store.upsert_document("doc1", "sess1", "notes.txt", IngestionStatus.READY)
        self.store.replace_chunks("doc1", "sess1", "notes.txt", [make_chunk(index=0)], [[1.0, 0.0]])

        self.store.remove_document("doc1")

        self.assertIsNone(self.store.get_status("doc1"))
        self.assertEqual(self.store.search("sess1", [1.0, 0.0], top_k=10), [])

    def test_remove_document_does_not_affect_other_documents(self):
        self.store.upsert_document("doc1", "sess1", "notes.txt", IngestionStatus.READY)
        self.store.replace_chunks("doc1", "sess1", "notes.txt", [make_chunk(document_id="doc1", index=0)], [[1.0, 0.0]])
        self.store.upsert_document("doc2", "sess1", "other.txt", IngestionStatus.READY)
        self.store.replace_chunks("doc2", "sess1", "other.txt", [make_chunk(document_id="doc2", index=0)], [[1.0, 0.0]])

        self.store.remove_document("doc1")

        self.assertIsNone(self.store.get_status("doc1"))
        self.assertEqual(self.store.get_status("doc2"), IngestionStatus.READY)
        results = self.store.search("sess1", [1.0, 0.0], top_k=10)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0][0].document_id, "doc2")
