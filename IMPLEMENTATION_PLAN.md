# Implementation Plan — Phase: Shell, Session Flow, Overlay UI

## Goal

A runnable native macOS app: Dashboard → Create Session → mocked Live
Session → Overlay, backed by local SQLite storage, with unit tests for the
three riskiest pieces of state (session persistence, conversation state
transitions, overlay preference persistence).

## Steps

1. `Package.swift` — macOS 13+ executable target `Veya`, dependency on
   GRDB.swift for SQLite + migrations, test target `VeyaTests`.
2. Core models (`Storage/Models`): `UserProfile`, `Session`,
   `SessionDocument`, `TranscriptSegment`, `DetectedQuestion`,
   `GeneratedAnswer` (persisted, GRDB `FetchableRecord`+
   `PersistableRecord`), plus the in-memory-only `CopilotAnswer` view model
   used by the overlay.
3. `Storage/Database` — `DatabaseManager` opens/creates the SQLite file in
   Application Support, registers versioned migrations
   (`DatabaseMigrator`).
4. `Storage/Repositories` — one repository per entity, thin CRUD wrappers
   over GRDB, async-friendly.
5. Stub protocols in `Audio/`, `Transcription/`, `Intelligence/`,
   `Knowledge/`, `Providers/` — interfaces only, each file has a single
   protocol + doc comment, zero logic, so later phases have a slot to fill.
6. `App/` — `VeyaApp` (SwiftUI `App` entry point) + `AppDelegate` (owns the
   overlay window controller + hotkey manager, since those are AppKit-level
   singletons, not SwiftUI scene state).
7. `UI/Dashboard` — `DashboardView` with navigation to Create Session,
   Previous Sessions, Knowledge Base (stub screen), Personal Profile (stub
   screen), Settings.
8. `UI/Session/CreateSessionView` + `CreateSessionViewModel` — form,
   validation, document picker (`NSOpenPanel` via `fileImporter`), saves via
   `SessionRepository` + `SessionDocumentRepository`.
9. `UI/Session/LiveSessionView` + `ConversationState` (`@MainActor`
   `ObservableObject`) + `MockTranscriptSource` + `MockAnswerGenerator` —
   drives the mocked pipeline described in ARCHITECTURE.md.
10. `Windowing/OverlayWindowController` (`NSPanel`, floating level,
    borderless, draggable, resizable, persisted frame via
    `setFrameAutosaveName`) hosting `UI/Overlay/OverlayView`.
11. `Windowing/GlobalHotkeyManager` (Carbon `RegisterEventHotKey`) — two
    hotkeys: toggle overlay visibility, toggle compact/expanded.
12. `UI/History/PreviousSessionsView` — lists persisted sessions.
13. `UI/Settings/SettingsView` — overlay opacity/always-on-top/compact
    toggles backed by `OverlayPreferencesStore`.
14. Unit tests:
    - `SessionRepositoryTests` — create/fetch/list round-trip on an
      in-memory GRDB database.
    - `ConversationStateTests` — feeding canned transcript segments drives
      the state machine through question-detected → answer-generated as
      expected.
    - `OverlayPreferencesStoreTests` — persistence round-trip using an
      isolated `UserDefaults` suite.
15. Build + test after each step; fix warnings before moving on.

## Explicit non-goals for this phase

Same list as ARCHITECTURE.md "What is explicitly NOT implemented" — not
repeated here to avoid drift between the two docs.

---

# Implementation Plan — Section 6: Swift ↔ Python Worker Bridge

## Goal

Replace the Swift timer/canned mock pipeline with a Python-driven mocked
live-session pipeline, connected over a managed-subprocess JSON Lines
protocol, while keeping the Swift demo pipeline as an explicit,
clearly-surfaced fallback when the worker is unavailable. Swift/GRDB
remains the sole session-persistence authority throughout.

## Steps

1. **Python project foundation** (`core/`): `pyproject.toml` (no runtime
   dependencies — stdlib `asyncio`/`json`/`dataclasses` only), package
   layout (`veya/`, `veya/ipc/`, `veya/mock/`, `tests/`).
2. **Wire protocol** (`veya/ipc/protocol.py`, `errors.py`, `events.py`):
   versioned JSON Lines dataclasses, `parse_incoming_line`/`serialize`,
   structured `ProtocolError`, typed event-payload builders.
