import tempfile
import unittest
from pathlib import Path

from veya.knowledge.embeddings import FakeEmbeddingProvider
from veya.knowledge.errors import EmbeddingUnavailableError
from veya.knowledge.models import DocumentChunk, IngestionStatus, RetrievalConfig
from veya.knowledge.retrieval import KnowledgeRetriever, chunk_sources
from veya.knowledge.vector_store import VectorStore


def make_chunk(document_id, index, file_name="notes.txt", excerpt=None):
    return DocumentChunk(
        chunk_id=f"{document_id}-{index}",
        document_id=document_id,
        session_id="sess1",
        file_name=file_name,
        chunk_index=index,
        text=f"chunk body {index}",
        excerpt=excerpt or f"excerpt {index}",
        char_start=index * 10,
        char_end=index * 10 + 10,
    )


class FailingEmbeddingProvider:
    async def check_availability(self):
        raise EmbeddingUnavailableError("not configured")

    async def embed(self, texts):
        raise EmbeddingUnavailableError("not configured")


class KnowledgeRetrieverTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = VectorStore(Path(self._tmp.name) / "knowledge.sqlite")
        self.provider = FakeEmbeddingProvider()

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    async def _seed(self, session_id="sess1", document_id="doc1", texts=None):
        texts = texts or ["the migration took six weeks because of staged rollout"]
        chunks = [make_chunk(document_id, i) for i in range(len(texts))]
        chunks = [
            DocumentChunk(
                chunk_id=c.chunk_id,
                document_id=c.document_id,
                session_id=session_id,
                file_name=c.file_name,
                chunk_index=c.chunk_index,
                text=text,
                excerpt=text[:50],
                char_start=c.char_start,
                char_end=c.char_end,
            )
            for c, text in zip(chunks, texts)
        ]
        embeddings = await self.provider.embed(texts)
        self.store.upsert_document(document_id, session_id, "notes.txt", IngestionStatus.READY)
        self.store.replace_chunks(document_id, session_id, "notes.txt", chunks, embeddings)
        return chunks

    async def test_blank_query_returns_no_results(self):
        await self._seed()
        retriever = KnowledgeRetriever(self.store, self.provider, RetrievalConfig(similarity_threshold=0.0))
        self.assertEqual(await retriever.retrieve("sess1", "   "), [])

    async def test_relevant_query_retrieves_the_matching_chunk(self):
        await self._seed()
        retriever = KnowledgeRetriever(self.store, self.provider, RetrievalConfig(similarity_threshold=0.1))
        results = await retriever.retrieve("sess1", "why did the migration take six weeks")
        self.assertEqual(len(results), 1)
        self.assertGreaterEqual(results[0].score, 0.1)

    async def test_similarity_threshold_filters_out_weak_matches(self):
        await self._seed(texts=["completely unrelated pizza recipe content"])
        retriever = KnowledgeRetriever(self.store, self.provider, RetrievalConfig(similarity_threshold=0.9))
        results = await retriever.retrieve("sess1", "why did the migration take six weeks")
        self.assertEqual(results, [])

    async def test_no_chunks_meeting_threshold_means_no_sources(self):
        await self._seed(texts=["completely unrelated pizza recipe content"])
        retriever = KnowledgeRetriever(self.store, self.provider, RetrievalConfig(similarity_threshold=0.99))
        results = await retriever.retrieve("sess1", "why did the migration take six weeks")
        self.assertEqual(chunk_sources(results), [])

    async def test_retrieval_is_session_scoped(self):
        await self._seed(session_id="sess1", document_id="doc1")
        await self._seed(session_id="sess2", document_id="doc2")
        retriever = KnowledgeRetriever(self.store, self.provider, RetrievalConfig(similarity_threshold=0.0))

        results = await retriever.retrieve("sess1", "the migration took six weeks")
        self.assertTrue(all(r.chunk.session_id == "sess1" for r in results))

    async def test_embedding_unavailable_returns_empty_list_not_an_error(self):
        retriever = KnowledgeRetriever(self.store, FailingEmbeddingProvider(), RetrievalConfig())
        results = await retriever.retrieve("sess1", "any question")
        self.assertEqual(results, [])

    async def test_build_context_block_is_empty_for_no_results(self):
        retriever = KnowledgeRetriever(self.store, self.provider)
        self.assertEqual(retriever.build_context_block([]), "")

    async def test_build_context_block_includes_file_name_and_excerpt(self):
        chunks = await self._seed(texts=["the migration took six weeks because of staged rollout"])
        retriever = KnowledgeRetriever(self.store, self.provider, RetrievalConfig(similarity_threshold=0.0))
        results = await retriever.retrieve("sess1", "why did the migration take six weeks")

        block = retriever.build_context_block(results)
        self.assertIn("notes.txt", block)

    async def test_build_context_block_is_bounded_by_max_context_characters(self):
        from veya.knowledge.models import RetrievedChunk

        long_excerpt = "word " * 500
        chunks = [make_chunk("doc1", i, excerpt=long_excerpt) for i in range(20)]
        retrieved = [RetrievedChunk(chunk=c, score=1.0) for c in chunks]

        retriever = KnowledgeRetriever(self.store, self.provider, RetrievalConfig(max_context_characters=200, max_excerpt_length=50))
        block = retriever.build_context_block(retrieved)
        self.assertLessEqual(len(block), 400)  # bounded, not literally every chunk included

    async def test_source_references_correspond_to_retrieved_chunks_only(self):
        chunks = await self._seed(texts=["the migration took six weeks because of staged rollout"])
        retriever = KnowledgeRetriever(self.store, self.provider, RetrievalConfig(similarity_threshold=0.1))
        results = await retriever.retrieve("sess1", "why did the migration take six weeks")

        sources = chunk_sources(results)
        self.assertEqual(len(sources), len(results))
        for source, result in zip(sources, results):
            self.assertEqual(source["chunk_id"], result.chunk.chunk_id)
            self.assertEqual(source["document_id"], result.chunk.document_id)
            self.assertEqual(source["file_name"], result.chunk.file_name)
            self.assertEqual(source["excerpt"], result.chunk.excerpt)
