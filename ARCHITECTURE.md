# Veya — Architecture

Veya is a native macOS real-time conversation copilot targeting Apple
Silicon (MacBook Air M2+). It is built with Swift + SwiftUI (AppKit where
low-level window management is required), using async/await and structured
concurrency.

## Build tooling

The dev machine has Xcode Command Line Tools but not the full Xcode.app, so
this phase is built as a **Swift Package Manager** executable app target
rather than an `.xcodeproj`. The code is ordinary SwiftUI/AppKit — a real
`.xcodeproj` wrapper can be generated later (`swift package generate-
xcodeproj` or a hand-authored project) once Xcode.app is installed, without
changing any source.

```
swift build   # compile
swift run     # launch the app
swift test    # run unit tests
```

## Target folder structure

```text
Veya/
├── Sources/Veya/
│   ├── App/                 # App entry point, AppDelegate, root scene
│   ├── UI/
│   │   ├── Dashboard/       # IMPLEMENTED — dashboard, entry points
│   │   ├── Session/         # IMPLEMENTED — create session, live session
│   │   ├── Overlay/         # IMPLEMENTED — overlay panel content
│   │   ├── Settings/        # IMPLEMENTED — settings + Presenter Privacy
│   │   ├── History/         # IMPLEMENTED — previous sessions list
│   │   └── SafeShare/       # IMPLEMENTED — Safe Share controls/view
│   ├── Windowing/
│   │   ├── OverlayWindowController.swift        # IMPLEMENTED
│   │   ├── GlobalHotkeyManager.swift            # IMPLEMENTED
│   │   ├── PresenterPrivacyModels.swift         # IMPLEMENTED
│   │   ├── PresenterPrivacyPreferencesStore.swift # IMPLEMENTED
│   │   ├── PresenterPrivacyManager.swift        # IMPLEMENTED
│   │   ├── CaptureCompatibilityTester.swift     # IMPLEMENTED
│   │   ├── CaptureResultAggregator.swift        # IMPLEMENTED (pure, tested)
│   │   ├── DisplayManager.swift                 # IMPLEMENTED
│   │   ├── SafeShareManager.swift                # IMPLEMENTED
│   │   ├── SafeShareCaptureEngine.swift          # IMPLEMENTED
│   │   ├── SafeShareWindowController.swift       # IMPLEMENTED
│   │   ├── MemoryDiagnostics.swift               # IMPLEMENTED (DEBUG diagnostics)
│   │   └── PrivacyLog.swift                      # IMPLEMENTED (structured logging)
│   ├── Audio/                                  # IMPLEMENTED — Section 7
│   │   ├── AudioCapturing.swift               # protocol: chunks()/start()/stop()
│   │   ├── MicrophonePermission.swift          # AVCaptureDevice authorization wrapper
│   │   ├── MicrophoneAudioCapture.swift        # AVAudioEngine capture + PCM conversion
│   │   ├── AudioChunk.swift                    # timestamped mono 16kHz s16le chunk
│   │   └── AudioCaptureError.swift             # typed capture errors
│   ├── Transcription/        # STUB ONLY — reserved for a possible future
│   │                         # on-device Swift transcriber. Real
│   │                         # transcription (Section 7) instead runs in
│   │                         # the Python worker — see Bridge/ below.
│   ├── Intelligence/         # STUB ONLY — protocol, no LLM/answer-gen.
│   │                         # Real answer generation (Section 8) runs in
│   │                         # the Python worker — see Bridge/ below.
│   ├── Knowledge/            # STUB ONLY — protocol placeholder. Real
│   │                         # ingestion/retrieval (Section 9) runs in
│   │                         # the Python worker — see Bridge/ + core/ below.
│   ├── Bridge/                                # IMPLEMENTED — Sections 6-9
│   │   ├── IPCModels.swift                    # wire DTOs, JSON value, snake_case coding
│   │   ├── IPCClient.swift                    # actor: correlation, timeouts, event stream
│   │   ├── PythonWorkerConfiguration.swift     # discovery/env/dev defaults, documents/knowledge-index paths
│   │   ├── PythonWorkerManager.swift           # Process lifecycle, state machine, restart, ping
│   │   ├── IPCEventRouter.swift                # events → ConversationState/repositories, per-answer sequence tracking, knowledge ingestion routing
│   │   ├── PythonIntelligenceCoordinator.swift # real-transcription vs. Python-mock vs. Swift-fallback selection, answer-intelligence state, document ingestion requests
│   │   ├── KnowledgeIngestionTracker.swift     # Section 9 — per-document ingestion status, app-lifetime
│   │   ├── DocumentIngestionStatus.swift       # Section 9 — mirrors core/veya/knowledge/models.py's IngestionStatus
│   │   ├── AudioChunkSender.swift              # Section 7 — bounded in-flight audio_chunk sends
│   │   ├── FileHandleLineReading.swift         # chunked async line reading (perf)
│   │   └── BridgeLog.swift                     # structured, metadata-only logging
│   ├── Storage/
│   │   ├── Database/         # IMPLEMENTED — GRDB pool + migrations
│   │   ├── Models/           # IMPLEMENTED — Codable/FetchableRecord models
│   │   └── Repositories/     # IMPLEMENTED — CRUD repositories, incl.
│   │                         # CaptureCompatibilityRepository (history)
│   └── Providers/            # STUB ONLY — protocol placeholders
├── Tests/VeyaTests/          # IMPLEMENTED — unit tests for this phase + Sections 5-9
├── core/                                       # IMPLEMENTED — Sections 6-9 Python worker
│   ├── pyproject.toml
│   ├── veya/
│   │   ├── __main__.py        # `python -m veya` entry point
│   │   ├── worker.py          # stdin loop, OutputWriter, lifecycle
│   │   ├── ipc/                # protocol.py, dispatcher.py, events.py, errors.py
│   │   ├── mock/live_feed.py   # deterministic mocked event sequence
│   │   ├── transcription/      # Section 7 — real transcription
│   │   │   ├── engine.py       # TranscriptionEngine protocol + local Whisper CLI engine
│   │   │   ├── rolling_buffer.py  # bounded rolling-window buffer with overlap
│   │   │   ├── overlap.py      # word-level overlap deduplication
│   │   │   └── session.py      # per-session orchestration, off-dispatch-path transcription
│   │   ├── conversation/       # Sections 8-9 — question detection + grounded answer orchestration
│   │   │   ├── models.py       # SessionContext, DetectedQuestionResult, ParsedAnswer
│   │   │   ├── question_detector.py  # deterministic punctuation/interrogative heuristic
│   │   │   ├── context_builder.py    # session-context + question + retrieved chunks → LLM prompt
│   │   │   ├── answer_generation.py  # streams + parses one answer
│   │   │   └── orchestrator.py       # ties detection/retrieval/generation to transcript.final, sequencing, cancellation
│   │   ├── llm/                # Section 8 — local LLM provider abstraction
│   │   │   ├── provider.py     # LLMProvider protocol
│   │   │   ├── ollama_provider.py  # stdlib-only (urllib) local Ollama chat client
│   │   │   └── errors.py       # LLMUnavailableError/LLMTimeoutError/LLMProviderError
│   │   └── knowledge/          # Section 9 — document ingestion + retrieval
│   │       ├── models.py       # DocumentChunk, IngestionStatus, Chunking/RetrievalConfig
│   │       ├── errors.py       # typed document/embedding errors
│   │       ├── extraction.py   # path validation + txt/md/pdf/docx text extraction
│   │       ├── chunking.py     # deterministic overlapping chunking, stable chunk IDs
│   │       ├── embeddings.py   # EmbeddingProvider protocol, Ollama + deterministic fake
│   │       ├── vector_store.py # SQLite chunk/embedding/status store, cosine similarity search
│   │       ├── retrieval.py    # session-scoped top-k retrieval, bounded prompt context
│   │       └── ingestion.py    # extract → chunk → embed → store orchestration + events
│   └── tests/                  # 242 unittest tests (2 opt-in real-integration, skipped by default)
├── docs/
│   ├── PRESENTER_PRIVACY.md          # IMPLEMENTED
│   ├── PRESENTER_PRIVACY_TESTING.md  # IMPLEMENTED
│   ├── IPC_PROTOCOL.md               # IMPLEMENTED — Section 6
│   ├── REALTIME_TRANSCRIPTION.md     # IMPLEMENTED — Section 7
│   ├── QUESTION_AND_ANSWER_INTELLIGENCE.md  # IMPLEMENTED — Section 8
│   ├── KNOWLEDGE_RETRIEVAL.md        # IMPLEMENTED — Section 9
│   └── PYTHON_PACKAGING.md           # IMPLEMENTED — Section 6 (future-work plan, not built)
├── Package.swift
├── ARCHITECTURE.md
└── IMPLEMENTATION_PLAN.md
```

