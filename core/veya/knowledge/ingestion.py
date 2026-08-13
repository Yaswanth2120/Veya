"""Orchestrates one document's ingestion: validate path → extract text →
chunk → embed → store, emitting typed progress events throughout. A
document is only ever marked `ready` once chunking *and* embedding *and*
storage all succeeded — any failure along the way marks it `failed`
(or `unsupported` for an unsupported extension) instead, never `ready`.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Awaitable, Callable, List, Optional

from .chunking import chunk_text
from .embeddings import EmbeddingProvider
from .errors import (
    DocumentEmptyError,
    DocumentEncryptedError,
    DocumentMalformedError,
    DocumentOversizedError,
    DocumentPathInvalidError,
    DocumentUnsupportedError,
    EmbeddingUnavailableError,
    KnowledgeError,
)
from .extraction import extract_text, validate_document_path
from .models import ChunkingConfig, IngestionStatus
from .vector_store import VectorStore
from ..ipc import events

logger = logging.getLogger("veya.knowledge")

EmitEvent = Callable[[str, dict], Awaitable[None]]


class IngestionService:
    def __init__(
        self,
        store: VectorStore,
        documents_directory: Path,
        embedding_provider_factory: Callable[[], EmbeddingProvider],
        emit_event: EmitEvent,
        chunking_config: Optional[ChunkingConfig] = None,
        run_blocking: Optional[Callable] = None,
    ) -> None:
        self._store = store
        self._documents_directory = documents_directory
        self._embedding_provider_factory = embedding_provider_factory
        self._emit_event = emit_event
        self._chunking_config = chunking_config or ChunkingConfig()
        self._run_blocking = run_blocking or self._default_run_blocking

    @staticmethod
    async def _default_run_blocking(fn, *args):
        import asyncio

        return await asyncio.to_thread(fn, *args)

    async def ingest(
        self, session_id: str, document_id: str, file_name: str, file_extension: str, file_path: str
    ) -> None:
        await self._emit_event(
            "knowledge.ingestion_started",
            events.knowledge_ingestion_started(session_id, document_id, file_name),
        )
        await self._run_blocking(
            self._store.upsert_document, document_id, session_id, file_name, IngestionStatus.INDEXING, None
        )

        try:
            resolved_path = validate_document_path(file_path, self._documents_directory)
            text = extract_text(resolved_path, file_extension)
        except DocumentUnsupportedError as exc:
            await self._fail(session_id, document_id, file_name, IngestionStatus.UNSUPPORTED, exc.reason)
            return
        except (
            DocumentPathInvalidError,
            DocumentEncryptedError,
            DocumentMalformedError,
            DocumentEmptyError,
            DocumentOversizedError,
        ) as exc:
            await self._fail(session_id, document_id, file_name, IngestionStatus.FAILED, exc.reason)
            return

        chunks = chunk_text(text, document_id, session_id, file_name, self._chunking_config)
        await self._emit_event(
            "knowledge.ingestion_progress",
            events.knowledge_ingestion_progress(session_id, document_id, "chunked", len(chunks)),
        )

        try:
            provider = self._embedding_provider_factory()
            embeddings = await provider.embed([chunk.text for chunk in chunks])
        except EmbeddingUnavailableError as exc:
            await self._fail(session_id, document_id, file_name, IngestionStatus.FAILED, exc.reason)
            return
        except Exception as exc:  # noqa: BLE001 - never let an embedding-provider failure crash the worker
            logger.error("Unhandled %s while embedding document chunks", type(exc).__name__)
            await self._fail(session_id, document_id, file_name, IngestionStatus.FAILED, "Embedding failed.")
            return

        if len(embeddings) != len(chunks):
            await self._fail(
                session_id, document_id, file_name, IngestionStatus.FAILED, "Embedding count did not match chunk count."
            )
            return

        await self._emit_event(
            "knowledge.ingestion_progress",
            events.knowledge_ingestion_progress(session_id, document_id, "embedded", len(chunks)),
        )

        await self._run_blocking(self._store.replace_chunks, document_id, session_id, file_name, chunks, embeddings)
        await self._run_blocking(self._store.set_document_status, document_id, IngestionStatus.READY, None)

        await self._emit_event(
            "knowledge.ingestion_completed",
            events.knowledge_ingestion_completed(session_id, document_id, file_name, len(chunks)),
        )

    async def remove(self, document_id: str) -> None:
        await self._run_blocking(self._store.remove_document, document_id)

    async def status(self, document_id: str) -> Optional[IngestionStatus]:
        return await self._run_blocking(self._store.get_status, document_id)

    async def _fail(
        self, session_id: str, document_id: str, file_name: str, status: IngestionStatus, reason: str
    ) -> None:
        await self._run_blocking(self._store.set_document_status, document_id, status, reason)
        await self._emit_event(
            "knowledge.ingestion_failed",
            events.knowledge_ingestion_failed(session_id, document_id, file_name, status.value, reason),
        )