3. **Dispatcher** (`veya/ipc/dispatcher.py`): `WorkerContext` (mutable
   per-process state), the seven V1 RPC method handlers, unhandled
   exceptions always become a safe `INTERNAL_ERROR` (never a leaked
   traceback).
4. **Mock pipeline** (`veya/mock/live_feed.py`): deterministic canned
   script, cancellation-safe `run_live_feed` coroutine, fixed event order.
5. **Worker** (`veya/worker.py`, `__main__.py`): `OutputWriter` (one
   `asyncio.Lock`-guarded stdout path), async stdin read loop,
   `worker.ready` emission, signal handling, `configure_logging`
   (stderr-only, metadata-only).
6. **Python tests**: `unittest`/`IsolatedAsyncioTestCase` (no pytest
   dependency needed) — protocol parsing/serialization, dispatcher
   (including error codes and idempotency), mock feed event ordering +
   cancellation, `OutputWriter` concurrent-write safety. 39 tests.
7. **Swift wire models** (`Bridge/IPCModels.swift`): `IPCJSONValue` (a
   minimal generic JSON value with `decoded(as:)`/`from(_:)`),
   request/response/event envelopes, per-method params/result DTOs,
   per-event data DTOs, `IPCClientError`. snake_case via
   `JSONEncoder/Decoder.keyEncodingStrategy`, with DTO properties
   spelling out `Id`/`Url` (not `ID`/`URL`) to keep that conversion
   correct.
8. **`IPCClient`** (`Bridge/IPCClient.swift`): an `actor` implementing
   request/response correlation (UUID keyed, per-request timeout tasks)
   and an `AsyncStream<IPCEvent>`, behind the small `IPCTransport`
   protocol so a future transport swap doesn't touch this file.
9. **`PythonWorkerConfiguration`**: env-var overrides
   (`VEYA_PYTHON_EXECUTABLE`, `VEYA_WORKER_DIRECTORY`) with a
   `/usr/bin/env python3` + source-relative `core/` dev-time default —
   never a hardcoded `/usr/bin/python3` or developer-specific path.
10. **`PythonWorkerManager`**: `Process` + 3 `Pipe`s lifecycle,
    `PythonWorkerState` machine, ready-continuation registered *before*
    `process.run()` (closes a real "lost wakeup" race — see
    `docs/IPC_PROTOCOL.md` §7), generation-counted + idempotent
    termination handling, bounded exponential-backoff restart, periodic
    health-check ping gated to active Python-driven sessions, a bounded
    metadata-only stderr diagnostic buffer.
11. **`FileHandleLineReading.swift`**: chunked (`readabilityHandler`)
    async line reading — replaced an initial `FileHandle.bytes`
    (byte-at-a-time) implementation once testing showed its per-byte
    async-suspension overhead stretching a ~3s mocked session past 10s.
12. **`IPCEventRouter`**: the single events→state translation point;
    sequential (`await`ed, not detached `Task`s) so events apply to
    `ConversationState` in arrival order.
13. **`ConversationState` refactor**: split the combined
    canned-detection `ingest(_:)` (kept, used only by the Swift fallback)
    from granular `ingestTranscriptSegment`/`ingestDetectedQuestion`/
    `ingestAnswer` (used only by `IPCEventRouter`, no auto-detection) —
    avoids running two disagreeing "mock intelligences" on the same data.
14. **`PythonIntelligenceCoordinator`**: owns `PythonWorkerManager` +
    `IPCEventRouter`, decides per-session Python-driven vs. Swift-fallback,
    exposes `drivingSource` for the UI indicator.
15. **`AppCoordinator` wiring**: injectable `pythonIntelligenceCoordinator`
    (same pattern as `presenterPrivacyManager`), launches the worker in
    the background at init, calls `beginLiveSession`/`endLiveSession`
    around the existing session start/stop flow.
16. **UI**: a minimal, non-intrusive intelligence-source indicator in
    `LiveSessionView` ("Intelligence: Python worker (mock) ✓" /
    "Intelligence: Swift fallback — Python worker unavailable ⚠").
17. **`pipeline.py`**: labeled as legacy experimentation code (header
    comment), not wired to the app, does not use the new worker.