`Coding/` is intentionally not created this phase (per spec).

## What is implemented in this phase

- App shell that launches to an empty-state Dashboard.
- Dashboard: New Session, Previous Sessions (list, can be empty), Knowledge
  Base entry point (UI only), Personal Profile entry point (UI only),
  Settings.
- Create Session form (title, company, role/topic, description, expected
  participants, session type, notes, preferred answer style, preferred
  language, custom instructions) with document attachment UI that stores
  file metadata + a copy of the file. As of Section 9, that copy is also
  sent (by path only, never contents) to the Python worker for local
  parsing/chunking/embedding — see below.
- Live Session flow: a `ConversationState` actor-backed observable object
  driven by a `MockTranscriptSource` (protocol-based) that plays a canned
  script on a timer, producing `TranscriptSegment`s, then
  `DetectedQuestion`s, then canned `CopilotAnswer`s. No real audio, no real
  question detection, no LLM calls anywhere in this pipeline.
- Overlay: a floating `NSPanel`-backed window (`OverlayWindowController`)
  hosting a SwiftUI view that renders the live `CopilotAnswer`, with
  compact/expanded modes, configurable opacity, draggable + resizable with
  persisted frame, and global hotkeys via `GlobalHotkeyManager` (show/hide,
  compact/expand toggle).
