"""RPC method dispatch.

`Dispatcher.dispatch(request, context)` is the single entry point the
worker's stdin loop calls for every incoming `Request`. Handlers are
plain `async def(params: dict, context: WorkerContext) -> dict` functions
registered by method name; unknown methods and bad params both become
typed `ProtocolError`s, never bare exceptions.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Optional

from .errors import ErrorCode, ProtocolError
from .protocol import PROTOCOL_VERSION, Request
from ..transcription.engine import TranscriptionEngine, TranscriptionSetupError, default_whisper_engine_factory
from ..transcription.session import TranscriptionSession
from ..conversation.models import SessionContext
from ..conversation.orchestrator import ConversationOrchestrator
from ..knowledge.embeddings import EmbeddingProvider, default_embedding_provider_factory
from ..knowledge.ingestion import IngestionService
from ..knowledge.models import IngestionStatus
from ..knowledge.retrieval import KnowledgeRetriever, chunk_sources
from ..knowledge.vector_store import VectorStore
from ..llm.errors import LLMUnavailableError
from ..llm.ollama_provider import default_ollama_provider_factory
from ..llm.provider import LLMProvider
from ..coding.analysis import analyze_python
from ..coding.execution import run_python
from ..coding.workspace import CodeWorkspaceStore
from ..coding.assistant import propose as propose_code
from ..design.state import ArchitectureEdge, ArchitectureNode, ArchitectureState, ArchitectureStore, mermaid
from ..design.assistant import propose_followup as propose_design_followup
from ..design import export as design_export
from ..conversation.report import SessionReport, analyze_session
from ..conversation.report_store import ReportStore
from ..memory.store import MemoryStore
from .. import __version__

logger = logging.getLogger("veya.dispatcher")

EmitEvent = Callable[[str, dict], Awaitable[None]]
Handler = Callable[[dict, "WorkerContext"], Awaitable[dict]]

# Raw PCM bytes per chunk, before base64 encoding. At 16kHz mono 16-bit PCM
# this is ~2 seconds of audio — comfortably above the ~0.5s chunks the
# Swift side actually sends, while still bounding worst-case memory/CPU
# per request. See docs/REALTIME_TRANSCRIPTION.md.
MAX_AUDIO_CHUNK_BYTES = 65536


def _default_documents_directory() -> Path:
    """Must agree with `CreateSessionViewModel`'s
    `~/Library/Application Support/Veya/SessionDocuments/` — the one
    filesystem boundary `knowledge.ingest` is ever allowed to read
    beneath. Swift always passes this explicitly via
    `VEYA_DOCUMENTS_DIRECTORY` in production (see
    `PythonWorkerManager.swift`); this fallback only matters for a
    worker launched without that env var set (e.g. directly for manual
    testing)."""
    value = os.environ.get("VEYA_DOCUMENTS_DIRECTORY")
    if value:
        return Path(value)
    return Path.home() / "Library" / "Application Support" / "Veya" / "SessionDocuments"


def _default_knowledge_index_directory() -> Path:
    value = os.environ.get("VEYA_KNOWLEDGE_INDEX_DIRECTORY")
    if value:
        return Path(value)
    return Path.home() / "Library" / "Application Support" / "Veya" / "KnowledgeIndex"


def _default_coding_workspace_directory() -> Path:
    value = os.environ.get("VEYA_CODING_WORKSPACE_DIRECTORY")
    return Path(value) if value else Path.home() / "Library" / "Application Support" / "Veya" / "CodingWorkspaces"


def _default_architecture_state_directory() -> Path:
    value = os.environ.get("VEYA_ARCHITECTURE_STATE_DIRECTORY")
    return Path(value) if value else Path.home() / "Library" / "Application Support" / "Veya" / "ArchitectureStates"


def _default_memory_database_path() -> Path:
    value = os.environ.get("VEYA_MEMORY_DATABASE_PATH")
    return Path(value) if value else Path.home() / "Library" / "Application Support" / "Veya" / "Memory" / "memory.sqlite"


def _default_report_store_directory() -> Path:
    value = os.environ.get("VEYA_REPORT_STORE_DIRECTORY")
    return Path(value) if value else Path.home() / "Library" / "Application Support" / "Veya" / "SessionReports"


@dataclass
class WorkerContext:
    """Mutable state shared across handler calls for the lifetime of the
    worker process. One instance per `Worker`."""

    emit_event: EmitEvent
    shutdown_event: asyncio.Event = field(default_factory=asyncio.Event)
    active_session_id: Optional[str] = None
    feed_task: Optional[asyncio.Task] = None
    transcription_session: Optional[TranscriptionSession] = None
    transcription_engine_factory: Callable[[], TranscriptionEngine] = field(
        default=default_whisper_engine_factory
    )
    session_context: SessionContext = field(default_factory=SessionContext)
    llm_provider_factory: Callable[[], LLMProvider] = field(default=default_ollama_provider_factory)
    conversation_orchestrator: Optional[ConversationOrchestrator] = None

    # Section 9 — knowledge/document ingestion + retrieval.
    documents_directory: Path = field(default_factory=_default_documents_directory)
    knowledge_index_directory: Path = field(default_factory=_default_knowledge_index_directory)
    embedding_provider_factory: Callable[[], EmbeddingProvider] = field(default=default_embedding_provider_factory)
    # All three lazily created on first knowledge.* RPC (see
    # `_get_or_create_*` below) — never touches disk/the embedding
    # provider just from constructing a `WorkerContext`, which matters
    # for every existing test that never exercises knowledge features.
    vector_store: Optional[VectorStore] = None
    ingestion_service: Optional[IngestionService] = None
    retriever: Optional[KnowledgeRetriever] = None
    coding_workspace_directory: Path = field(default_factory=_default_coding_workspace_directory)
    code_workspace_store: Optional[CodeWorkspaceStore] = None
    architecture_state_directory: Path = field(default_factory=_default_architecture_state_directory)
    architecture_store: Optional[ArchitectureStore] = None

    # Section 13 — session reports + durable memory.
    memory_database_path: Path = field(default_factory=_default_memory_database_path)
    memory_store: Optional[MemoryStore] = None
    report_store_directory: Path = field(default_factory=_default_report_store_directory)
    report_store: Optional[ReportStore] = None

    async def cancel_feed_task_if_running(self) -> None:
        if self.feed_task is None:
            return
        self.feed_task.cancel()
        try:
            await self.feed_task
        except asyncio.CancelledError:
            pass
        finally:
            self.feed_task = None

    async def close_transcription_session_if_running(self) -> None:
        if self.conversation_orchestrator is not None:
            orchestrator = self.conversation_orchestrator
            self.conversation_orchestrator = None
            await orchestrator.close()
        if self.transcription_session is None:
            return
        session = self.transcription_session
        self.transcription_session = None
        await session.close()


def _get_or_create_vector_store(context: WorkerContext) -> VectorStore:
    if context.vector_store is None:
        context.vector_store = VectorStore(context.knowledge_index_directory / "knowledge.sqlite")
    return context.vector_store


def _get_or_create_ingestion_service(context: WorkerContext) -> IngestionService:
    if context.ingestion_service is None:
        context.ingestion_service = IngestionService(
            store=_get_or_create_vector_store(context),
            documents_directory=context.documents_directory,
            embedding_provider_factory=context.embedding_provider_factory,
            emit_event=context.emit_event,
        )
    return context.ingestion_service


def _get_or_create_retriever(context: WorkerContext) -> Optional[KnowledgeRetriever]:
    """Returns `None` (never raises) if the embedding provider can't even
    be constructed (e.g. a rejected non-loopback `VEYA_OLLAMA_URL`) —
    retrieval unavailability must never break question answering, it just
    means answers proceed without document sources."""
    if context.retriever is not None:
        return context.retriever
    try:
        provider = context.embedding_provider_factory()
    except Exception as exc:  # noqa: BLE001
        logger.info("Embedding provider unavailable for retrieval (%s).", type(exc).__name__)
        return None
    context.retriever = KnowledgeRetriever(store=_get_or_create_vector_store(context), embedding_provider=provider)
    return context.retriever


def _get_or_create_code_workspace_store(context: WorkerContext) -> CodeWorkspaceStore:
    if context.code_workspace_store is None:
        context.code_workspace_store = CodeWorkspaceStore(context.coding_workspace_directory)
    return context.code_workspace_store


def _get_or_create_architecture_store(context: WorkerContext) -> ArchitectureStore:
    if context.architecture_store is None:
        context.architecture_store = ArchitectureStore(context.architecture_state_directory)
    return context.architecture_store


def _get_or_create_memory_store(context: WorkerContext) -> MemoryStore:
    if context.memory_store is None:
        context.memory_store = MemoryStore(context.memory_database_path)
    return context.memory_store


def _get_or_create_report_store(context: WorkerContext) -> ReportStore:
    if context.report_store is None:
        context.report_store = ReportStore(context.report_store_directory)
    return context.report_store


async def _handle_ping(params: dict, context: WorkerContext) -> dict:
    return {"pong": True}


async def _handle_info(params: dict, context: WorkerContext) -> dict:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "worker_version": __version__,
        "pid": os.getpid(),
    }


async def _handle_system_llm_status(params: dict, context: WorkerContext) -> dict:
    try:
        provider = context.llm_provider_factory()
    except Exception as exc:  # noqa: BLE001 - e.g. a rejected non-loopback VEYA_OLLAMA_URL; report, never raise
        return {"reachable": False, "base_url": "", "configured_model": "", "model_installed": False, "available_models": [], "error": type(exc).__name__}
    describe = getattr(provider, "describe_status", None)
    if describe is None:
        # A test/fake `llm_provider_factory` that doesn't implement this —
        # never a crash, just "we don't know."
        return {"reachable": False, "base_url": "", "configured_model": "", "model_installed": False, "available_models": [], "error": "unsupported"}
    return await describe()


async def _handle_session_delete_data(params: dict, context: WorkerContext) -> dict:
    """Cleans up every piece of Python-owned derived data for a session
    that's being deleted from Swift/GRDB — the coding workspace,
    architecture state, knowledge-index documents/chunks, never-approved
    memory candidates, and the cached session report. Approved memory is
    deliberately untouched (see `MemoryStore.delete_proposed_for_session`)
    — it's meant to outlive the session it came from. Idempotent: deleting
    data for a session with nothing stored is not an error."""
    session_id = _code_session_id(params)
    _get_or_create_code_workspace_store(context).delete_session(session_id)
    _get_or_create_architecture_store(context).delete_session(session_id)
    _get_or_create_vector_store(context).remove_session(session_id)
    _get_or_create_memory_store(context).delete_proposed_for_session(session_id)
    _get_or_create_report_store(context).delete(session_id)
    return {"ok": True}


async def _handle_shutdown(params: dict, context: WorkerContext) -> dict:
    await context.cancel_feed_task_if_running()
    await context.close_transcription_session_if_running()
    context.shutdown_event.set()
    return {"ok": True}


def _string_param(params: dict, key: str) -> str:
    value = params.get(key)
    return value if isinstance(value, str) else ""


async def _handle_session_start(params: dict, context: WorkerContext) -> dict:
    session_id = params.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise ProtocolError(ErrorCode.INVALID_PARAMS, "'session_id' is required and must be a string.")
    context.active_session_id = session_id
    # Every field is optional/blank-safe (see `SessionContext`) — existing
    # callers that only ever sent `session_id` (Section 6/7 tests, the
    # mock feed's own session lifecycle) keep working unchanged.
    context.session_context = SessionContext(
        session_type=_string_param(params, "session_type"),
        title=_string_param(params, "title"),
        company=_string_param(params, "company"),
        role_or_topic=_string_param(params, "role_or_topic"),
        description=_string_param(params, "session_description"),
        notes=_string_param(params, "notes"),
        preferred_answer_style=_string_param(params, "preferred_answer_style"),
        preferred_programming_language=_string_param(params, "preferred_programming_language"),
        custom_instructions=_string_param(params, "custom_instructions"),
    )
    logger.info("session.start session_id=%s", session_id)
    return {"ok": True}


async def _handle_session_stop(params: dict, context: WorkerContext) -> dict:
    session_id = params.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise ProtocolError(ErrorCode.INVALID_PARAMS, "'session_id' is required and must be a string.")
    if context.active_session_id != session_id:
        raise ProtocolError(ErrorCode.SESSION_NOT_FOUND, f"No active session with id {session_id!r}.")
    await context.cancel_feed_task_if_running()
    await context.close_transcription_session_if_running()
    context.active_session_id = None
    logger.info("session.stop session_id=%s", session_id)
    return {"ok": True}


async def _handle_mock_start_live_feed(params: dict, context: WorkerContext) -> dict:
    # Imported lazily to avoid a module-level import cycle between
    # dispatcher.py and mock/live_feed.py (the latter imports `events`,
    # not `dispatcher`, but keeping the import local here also makes the
    # dependency direction explicit: dispatcher -> mock, never reverse).
    from ..mock.live_feed import run_live_feed

    session_id = params.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise ProtocolError(ErrorCode.INVALID_PARAMS, "'session_id' is required and must be a string.")
    if context.active_session_id != session_id:
        raise ProtocolError(ErrorCode.SESSION_NOT_FOUND, f"No active session with id {session_id!r}.")
    if context.feed_task is not None and not context.feed_task.done():
        raise ProtocolError(ErrorCode.ALREADY_RUNNING, "A mock live feed is already running for this worker.")

    context.feed_task = asyncio.create_task(run_live_feed(session_id, context.emit_event))
    logger.info("mock.start_live_feed session_id=%s", session_id)
    return {"ok": True}


async def _handle_mock_stop_live_feed(params: dict, context: WorkerContext) -> dict:
    session_id = params.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise ProtocolError(ErrorCode.INVALID_PARAMS, "'session_id' is required and must be a string.")
    if context.feed_task is None:
        raise ProtocolError(ErrorCode.NOT_RUNNING, "No mock live feed is currently running.")
    await context.cancel_feed_task_if_running()
    logger.info("mock.stop_live_feed session_id=%s", session_id)
    return {"ok": True}


async def _handle_transcription_start(params: dict, context: WorkerContext) -> dict:
    session_id = params.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise ProtocolError(ErrorCode.INVALID_PARAMS, "'session_id' is required and must be a string.")
    if context.active_session_id != session_id:
        raise ProtocolError(ErrorCode.SESSION_NOT_FOUND, f"No active session with id {session_id!r}.")

    sample_rate_hz = params.get("sample_rate_hz")
    channels = params.get("channels")
    encoding = params.get("encoding")
    if not isinstance(sample_rate_hz, int) or sample_rate_hz <= 0:
        raise ProtocolError(ErrorCode.INVALID_PARAMS, "'sample_rate_hz' must be a positive integer.")
    if channels != 1:
        raise ProtocolError(ErrorCode.INVALID_PARAMS, "Only mono ('channels': 1) audio is supported.")
    if encoding != "pcm_s16le":
        raise ProtocolError(ErrorCode.INVALID_PARAMS, "Only the 'pcm_s16le' encoding is supported.")
    if context.transcription_session is not None:
        raise ProtocolError(ErrorCode.ALREADY_RUNNING, "A transcription session is already running for this worker.")

    try:
        engine = context.transcription_engine_factory()
    except TranscriptionSetupError as exc:
        raise ProtocolError(ErrorCode.TRANSCRIPTION_UNAVAILABLE, exc.reason) from exc

    # Whisper being available is required for transcription.start to
    # succeed at all (above); Ollama is not — real transcription must
    # never fall back to the mock feed merely because answer intelligence
    # isn't available (see docs/QUESTION_AND_ANSWER_INTELLIGENCE.md).
    # A failure here is caught, logged type-only, and simply leaves
    # `llm_provider` `None` for this session.
    llm_provider = None
    try:
        candidate_provider = context.llm_provider_factory()
        await candidate_provider.check_availability()
        llm_provider = candidate_provider
    except LLMUnavailableError as exc:
        logger.info("Ollama unavailable for this session (%s); answer intelligence disabled.", type(exc).__name__)
    except Exception as exc:  # noqa: BLE001 - never let an LLM setup failure break transcription
        logger.error("Unhandled %s checking Ollama availability; answer intelligence disabled.", type(exc).__name__)

    orchestrator = ConversationOrchestrator(
        session_id=session_id,
        session_context=context.session_context,
        emit_event=context.emit_event,
        llm_provider=llm_provider,
        retriever=_get_or_create_retriever(context),
        memory_texts=_get_or_create_memory_store(context).approved_texts(),
    )
    context.conversation_orchestrator = orchestrator

    context.transcription_session = TranscriptionSession(
        session_id=session_id,
        sample_rate_hz=sample_rate_hz,
        engine=engine,
        emit_event=context.emit_event,
        on_final_transcript=orchestrator.handle_final_transcript,
        on_turn_boundary=orchestrator.handle_turn_boundary,
    )
    logger.info("transcription.start session_id=%s sample_rate_hz=%s", session_id, sample_rate_hz)
    return {"ok": True, "answer_intelligence_available": llm_provider is not None}


async def _handle_transcription_audio_chunk(params: dict, context: WorkerContext) -> dict:
    session_id = params.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise ProtocolError(ErrorCode.INVALID_PARAMS, "'session_id' is required and must be a string.")
    if context.transcription_session is None or context.transcription_session.session_id != session_id:
        raise ProtocolError(ErrorCode.NOT_RUNNING, "No active transcription session for this session id.")

    sequence = params.get("sequence")
    started_at = params.get("started_at_seconds")
    duration = params.get("duration_seconds")
    audio_b64 = params.get("audio_base64")
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0:
        raise ProtocolError(ErrorCode.INVALID_PARAMS, "'sequence' must be a non-negative integer.")
    if not isinstance(started_at, (int, float)) or isinstance(started_at, bool):
        raise ProtocolError(ErrorCode.INVALID_PARAMS, "'started_at_seconds' must be a number.")
    if not isinstance(duration, (int, float)) or isinstance(duration, bool):
        raise ProtocolError(ErrorCode.INVALID_PARAMS, "'duration_seconds' must be a number.")
    if not isinstance(audio_b64, str) or not audio_b64:
        raise ProtocolError(ErrorCode.INVALID_PARAMS, "'audio_base64' is required and must be a non-empty string.")

    try:
        pcm = base64.b64decode(audio_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ProtocolError(ErrorCode.INVALID_PARAMS, "'audio_base64' is not valid base64.") from exc

    if len(pcm) > MAX_AUDIO_CHUNK_BYTES:
        raise ProtocolError(
            ErrorCode.INVALID_PARAMS,
            f"Audio chunk exceeds the maximum size of {MAX_AUDIO_CHUNK_BYTES} bytes.",
        )

    # `handle_chunk` raises ProtocolError itself for out-of-order/duplicate
    # sequences (propagates unchanged, per `dispatch`'s ProtocolError
    # passthrough) and otherwise only buffers — it does not wait for the
    # resulting window (if any) to actually transcribe.
    await context.transcription_session.handle_chunk(sequence, float(started_at), float(duration), pcm)
    return {"ok": True}


async def _handle_transcription_stop(params: dict, context: WorkerContext) -> dict:
    session_id = params.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise ProtocolError(ErrorCode.INVALID_PARAMS, "'session_id' is required and must be a string.")
    if context.transcription_session is None or context.transcription_session.session_id != session_id:
        raise ProtocolError(ErrorCode.NOT_RUNNING, "No active transcription session for this session id.")

    await context.close_transcription_session_if_running()
    logger.info("transcription.stop session_id=%s", session_id)
    return {"ok": True}


async def _handle_answer_cancel(params: dict, context: WorkerContext) -> dict:
    session_id = params.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise ProtocolError(ErrorCode.INVALID_PARAMS, "'session_id' is required and must be a string.")
    if context.conversation_orchestrator is None or context.conversation_orchestrator.session_id != session_id:
        raise ProtocolError(ErrorCode.NOT_RUNNING, "No active conversation orchestrator for this session id.")

    await context.conversation_orchestrator.cancel_active_answer()
    logger.info("answer.cancel session_id=%s", session_id)
    return {"ok": True}


async def _handle_knowledge_ingest(params: dict, context: WorkerContext) -> dict:
    required = ("session_id", "document_id", "file_name", "file_extension", "file_path")
    values = {}
    for key in required:
        value = params.get(key)
        if not isinstance(value, str) or not value:
            raise ProtocolError(ErrorCode.INVALID_PARAMS, f"'{key}' is required and must be a non-empty string.")
        values[key] = value

    service = _get_or_create_ingestion_service(context)
    # Fire-and-forget, same pattern as `mock.start_live_feed`: the RPC
    # acknowledges "ingestion started," not "ingestion finished" — actual
    # progress/completion/failure arrive later as
    # `knowledge.ingestion_*` events. A file-content-level failure
    # (unsupported/encrypted/malformed/empty/oversized/invalid path) is
    # never an RPC error here; it's always a `knowledge.ingestion_failed`
    # event, same as any other async pipeline stage in this worker.
    asyncio.create_task(
        service.ingest(
            values["session_id"], values["document_id"], values["file_name"], values["file_extension"], values["file_path"]
        )
    )
    logger.info("knowledge.ingest session_id=%s document_id=%s", values["session_id"], values["document_id"])
    return {"ok": True}


async def _handle_knowledge_remove(params: dict, context: WorkerContext) -> dict:
    document_id = params.get("document_id")
    if not isinstance(document_id, str) or not document_id:
        raise ProtocolError(ErrorCode.INVALID_PARAMS, "'document_id' is required and must be a string.")

    service = _get_or_create_ingestion_service(context)
    await service.remove(document_id)
    logger.info("knowledge.remove document_id=%s", document_id)
    return {"ok": True}


async def _handle_knowledge_status(params: dict, context: WorkerContext) -> dict:
    document_id = params.get("document_id")
    if not isinstance(document_id, str) or not document_id:
        raise ProtocolError(ErrorCode.INVALID_PARAMS, "'document_id' is required and must be a string.")

    service = _get_or_create_ingestion_service(context)
    status = await service.status(document_id)
    return {"status": (status or IngestionStatus.NOT_INDEXED).value}


async def _handle_knowledge_retrieve(params: dict, context: WorkerContext) -> dict:
    session_id = params.get("session_id")
    query = params.get("query")
    if not isinstance(session_id, str) or not session_id:
        raise ProtocolError(ErrorCode.INVALID_PARAMS, "'session_id' is required and must be a string.")
    if not isinstance(query, str) or not query:
        raise ProtocolError(ErrorCode.INVALID_PARAMS, "'query' is required and must be a non-empty string.")

    retriever = _get_or_create_retriever(context)
    if retriever is None:
        return {"sources": []}
    retrieved = await retriever.retrieve(session_id, query)
    return {"sources": chunk_sources(retrieved)}


def _code_session_id(params: dict) -> str:
    session_id = params.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise ProtocolError(ErrorCode.INVALID_PARAMS, "'session_id' is required and must be a string.")
    return session_id


async def _handle_coding_list_files(params: dict, context: WorkerContext) -> dict:
    session_id = _code_session_id(params)
    files = _get_or_create_code_workspace_store(context).list_files(session_id)
    return {"files": [{"name": item.name, "language": item.language, "content": item.content, "version": item.version} for item in files]}


async def _handle_coding_upsert_file(params: dict, context: WorkerContext) -> dict:
    session_id = _code_session_id(params)
    name, content = params.get("name"), params.get("content")
    language = params.get("language", "text")
    base_version = params.get("base_version")
    if not isinstance(name, str) or not isinstance(content, str) or not isinstance(language, str):
        raise ProtocolError(ErrorCode.INVALID_PARAMS, "Code file name, language, and content are required strings.")
    if base_version is not None and (not isinstance(base_version, int) or isinstance(base_version, bool)):
        raise ProtocolError(ErrorCode.INVALID_PARAMS, "'base_version' must be an integer when supplied.")
    item = _get_or_create_code_workspace_store(context).upsert_file(session_id, name, language, content, base_version)
    return {"name": item.name, "language": item.language, "content": item.content, "version": item.version}


async def _handle_coding_apply_edits(params: dict, context: WorkerContext) -> dict:
    session_id = _code_session_id(params)
    name, base_version, edits = params.get("name"), params.get("base_version"), params.get("edits")
    if not isinstance(name, str) or not isinstance(base_version, int) or isinstance(base_version, bool) or not isinstance(edits, list):
        raise ProtocolError(ErrorCode.INVALID_PARAMS, "Code edits require name, base_version, and edits.")
    item = _get_or_create_code_workspace_store(context).apply_edits(session_id, name, base_version, edits)
    return {"name": item.name, "language": item.language, "content": item.content, "version": item.version}


async def _handle_coding_delete_file(params: dict, context: WorkerContext) -> dict:
    session_id = _code_session_id(params)
    name = params.get("name")
    if not isinstance(name, str) or not name:
        raise ProtocolError(ErrorCode.INVALID_PARAMS, "'name' is required and must be a string.")
    _get_or_create_code_workspace_store(context).delete_file(session_id, name)
    return {"ok": True}


async def _handle_coding_rename_file(params: dict, context: WorkerContext) -> dict:
    session_id = _code_session_id(params)
    name, new_name = params.get("name"), params.get("new_name")
    if not isinstance(name, str) or not name or not isinstance(new_name, str) or not new_name:
        raise ProtocolError(ErrorCode.INVALID_PARAMS, "'name' and 'new_name' are required strings.")
    item = _get_or_create_code_workspace_store(context).rename_file(session_id, name, new_name)
    return {"name": item.name, "language": item.language, "content": item.content, "version": item.version}


async def _handle_coding_analyze(params: dict, context: WorkerContext) -> dict:
    session_id = _code_session_id(params)
    name = params.get("name")
    if not isinstance(name, str):
        raise ProtocolError(ErrorCode.INVALID_PARAMS, "'name' is required and must be a string.")
    item = _get_or_create_code_workspace_store(context).get_file(session_id, name)
    if item is None:
        raise ProtocolError(ErrorCode.SESSION_NOT_FOUND, "The requested code file does not exist.")
    if item.language.lower() not in {"python", "py"}:
        return {"syntax_ok": True, "diagnostics": [], "complexity": 0, "function_count": 0, "unsupported_language": True}
    return analyze_python(item.content)


async def _handle_coding_run(params: dict, context: WorkerContext) -> dict:
    session_id = _code_session_id(params)
    name = params.get("name")
    if not isinstance(name, str):
        raise ProtocolError(ErrorCode.INVALID_PARAMS, "'name' is required and must be a string.")
    item = _get_or_create_code_workspace_store(context).get_file(session_id, name)
    if item is None:
        raise ProtocolError(ErrorCode.SESSION_NOT_FOUND, "The requested code file does not exist.")
    if item.language.lower() not in {"python", "py"}:
        raise ProtocolError(ErrorCode.INVALID_PARAMS, "Only Python execution is supported in this local V1.")
    return await run_python(item.content, params.get("timeout_seconds", 5.0))


async def _propose_coding_operation(params: dict, context: WorkerContext, operation: str) -> dict:
    session_id = _code_session_id(params)
    name, request = params.get("name"), params.get("request")
    if not isinstance(name, str) or not name or not isinstance(request, str) or not request:
        raise ProtocolError(ErrorCode.INVALID_PARAMS, "Coding assistance requires name and request.")
    store = _get_or_create_code_workspace_store(context)
    file = store.get_file(session_id, name)
    if file is None:
        raise ProtocolError(ErrorCode.SESSION_NOT_FOUND, "The requested code file does not exist.")
    try:
        provider = context.llm_provider_factory()
        await provider.check_availability()
    except Exception as exc:
        logger.info("Coding LLM unavailable (%s).", type(exc).__name__)
        raise ProtocolError(ErrorCode.TRANSCRIPTION_UNAVAILABLE, "Local coding intelligence is unavailable.") from exc
    proposal = await propose_code(provider, file, operation, request)
    # Recorded regardless of whether the user later applies or rejects the
    # proposal — a *rejected* proposal must not mutate `file.content`, but
    # the fact a follow-up was asked is exactly the context the next
    # follow-up needs (see `assistant.propose`'s history_block).
    store.append_history(session_id, name, operation, request, proposal.get("explanation", ""))
    return proposal


async def _handle_coding_assist(params: dict, context: WorkerContext) -> dict:
    operation = params.get("operation")
    if operation not in {"followup", "debug", "generate_tests", "explain", "diff"}:
        raise ProtocolError(ErrorCode.INVALID_PARAMS, "Unsupported coding operation.")
    return await _propose_coding_operation(params, context, operation)


async def _handle_coding_followup(params: dict, context: WorkerContext) -> dict:
    return await _propose_coding_operation(params, context, "followup")


async def _handle_coding_debug(params: dict, context: WorkerContext) -> dict:
    return await _propose_coding_operation(params, context, "debug")


async def _handle_coding_generate_tests(params: dict, context: WorkerContext) -> dict:
    return await _propose_coding_operation(params, context, "generate_tests")


async def _handle_coding_explain(params: dict, context: WorkerContext) -> dict:
    return await _propose_coding_operation(params, context, "explain")


_DESIGN_LIST_FIELDS = ("decisions", "assumptions", "requirements", "risks", "trade_offs", "action_items")


def _design_state_result(state: ArchitectureState) -> dict:
    result = {"version": state.version, "title": state.title, "nodes": [node.__dict__ for node in state.nodes], "edges": [edge.__dict__ for edge in state.edges], "mermaid": mermaid(state)}
    for name in _DESIGN_LIST_FIELDS:
        result[name] = getattr(state, name)
    return result


async def _handle_design_get(params: dict, context: WorkerContext) -> dict:
    state = _get_or_create_architecture_store(context).get(_code_session_id(params))
    return _design_state_result(state)


async def _handle_design_replace(params: dict, context: WorkerContext) -> dict:
    session_id = _code_session_id(params)
    raw_nodes, raw_edges = params.get("nodes", []), params.get("edges", [])
    base_version = params.get("base_version")
    if not isinstance(raw_nodes, list) or not isinstance(raw_edges, list) or (base_version is not None and not isinstance(base_version, int)):
        raise ProtocolError(ErrorCode.INVALID_PARAMS, "Architecture state has invalid fields.")
    list_fields = {}
    for name in _DESIGN_LIST_FIELDS:
        raw = params.get(name, [])
        if not isinstance(raw, list):
            raise ProtocolError(ErrorCode.INVALID_PARAMS, f"'{name}' must be a list of strings.")
        list_fields[name] = [item for item in raw if isinstance(item, str)]
    try:
        state = ArchitectureState(title=params.get("title", "System Design"), nodes=[ArchitectureNode(**node) for node in raw_nodes], edges=[ArchitectureEdge(**edge) for edge in raw_edges], **list_fields)
    except (TypeError, ValueError) as exc:
        raise ProtocolError(ErrorCode.INVALID_PARAMS, "Architecture nodes or edges are invalid.") from exc
    saved = _get_or_create_architecture_store(context).replace(session_id, state, base_version)
    return _design_state_result(saved)


async def _handle_design_followup(params: dict, context: WorkerContext) -> dict:
    session_id = _code_session_id(params)
    request = params.get("request")
    if not isinstance(request, str) or not request:
        raise ProtocolError(ErrorCode.INVALID_PARAMS, "'request' is required and must be a non-empty string.")
    store = _get_or_create_architecture_store(context)
    current = store.get(session_id)
    try:
        provider = context.llm_provider_factory()
        await provider.check_availability()
    except Exception as exc:
        logger.info("Design LLM unavailable (%s).", type(exc).__name__)
        raise ProtocolError(ErrorCode.TRANSCRIPTION_UNAVAILABLE, "Local design intelligence is unavailable.") from exc
    evolved = await propose_design_followup(provider, current, request)
    saved = store.replace(session_id, evolved, current.version)
    return _design_state_result(saved)


async def _handle_design_export(params: dict, context: WorkerContext) -> dict:
    session_id = _code_session_id(params)
    export_format = params.get("format")
    if export_format not in {"mermaid", "json", "markdown", "pdf"}:
        raise ProtocolError(ErrorCode.INVALID_PARAMS, "'format' must be one of mermaid, json, markdown, pdf.")
    state = _get_or_create_architecture_store(context).get(session_id)
    if export_format == "mermaid":
        return {"format": "mermaid", "content": mermaid(state)}
    if export_format == "json":
        return {"format": "json", "content": design_export.to_json(state)}
    if export_format == "markdown":
        return {"format": "markdown", "content": design_export.to_markdown(state)}
    pdf_bytes = design_export.to_pdf_bytes(state)
    return {"format": "pdf", "content_base64": base64.b64encode(pdf_bytes).decode("ascii")}


def _report_to_dict(report: SessionReport) -> dict:
    return {
        "session_id": report.session_id,
        "summary": report.summary,
        "topics": report.topics,
        "questions": report.questions,
        "generated_answers": report.generated_answers,
        "sources": report.sources,
        "decisions": report.decisions,
        "action_items": report.action_items,
        "unanswered_questions": report.unanswered_questions,
        "preparation_gaps": report.preparation_gaps,
        "memory_candidate_ids": report.memory_candidates,
    }


async def _handle_session_analyze(params: dict, context: WorkerContext) -> dict:
    session_id = _code_session_id(params)
    transcript, questions, answers = params.get("transcript", []), params.get("questions", []), params.get("answers", [])
    if not isinstance(transcript, list) or not isinstance(questions, list) or not isinstance(answers, list):
        raise ProtocolError(ErrorCode.INVALID_PARAMS, "'transcript', 'questions', and 'answers' must be lists.")

    provider = None
    try:
        candidate = context.llm_provider_factory()
        await candidate.check_availability()
        provider = candidate
    except Exception as exc:  # noqa: BLE001 - analysis must degrade, never fail, without a local LLM
        logger.info("Report LLM unavailable (%s); returning a data-only report.", type(exc).__name__)

    report = await analyze_session(provider, session_id, transcript, questions, answers)

    memory_store = _get_or_create_memory_store(context)
    candidate_ids = [memory_store.create_candidate(session_id, text).id for text in report.memory_candidates]
    report.memory_candidates = candidate_ids
    _get_or_create_report_store(context).save(report)
    return _report_to_dict(report)


async def _handle_session_report_get(params: dict, context: WorkerContext) -> dict:
    session_id = _code_session_id(params)
    report = _get_or_create_report_store(context).get(session_id)
    if report is None:
        raise ProtocolError(ErrorCode.SESSION_NOT_FOUND, "No analyzed report is stored for this session.")
    return _report_to_dict(report)


def _memory_record_to_dict(record) -> dict:
    return {"id": record.id, "session_id": record.session_id, "text": record.text, "status": record.status, "created_at": record.created_at, "updated_at": record.updated_at}


async def _handle_memory_list(params: dict, context: WorkerContext) -> dict:
    status = params.get("status")
    if status is not None and not isinstance(status, str):
        raise ProtocolError(ErrorCode.INVALID_PARAMS, "'status' must be a string when supplied.")
    records = _get_or_create_memory_store(context).list(status)
    return {"memories": [_memory_record_to_dict(r) for r in records]}


def _memory_id_param(params: dict) -> str:
    memory_id = params.get("memory_id")
    if not isinstance(memory_id, str) or not memory_id:
        raise ProtocolError(ErrorCode.INVALID_PARAMS, "'memory_id' is required and must be a string.")
    return memory_id


async def _handle_memory_approve(params: dict, context: WorkerContext) -> dict:
    record = _get_or_create_memory_store(context).approve(_memory_id_param(params))
    return _memory_record_to_dict(record)


async def _handle_memory_reject(params: dict, context: WorkerContext) -> dict:
    _get_or_create_memory_store(context).reject(_memory_id_param(params))
    return {"ok": True}


async def _handle_memory_update(params: dict, context: WorkerContext) -> dict:
    text = params.get("text")
    if not isinstance(text, str) or not text:
        raise ProtocolError(ErrorCode.INVALID_PARAMS, "'text' is required and must be a non-empty string.")
    record = _get_or_create_memory_store(context).update(_memory_id_param(params), text)
    return _memory_record_to_dict(record)


async def _handle_memory_delete(params: dict, context: WorkerContext) -> dict:
    _get_or_create_memory_store(context).delete(_memory_id_param(params))
    return {"ok": True}


_HANDLERS: dict[str, Handler] = {
    "system.ping": _handle_ping,
    "system.info": _handle_info,
    "system.llm_status": _handle_system_llm_status,
    "session.delete_data": _handle_session_delete_data,
    "worker.shutdown": _handle_shutdown,
    "session.start": _handle_session_start,
    "session.stop": _handle_session_stop,
    "mock.start_live_feed": _handle_mock_start_live_feed,
    "mock.stop_live_feed": _handle_mock_stop_live_feed,
    "transcription.start": _handle_transcription_start,
    "transcription.audio_chunk": _handle_transcription_audio_chunk,
    "transcription.stop": _handle_transcription_stop,
    "answer.cancel": _handle_answer_cancel,
    "knowledge.ingest": _handle_knowledge_ingest,
    "knowledge.remove": _handle_knowledge_remove,
    "knowledge.status": _handle_knowledge_status,
    "knowledge.retrieve": _handle_knowledge_retrieve,
    "coding.list_files": _handle_coding_list_files,
    "coding.upsert_file": _handle_coding_upsert_file,
    "coding.apply_edits": _handle_coding_apply_edits,
    "coding.delete_file": _handle_coding_delete_file,
    "coding.rename_file": _handle_coding_rename_file,
    "coding.analyze": _handle_coding_analyze,
    "coding.run": _handle_coding_run,
    "coding.assist": _handle_coding_assist,
    "coding.followup": _handle_coding_followup,
    "coding.debug": _handle_coding_debug,
    "coding.generate_tests": _handle_coding_generate_tests,
    "coding.explain": _handle_coding_explain,
    "design.get": _handle_design_get,
    "design.replace": _handle_design_replace,
    "design.followup": _handle_design_followup,
    "design.export": _handle_design_export,
    "session.analyze": _handle_session_analyze,
    "session.report.get": _handle_session_report_get,
    "memory.list": _handle_memory_list,
    "memory.approve": _handle_memory_approve,
    "memory.reject": _handle_memory_reject,
    "memory.update": _handle_memory_update,
    "memory.delete": _handle_memory_delete,
}


class Dispatcher:
    def __init__(self, handlers: Optional[dict[str, Handler]] = None) -> None:
        self._handlers = dict(handlers if handlers is not None else _HANDLERS)

    async def dispatch(self, request: Request, context: WorkerContext) -> dict:
        handler = self._handlers.get(request.method)
        if handler is None:
            raise ProtocolError(ErrorCode.METHOD_NOT_FOUND, f"Unknown method: {request.method!r}")
        try:
            return await handler(request.params, context)
        except ProtocolError:
            raise
        except Exception as exc:  # noqa: BLE001 - deliberately broad: never leak exception content to Swift or stderr
            # Deliberately metadata-only: method name and exception *type*,
            # never `logger.exception`/`str(exc)`/the traceback. Those can
            # contain arbitrary exception-message content (which may
            # embed transcript/answer/document text), and stderr is
            # documented as metadata-only — see docs/IPC_PROTOCOL.md.
            logger.error("Unhandled %s in method %s", type(exc).__name__, request.method)
            raise ProtocolError(ErrorCode.INTERNAL_ERROR, "An internal error occurred.") from exc
