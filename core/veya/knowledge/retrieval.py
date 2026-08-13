"""Session-scoped retrieval: embeds a query, searches the vector store,
applies a relevance threshold, and assembles a bounded prompt context
block. Never retrieves across sessions, never dumps whole documents into
a prompt.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import List, Optional

from .embeddings import EmbeddingProvider
from .errors import EmbeddingUnavailableError
from .models import RetrievalConfig, RetrievedChunk
from .vector_store import VectorStore


def chunk_sources(retrieved: List[RetrievedChunk]) -> List[dict]:
    """The wire shape of `answer.completed`'s `sources` field — a
    structured reference per actually-retrieved chunk, never invented.
    Shared by `ConversationOrchestrator` and `dispatcher.py`'s
    `knowledge.retrieve` handler so both build sources identically."""
    return [
        {
            "document_id": item.chunk.document_id,
            "file_name": item.chunk.file_name,
            "chunk_id": item.chunk.chunk_id,
            "excerpt": item.chunk.excerpt,
        }
        for item in retrieved
    ]


class KnowledgeRetriever:
    def __init__(
        self,
        store: VectorStore,
        embedding_provider: EmbeddingProvider,
        config: Optional[RetrievalConfig] = None,
    ) -> None:
        self._store = store
        self._embedding_provider = embedding_provider
        self._config = config or RetrievalConfig()

    async def retrieve(self, session_id: str, query_text: str) -> List[RetrievedChunk]:
        """Returns `[]` (never raises) if the query is blank or if
        embedding the query fails — retrieval unavailability must never
        break answer generation; it just means the answer proceeds
        without document sources (see `docs/KNOWLEDGE_RETRIEVAL.md`)."""
        stripped = query_text.strip()
        if not stripped:
            return []

        try:
            embeddings = await self._embedding_provider.embed([stripped])
        except EmbeddingUnavailableError:
            return []
        if not embeddings:
            return []
        query_embedding = embeddings[0]

        results = await asyncio.to_thread(self._store.search, session_id, query_embedding, self._config.top_k)
        return [
            RetrievedChunk(chunk=chunk, score=score)
            for chunk, score in results
            if score >= self._config.similarity_threshold
        ]

    def build_context_block(self, retrieved: List[RetrievedChunk]) -> str:
        """Assembles a clearly delimited context block for the prompt,
        bounded by `max_context_characters` — truncates individual
        excerpts to `max_excerpt_length` and stops adding chunks once the
        overall budget is spent, rather than ever including a whole
        document."""
        if not retrieved:
            return ""

        lines = ["Supporting context from the user's session documents (use only for document-specific claims):"]
        budget = self._config.max_context_characters
        for item in retrieved:
            excerpt = item.chunk.excerpt[: self._config.max_excerpt_length]
            line = f"- [{item.chunk.file_name}] {excerpt}"
            if budget - len(line) < 0:
                break
            lines.append(line)
            budget -= len(line)

        return "\n".join(lines)