18. **Swift tests**: `IPCModelsTests` (Codable/snake_case),
    `IPCClientTests` (correlation, timeout, malformed input, error
    responses, event yielding, transport-close/stop failing pending
    requests — via a deterministic `FakeIPCTransport`), `IPCEventRouterTests`
    (per-event-type state mutation + persistence, session-id mismatch,
    detach), `PythonIntelligenceCoordinatorTests` (fallback selection, no
    Python required), `PythonWorkerManagerIntegrationTests` (gated on a
    real `python3`/`veya` availability check — state transitions, crash
    + bounded restart, graceful shutdown, and one full real-worker
    end-to-end session). 36 new Swift tests (96 total with Sections 1-5).
19. **Docs**: `docs/IPC_PROTOCOL.md`, `docs/PYTHON_PACKAGING.md` (future
    plan, not implemented), this file, `ARCHITECTURE.md`.
20. Build + test after each step; fix warnings before moving on. Three
    genuine bugs were found and fixed via the integration test suite
    against the real subprocess (not just reasoned about): a
    continuation double-resume race across restarts, the `FileHandle
    .bytes` performance issue in (11), and the ready-continuation lost-
    wakeup race in (10) — see `docs/IPC_PROTOCOL.md` §7 and the doc
    comments at each fix site.

## Explicit non-goals for this phase

Same list as ARCHITECTURE.md "What is explicitly NOT implemented" — not
repeated here to avoid drift between the two docs. In particular: no
`session.create`/`get`/`list` RPCs, no Python-side session repository or
SQLite, no real transcription/RAG/LLM calls, no production Python runtime
bundling (see `docs/PYTHON_PACKAGING.md`).

---

# Implementation Plan — Section 7: Native Microphone Capture & Streaming Transcription

## Goal

Replace the Python-driven mocked transcript feed with real microphone
capture and streaming (rolling-window) transcription when local
dependencies (a built `whisper.cpp`-style CLI + model) are available,
while keeping both the Python mock feed and the Swift fallback pipeline
intact as explicit, lower-priority fallbacks. Swift owns native
microphone access; Python owns transcription processing — neither does
the other's job. See `docs/REALTIME_TRANSCRIPTION.md` for full detail.

## Steps

1. **Swift audio subsystem** (`Sources/Veya/Audio/`): `AudioChunk`
   (timestamped mono 16kHz `pcm_s16le`), `MicrophoneAuthorizationState` +
   `MicrophonePermissionChecking` (+ `AVFoundationMicrophonePermission`),
   `AudioCaptureError`, the `AudioCapturing` protocol (replacing the old
   stub), and `MicrophoneAudioCapture` — a real `AVAudioEngine`-backed
   implementation: hardware-format tap → `AVAudioConverter` → mono 16kHz
   16-bit PCM → bounded (`bufferingNewest`) `AsyncStream`, with dropped
   chunks counted (metadata only), never accumulated unbounded.
2. **IPC protocol extension** (`Bridge/IPCModels.swift`):
   `TranscriptionStartParams`, `AudioChunkParams` — three new RPC methods
   on the existing Section 6 JSON Lines protocol
   (`transcription.start`/`audio_chunk`/`stop`), no transport changes.
3. **`AudioChunkSender`** (`Bridge/AudioChunkSender.swift`): an actor that
   sends audio chunks with a bounded number of concurrent in-flight RPCs
   (`maxInFlight`, default 2) rather than one-at-a-time synchronous waits
   — excess/oversized chunks are dropped and counted, capture is never
   blocked on Python. The RPC call itself is injected as a closure (same
   reasoning as `IPCClient`/`IPCTransport`) so it's testable with a
   controllable fake, not just a real subprocess.