- Local storage via GRDB (SQLite) with versioned migrations for:
  `UserProfile`, `Session`, `SessionDocument`, `TranscriptSegment`,
  `DetectedQuestion`, `GeneratedAnswer`, `CaptureCompatibilityRecord`.
- **Presenter Privacy** (Phase 2 §5 — see `docs/PRESENTER_PRIVACY.md` for
  full detail):
  - Presenter Privacy state model (`PresenterPrivacyStatus`,
    `PresenterPrivacyMode`, `PresenterPrivacyPreferences`).
  - `DisplayManager` — display enumeration, topology-change tracking,
    preferred-display resolution (single- and multi-display).
  - Direct Private Overlay: best-effort `NSWindowSharingType` config,
    always paired with real, local, non-faked measurement — never
    reported "verified" from configuration alone.
  - `CaptureCompatibilityTester` — `ScreenCaptureKit`-based diagnostic
    using a deterministic checkerboard marker (no OCR), multi-frame
    (5-frame) verification with tested aggregation rules
    (`CaptureResultAggregator`).
  - **Veya Safe Share** — `SafeShareCaptureEngine` (actor, owns the
    `SCStream`, excludes Veya's own application via `SCContentFilter`),
    `SafeShareManager` (testable coordinator), `SafeShareWindowController`
    (the "Veya Safe Share" window, rendered via
    `AVSampleBufferDisplayLayer`).
  - Presenter Privacy Settings UI, Safe Share controls UI, a minimal
    non-intrusive privacy indicator in the overlay, and Live Session
    integration (confirmation prompts before starting a session when
    privacy is enabled but not yet ready — never a hard block).
  - Local-only compatibility history (`CaptureCompatibilityRepository`,
    capped at the most recent 20 results, no telemetry upload) and a
    DEBUG-only diagnostics panel.
- **Swift ↔ Python worker bridge** (Section 6 — see `docs/IPC_PROTOCOL.md`
  for full detail):
  - A long-running Python worker (`core/veya`) speaking a versioned JSON
    Lines protocol over a managed subprocess's stdin/stdout (stderr is
    logs only, never IPC).
  - `IPCClient` (Swift actor): request/response RPC with UUID
    correlation and timeouts, plus an asynchronous event stream.
    Transport-agnostic behind the small `IPCTransport` protocol.
  - `PythonWorkerManager`: process launch, an observable
    `PythonWorkerState` state machine, periodic health-check pings
    (only while a Python-driven session is active), and bounded
    crash-restart with exponential backoff (never restarts forever).
  - `IPCEventRouter`: the single place worker events become
    `ConversationState`/repository mutations — no event-handling logic
    lives in SwiftUI views.
  - `PythonIntelligenceCoordinator`: decides per-session whether to
    drive `ConversationState` from the Python worker's mocked event feed
    or fall back to the existing Swift `MockTranscriptSource` timer —
    only one pipeline ever runs at a time, and the UI never claims
    Python-backed intelligence is active when the Swift fallback is
    actually running (see `LiveSessionView`'s indicator).
  - Python side (`core/veya`): `asyncio`-based worker, dispatcher for the
    seven V1 RPC methods, structured errors, and a deterministic mocked
    live-session event pipeline (`mock/live_feed.py`) — canned data,
    controllable timing, no real intelligence.
  - Swift/GRDB remains the sole session-persistence authority — Python
    has no session database, no `session.create`/`get`/`list`, and no
    SQLite of its own.
- **Real microphone capture & streaming transcription** (Section 7 — see
  `docs/REALTIME_TRANSCRIPTION.md` for full detail):
  - `MicrophoneAudioCapture` (`Audio/`): real `AVAudioEngine` capture,
    converted to mono 16kHz `pcm_s16le`, chunked, and delivered through a
    bounded `AsyncStream` (drops + counts rather than growing unbounded).
  - `AudioChunkSender` (`Bridge/`): bounded-concurrency audio delivery to
    the worker — never blocks capture waiting on Python.
  - Three new IPC methods (`transcription.start`/`audio_chunk`/`stop`) on
    the same Section 6 JSON Lines protocol.
  - `core/veya/transcription/`: a bounded rolling-window buffer feeding a
    local `whisper.cpp`-style CLI (no cloud APIs, no Ollama), behind a
    `TranscriptionEngine` abstraction so it's fakeable in tests.
  - `PythonIntelligenceCoordinator` now picks one of **three** sources per
    session (real transcription > Python mock feed > Swift fallback), and
    the same mid-session-crash fallback from Section 6 covers real
    transcription too.
  - Real transcription only ever populates transcript text — it does not
    trigger the Swift fallback's canned question detection/answer
    generation.
- **Question detection & local LLM answer generation** (Section 8 — see
  `docs/QUESTION_AND_ANSWER_INTELLIGENCE.md` for full detail):
  - `core/veya/conversation/question_detector.py`: a deterministic
    punctuation/interrogative-form heuristic (not an LLM call) run only on
    real `transcript.final` text, with near-duplicate suppression across
    overlapping Whisper windows and a configurable confidence threshold.
  - `core/veya/llm/`: an `LLMProvider` abstraction with a local,
    stdlib-only (`urllib`, no `requests`) `OllamaProvider` implementation
    — the only provider in this section, never a cloud API.
  - `core/veya/conversation/orchestrator.py`: ties detection + generation
    to every real final transcript, with per-session answer sequence
    numbers, one-active-generation-at-a-time supersession, and
    cancellation (`answer.cancel`).
  - `PythonIntelligenceCoordinator` now also tracks
    `answerIntelligenceAvailable` (from `transcription.start`'s response)
    — Ollama being unavailable never falls real transcription back to the
    mock feed, it only disables question detection/answer generation for
    that session. `IPCEventRouter` drops stale/superseded answer events
    using the new per-answer sequence numbers.
  - `ConversationState` gained `isAnalyzingQuestion` (between
    `question.detected` and `answer.started`) alongside the existing
    `isGeneratingAnswer`, feeding two new Live Session indicator states.
- **Local document ingestion, retrieval & grounded answers** (Section 9 —
  see `docs/KNOWLEDGE_RETRIEVAL.md` for full detail):
  - `core/veya/knowledge/`: local `.txt`/`.md`/`.pdf`/`.docx` text
    extraction (`pypdf` for PDF, stdlib `zipfile`/`ElementTree` for
    DOCX — no OCR, no macro/script execution), deterministic overlapping
    chunking with stable chunk IDs, a local `EmbeddingProvider`
    abstraction (real: Ollama's `/api/embed`; fake: deterministic
    hash-based vectors for tests), and a SQLite `VectorStore` holding only
    *derived* chunk/embedding/status data — never session, transcript,
    question, or answer data (Swift/GRDB remains sole owner of those).
  - Four new IPC methods (`knowledge.ingest`/`remove`/`status`/`retrieve`)
    on the same Sections 6-8 JSON Lines protocol, plus four
    `knowledge.ingestion_*` events. Python validates every ingested path
    resolves strictly beneath `VEYA_DOCUMENTS_DIRECTORY` (the same
    Application Support folder Swift already copies attached documents
    into) before ever reading it — whole document contents are never
    sent over IPC.
  - `ConversationOrchestrator` (Section 8, extended) retrieves session-
    scoped, relevance-thresholded chunks for a detected question — when
    Ollama chat + embeddings are both available — and injects them into
    the answer prompt under an explicit, bounded, delimited context
    block. `answer.completed`'s `sources` field is now a list of
    structured references (`document_id`/`file_name`/`chunk_id`/
    `excerpt`) that always corresponds exactly to what was actually
    retrieved — `[]` whenever nothing was retrieved or nothing met the
    similarity threshold, never fabricated by the model.
  - `KnowledgeIngestionTracker` (Swift, app-lifetime, independent of any
    single Live Session's attach/detach) tracks each document's status —
    Not indexed / Indexing… / Ready / Failed to index / Unsupported
    document — shown in `LiveSessionView`'s new "DOCUMENTS" section.
    `IPCEventRouter` folds each structured source into a compact
    `"filename: excerpt"` string for `CopilotAnswer.sources: [String]`,
    so `OverlayView`'s existing compact source line needed no changes.

## What is explicitly NOT implemented in this phase

- System audio capture — only microphone input is captured (`Audio/`
  never touches system audio/other applications' output).
- OCR (scanned/image-only PDFs extract as empty text and are rejected),
  cloud embeddings/retrieval, external/hosted vector databases, and a
  Python-owned session database — Python's knowledge store holds only
  derived chunk/embedding/status data (see `docs/KNOWLEDGE_RETRIEVAL.md`).
- Coding-practice mode, system-design mode.
- Any anti-proctoring / anti-cheat-bypass / meeting-app-injection code.
  Not in scope now or ever, per the build prompt.
- Production packaging/bundling of the Python runtime — see
  `docs/PYTHON_PACKAGING.md` for the (unimplemented) plan.

## Data flow (three possible sources, question/answer intelligence layered on top of #1)

**1. Real transcription** (used when the worker is `.ready`, a
microphone `AudioCapturing` is configured, permission is authorized, and
Python's local Whisper setup succeeds — see `docs/REALTIME_TRANSCRIPTION.md`).
Question detection/answer generation (Section 8) run *inside* this same
path whenever Ollama is also available — see
`docs/QUESTION_AND_ANSWER_INTELLIGENCE.md`:

```text
MicrophoneAudioCapture (AVAudioEngine)
        │  mono 16kHz pcm_s16le chunks
        ▼
AudioChunkSender (bounded in-flight RPCs) ──► core/veya/transcription (rolling window + local Whisper)
                                                      │  transcript.final events
                                                      ├──────────────────────────────────────────────┐
                                                      ▼                                                ▼
                                   IPCClient ──► PythonWorkerManager ──► IPCEventRouter    core/veya/conversation
                                                      │                        ▲          (question_detector → context_builder
                                                      ▼                        │           → llm/ollama_provider, if available)
                              ConversationState.ingestTranscriptSegment        │                        │
                              / ingestDetectedQuestion / ingestAnswer  ◄───────┴── question.detected / answer.started
                                                      │                            / answer.delta / answer.completed
                                                      ▼                            (sequence-checked by IPCEventRouter)
                              ConversationRepository (persisted) ──► Overlay SwiftUI panel updates
```

**2. Python mock feed** (used when the worker is `.ready` but real
transcription isn't available for any reason):

```text
core/veya (asyncio worker, real subprocess)
        │  JSON Lines events over stdout
        ▼
IPCClient (actor) ──► PythonWorkerManager ──► IPCEventRouter
        │
        ▼
ConversationState.ingestTranscriptSegment / ingestDetectedQuestion / ingestAnswer
        │                                            │
        ▼                                            ▼
Overlay SwiftUI panel updates          ConversationRepository (persisted, same
                                        tables as before — Swift/GRDB unchanged)
```

**3. Swift fallback** (used only when the worker itself isn't `.ready`,
or `session.start` fails):

```text
MockTranscriptSource (timer, canned script)
        │  produces
        ▼
ConversationState.ingest(_:) — canned keyword match + MockAnswerGenerator
        │
        ▼
ConversationRepository (persisted) ──► Overlay SwiftUI panel updates
```

`PythonIntelligenceCoordinator` picks exactly one of these three per
session — never more than one at once — and `LiveSessionView` shows which
one is active via `liveSessionIndicatorText`.

Real question detection, LLM-backed answer generation (Section 8), and
document-grounded retrieval (Section 9) are all implemented now, but only
for the real-transcription path — the Python mock feed and Swift fallback
paths above are both unchanged, deliberately simple canned pipelines. The
`Intelligence`/`Knowledge` Swift stub protocols remain unused; they're
reserved for a possible future Swift-side (on-device) implementation of
either capability, not the current Python-worker-backed ones, which don't
go through them — see "What is explicitly NOT implemented" above for what
still doesn't exist anywhere (RAG beyond this section's local retrieval,
Coding/System Design Copilot, etc.).