4. **Python transcription package** (`core/veya/transcription/`):
   - `rolling_buffer.py` — bounded rolling-window buffer with overlap
     (works around `whisper.cpp`'s lack of a true streaming-ingest mode).
   - `overlap.py` — word-level `dedupe_overlap` between consecutive
     overlapping windows' transcripts.
   - `engine.py` — the `TranscriptionEngine` protocol,
     `WhisperCliTranscriptionEngine` (writes each window to a temp WAV,
     invokes the CLI, deletes the temp file immediately after — never
     persists raw audio), `WhisperConfig.resolve_from_env()`
     (`VEYA_WHISPER_BIN`/`VEYA_WHISPER_MODEL`, no hardcoded paths),
     `TranscriptionSetupError` (typed, never crashes the worker).
   - `session.py` — `TranscriptionSession`: buffers/validates chunks
     (strict sequence ordering) on the worker's main dispatch path, but
     runs the actual (multi-second) Whisper invocation in a background
     `asyncio.Task` consumer — the dispatch loop is otherwise strictly
     sequential (see `worker.py`), so blocking it on a real transcription
     call would stall every other in-flight RPC, not just transcription.
5. **Dispatcher wiring** (`core/veya/ipc/dispatcher.py`):
   `transcription.start`/`audio_chunk`/`stop` handlers,
   `ErrorCode.TRANSCRIPTION_UNAVAILABLE`, `MAX_AUDIO_CHUNK_BYTES`
   server-side enforcement (mirroring Swift's client-side cap),
   `WorkerContext.transcription_session`/`transcription_engine_factory`,
   and defensive cleanup from `session.stop`/`worker.shutdown`.
6. **`PythonIntelligenceCoordinator` three-way selection**
   (`Bridge/PythonIntelligenceCoordinator.swift`): extends
   `ConversationDrivingSource` with `.realTranscription`; selection order
   is real transcription → Python mock feed → Swift fallback, decided
   fresh per session. Reuses Section 6's mid-session-crash fallback
   mechanism (`PythonWorkerManager.stateChangeHandler`) for real
   transcription too, and adds `liveSessionIndicatorText` — the single
   place that maps internal state to the five user-facing indicator
   strings the build prompt specifies.
7. **`AppDelegate` wiring**: real capture (`MicrophoneAudioCapture()`) is
   opted into explicitly at the one real app-launch call site, not as
   `PythonIntelligenceCoordinator`'s own default (`audioCapture: nil`) —
   every other call site (tests, previews) never touches AVFoundation or
   prompts for permission just by constructing a coordinator.
8. **Tests**: 21 new Swift tests (fakes only — `FakeAudioCapture`,
   `FakeMicrophonePermission`, a closure-injected `AudioChunkSender`) plus
   one manual/opt-in real end-to-end suite
   (`RealTranscriptionIntegrationTests`, gated on `VEYA_WHISPER_BIN`/
   `VEYA_WHISPER_MODEL` being set to a real local build — skipped
   otherwise). 47 new Python tests (rolling buffer, overlap dedup, fake
   engine, dispatcher validation) plus one manual/opt-in real-Whisper
   smoke test (`core/tests/test_whisper_smoke.py`), skipped by default.
9. **Docs**: `docs/REALTIME_TRANSCRIPTION.md` (architecture, wire
   contract, Whisper setup, fallback behavior, privacy, measured latency,
   known limitations, manual verification checklist, troubleshooting),
   `ARCHITECTURE.md` and this file updated.
10. Build + test after each step. The real end-to-end smoke test (Swift
    fake-audio-capture → real Python worker → real `whisper.cpp` CLI →
    real speech audio) was actually run against a local `whisper.cpp`
    checkout available in this dev environment and passed, producing
    real transcript text from real speech — see
    `docs/REALTIME_TRANSCRIPTION.md`'s "What was and wasn't actually
    verified" section for exactly what that does and does not prove
    (notably: no real microphone hardware was involved).

## Explicit non-goals for this phase

Same list as ARCHITECTURE.md "What is explicitly NOT implemented" — not
repeated here to avoid drift. In particular: no system audio capture, no
RAG/embeddings, no Coding/System Design Copilot, no production Python
runtime bundling.

---

# Implementation Plan — Section 8: Question Detection & Local LLM Answer Generation

## Goal

Add Python-side question detection and local Ollama-backed answer
generation to the real transcription pipeline from Section 7, without
ever making real transcription depend on Ollama being available. Swift
remains the host/presentation/persistence owner; Python owns question
intelligence and answer generation. See
`docs/QUESTION_AND_ANSWER_INTELLIGENCE.md` for full detail.

## Steps

1. **Python LLM provider abstraction** (`core/veya/llm/`): `provider.py`'s
   `LLMProvider` protocol (`check_availability`/`generate_stream`),
   `errors.py`'s typed `LLMUnavailableError`/`LLMTimeoutError`/
   `LLMProviderError`, and `ollama_provider.py`'s `OllamaProvider` — talks
   to a local Ollama instance using only `urllib` (no `requests`
   dependency), with `VEYA_OLLAMA_URL`/`VEYA_OLLAMA_MODEL` env
   configuration and sensible-but-unverified defaults.
2. **Python question detection** (`core/veya/conversation/question_detector.py`):
   a deterministic punctuation/interrogative-form scorer (not an LLM
   call), configurable confidence threshold, near-duplicate suppression
   for questions split across overlapping Whisper windows.
3. **Prompt construction** (`core/veya/conversation/context_builder.py`):
   assembles a minimal prompt from `SessionContext` (the Swift `Session`
   fields sent once via `session.start`) + the detected question, asking
   the model to respond in a fixed `ANSWER:`/`POINTS:`/`CAVEAT:` format.
4. **Answer generation** (`core/veya/conversation/answer_generation.py`):
   `parse_answer_text` (pure, testable, with a sentence-splitting fallback
   for when a model doesn't follow the requested format) and
   `generate_answer` (streams deltas, accumulates, parses).
5. **Orchestration** (`core/veya/conversation/orchestrator.py`):
   `ConversationOrchestrator` ties detection + generation to every real
   `transcript.final` (via a new `TranscriptionSession.on_final_transcript`
   hook — Section 7's file, extended, not replaced), with per-session
   incrementing answer sequence numbers, one-active-generation-at-a-time
   supersession, `answer.cancel` support, and a degraded
   `answer.completed` on mid-stream provider failure so the UI never
   hangs on "Generating answer…" indefinitely.
6. **Dispatcher wiring** (`core/veya/ipc/dispatcher.py`): `session.start`
   now captures the full `SessionContext` (every new field
   optional/blank-safe — old callers unaffected); `transcription.start`
   additionally attempts an Ollama `check_availability()` (failure is
   caught, logged type-only, and only disables answer intelligence — it
   never turns transcription.start itself into a failure) and returns
   `answer_intelligence_available` in its response; new `answer.cancel`
   RPC.
7. **Wire protocol extensions** (`core/veya/ipc/events.py` /
   `Bridge/IPCModels.swift`): `question.detected` gained `confidence`/
   `detected_at`; `answer.started`/`answer.delta`/`answer.completed`
   gained `sequence`; `answer.completed` gained `caveat`. All backward
   compatible via defaults — Section 6's mock feed needed only a trivial
   sequence-counter addition, no behavior change.
8. **Swift wiring** (`Bridge/PythonIntelligenceCoordinator.swift`):
   `session.start`'s params extended with the full session context;
   `answerIntelligenceAvailable` published from `transcription.start`'s
   result; `answer.cancel` sent (best-effort) on `endLiveSession`;
   `liveSessionIndicatorText` extended with "Listening — answer
   intelligence unavailable"/"Analyzing question…"/"Generating answer…" —
   the latter two read directly from `ConversationState`.
9. **`IPCEventRouter` sequence guard**: `answer.started` only moves the
   tracked sequence forward; `answer.delta`/`answer.completed` are only
   applied when their sequence exactly matches — a stale/superseded
   round's late events are dropped safely rather than corrupting a newer
   round's state. `answer.completed`'s optional `caveat` is folded into
   `CopilotAnswer.talkingPoints` as a final entry (no schema/migration
   change needed).
10. **`ConversationState`**: new `isAnalyzingQuestion` (between
    `question.detected` and `answer.started`) alongside the existing
    `isGeneratingAnswer`; new `cancelPendingAnswerActivity()` clears both
    plus partial-answer text without touching persisted data — called on
    session end and on the existing Section 6 mid-session-crash fallback
    path.
11. **Tests**: 53 new Python tests (question detector, context builder,
    answer-text parsing, streaming/cancellation with a fake provider, a
    fully-faked Ollama HTTP layer via `urllib.request.urlopen` mocking,
    orchestrator sequencing/supersession/cancellation, dispatcher wiring)
    plus one new opt-in real-Ollama smoke test
    (`core/tests/test_ollama_smoke.py`, skipped by default). 4 new Swift
    unit/integration tests plus one new gated real-integration suite
    (`AnswerIntelligenceAvailabilityIntegrationTests`, opt-in on both real
    Whisper and real Ollama being configured).
12. **Docs**: `docs/QUESTION_AND_ANSWER_INTELLIGENCE.md`, `ARCHITECTURE.md`
    and this file updated.
13. Build + test after each step. A real local Ollama instance
    (`qwen3:1.7b`) happened to already be installed and running in this
    dev environment, so — unlike a typical from-scratch verification —
    the full question-detection → prompt → streaming-generation →
    parsing pipeline, and the Swift-side
    `answerIntelligenceAvailable` wiring, were both actually exercised
    against a real model, not just faked. See
    `docs/QUESTION_AND_ANSWER_INTELLIGENCE.md`'s "What was and wasn't
    actually verified" section for exactly what that does and does not
    prove (notably: still no real microphone hardware, no real spoken
    question, no answer-quality evaluation).

## Explicit non-goals for this phase

Same list as ARCHITECTURE.md "What is explicitly NOT implemented" — not
repeated here to avoid drift. In particular: no cloud LLM providers, no
Coding/System Design Copilot, no Python-owned session SQLite, no
Presenter Privacy changes, no production Python runtime bundling. (Local
document retrieval/grounded `sources` were still `[]`-only as of Section
8 — see Section 9 below for where that changed.)

---

# Implementation Plan — Section 9: Local Document Ingestion, Retrieval & Grounded Answers

## Goal

Add local parsing/chunking/embedding/retrieval of a session's attached
documents so Section 8's answer generation can ground its answers in
real, verifiable source references — without ever making real
transcription or chat answer generation depend on retrieval being
available. Swift remains the owner of file selection, session lifecycle,
and all persistence; Python owns parsing/chunking/embeddings/retrieval
and a small local derived-data index. See `docs/KNOWLEDGE_RETRIEVAL.md`
for full detail.

## Steps

1. **Python document extraction** (`core/veya/knowledge/extraction.py`):
   `validate_document_path` (the one filesystem read boundary — requires
   the path to resolve strictly beneath `VEYA_DOCUMENTS_DIRECTORY`) plus
   per-format extractors: stdlib text read for `.txt`/`.md`, `pypdf` for
   `.pdf` (embedded text only, no OCR; encryption detected and rejected),
   stdlib `zipfile`/`xml.etree.ElementTree` for `.docx` (no external
   DOCX library). Every failure mode is a typed `KnowledgeError` subclass
   (`errors.py`) — unsupported/encrypted/malformed/empty/oversized —
   never a bare exception.
2. **Deterministic chunking** (`chunking.py`): character-based (not
   token-based — no tokenizer dependency), configurable target
   size/overlap, stable chunk IDs (`sha256(document_id:chunk_index)`) so
   re-ingesting a document is a clean replace, not an accumulation.
3. **Local embeddings** (`embeddings.py`): `EmbeddingProvider` protocol,
   `OllamaEmbeddingProvider` (stdlib-only `urllib` against Ollama's local
   `/api/embed`, same loopback-only trust boundary as Section 8's chat
   provider, reusing that same local runtime rather than adding a second
   ML dependency), and a deterministic `FakeEmbeddingProvider` (hash-based
   bag-of-words vectors) used throughout the test suite.
4. **Local vector store** (`vector_store.py`): SQLite, holding only
   derived data — document ingestion status and chunk
   text/metadata/embeddings, `ON DELETE CASCADE` so removing a document
   removes its chunks. Pure-Python cosine similarity (no numpy). Search
   is always session-scoped and only considers `ready` documents.
5. **Retrieval** (`retrieval.py`): embeds the query, searches the current
   session only, applies a similarity threshold, and assembles a bounded,
   clearly-delimited prompt context block — retrieval failing (no
   provider, embedding error) returns `[]`, never raises, never blocks
   answer generation.
6. **Ingestion orchestration** (`ingestion.py`): `IngestionService` ties
   extraction → chunking → embedding → storage together, emitting
   `knowledge.ingestion_started`/`_progress`/`_completed`/`_failed`
   throughout — a document is only ever marked `ready` once every step
   actually succeeded.
7. **Dispatcher wiring** (`core/veya/ipc/dispatcher.py`): four new RPCs
   (`knowledge.ingest`/`remove`/`status`/`retrieve`), a `WorkerContext`
   extended with `documents_directory`/`knowledge_index_directory`/
   `embedding_provider_factory` and lazily-constructed
   `vector_store`/`ingestion_service`/`retriever` (never touches disk or
   the embedding provider just from constructing a `WorkerContext` — kept
   every existing Section 6-8 test hermetic after an initial regression
   was caught and fixed: `transcription.start` tests were silently
   writing a real SQLite file into the real Application Support directory
   until their `make_context()` helper was given an explicit fake
   embedding-provider factory + temp knowledge-index directory).
8. **Grounded answer generation** (`core/veya/conversation/`):
   `context_builder.render_prompt` gained an optional
   `document_context_block` parameter + grounding instructions;
   `ConversationOrchestrator` retrieves before generating (when a
   retriever was configured), builds `answer.completed`'s `sources` from
   the actual retrieved chunks (never from what the model said), and
   passes `[]` whenever nothing was retrieved.
9. **Wire protocol extension** (`core/veya/ipc/events.py` /
   `Bridge/IPCModels.swift`): `answer_completed`'s `sources` changed from
   `list[str]`/`[String]` to a list of structured
   `{document_id, file_name, chunk_id, excerpt}` references
   (`AnswerSourceEventData` in Swift) — the mock feed's canned source was
   updated to the same structured shape for wire consistency.
10. **Swift wiring**: `PythonWorkerConfiguration` gained
    `documentsDirectoryURL`/`knowledgeIndexDirectoryURL` (mirroring
    `CreateSessionViewModel`'s own Application Support paths),
    `PythonWorkerManager` passes them to the subprocess as
    `VEYA_DOCUMENTS_DIRECTORY`/`VEYA_KNOWLEDGE_INDEX_DIRECTORY`.
    `KnowledgeIngestionTracker` (new, app-lifetime `ObservableObject`)
    tracks per-document status from `IPCEventRouter`'s new
    `knowledge.ingestion_*` routing — independent of any single Live
    Session's attach/detach. `PythonIntelligenceCoordinator.ingestDocuments`
    sends `knowledge.ingest` per document (fire-and-forget; a failure only
    marks that document's tracked status, never breaks session
    creation/start or deletes the copied file).
    `CreateSessionViewModel`/`CreateSessionView` wired to call it right
    after a successful save. `IPCEventRouter` folds each structured
    source into a compact `"filename: excerpt"` string for
    `CopilotAnswer.sources: [String]` — no schema/migration change, and
    `OverlayView`'s existing compact source line needed no changes at all
    to pick it up. `LiveSessionView` gained a "DOCUMENTS" section showing
    the build prompt's exact five status strings.
11. **Tests**: 91 new Python tests (extraction with real hand-crafted
    PDF/DOCX/encrypted-PDF fixtures, chunking, embeddings incl. a real
    Ollama round trip, vector store, retrieval, ingestion, dispatcher
    wiring, grounded-orchestrator wiring) bringing the Python total to
    242 (2 opt-in real-integration tests, skipped by default). 21 new
    Swift tests (IPC models, ingestion tracker, event routing, coordinator
    `ingestDocuments`, plus a new real-worker-gated test proving the
    `knowledge.ingest` RPC → event → tracker pipeline end-to-end) bringing
    the Swift total to 141.
12. **Docs**: `docs/KNOWLEDGE_RETRIEVAL.md` (new), `ARCHITECTURE.md`
    (including a correction to a stale Section 6-era closing paragraph
    that had drifted since Section 8) and this file updated.
13. Build + test after each step. `pypdf` was installed for real
    (`pip3 install --break-system-packages pypdf`, with the user's
    explicit go-ahead given the Homebrew-managed-Python constraint) and a
    real `nomic-embed-text` model was pulled via `ollama pull` — real PDF
    extraction (including a real encrypted PDF generated with
    `pypdf.PdfWriter.encrypt`), real DOCX extraction, real local
    embeddings, and the **full grounded-answer pipeline end-to-end**
    (real chunking → real embeddings → real retrieval → real Ollama chat
    → a real, correct source reference) were all actually exercised, not
    simulated. See `docs/KNOWLEDGE_RETRIEVAL.md`'s "What was and wasn't
    actually verified" for exactly what that does and does not prove
    (notably: still no real GUI interaction, no real microphone hardware).

## Explicit non-goals for this phase

Same list as ARCHITECTURE.md "What is explicitly NOT implemented" — not
repeated here to avoid drift. In particular: no OCR, no cloud
embeddings/retrieval, no external/hosted vector database, no Python-owned
session database, no Coding/System Design Copilot, no Presenter Privacy
changes, no production Python runtime bundling.
