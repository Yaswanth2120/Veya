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
│   │   ├── Settings/        # IMPLEMENTED (minimal) — settings screen
│   │   └── History/         # IMPLEMENTED — previous sessions list
│   ├── Windowing/
│   │   ├── OverlayWindowController.swift   # IMPLEMENTED
│   │   ├── GlobalHotkeyManager.swift       # IMPLEMENTED
│   │   # No PresenterPrivacyManager / CaptureCompatibilityTester here.
│   │   # That subsystem is out of scope for this prompt and is being
│   │   # built separately by the project owner.
│   ├── Audio/                # STUB ONLY — protocol, no capture logic
│   ├── Transcription/        # STUB ONLY — protocol, no real transcription
│   ├── Intelligence/         # STUB ONLY — protocol, no LLM/answer-gen
│   ├── Knowledge/            # STUB ONLY — protocol, no RAG/ingestion
│   ├── Storage/
│   │   ├── Database/         # IMPLEMENTED — GRDB pool + migrations
│   │   ├── Models/           # IMPLEMENTED — Codable/FetchableRecord models
│   │   └── Repositories/     # IMPLEMENTED — CRUD repositories
│   └── Providers/            # STUB ONLY — protocol placeholders
├── Tests/VeyaTests/          # IMPLEMENTED — unit tests for this phase
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
  file metadata + a copy of the file, but does **not** parse/chunk/embed it.
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
  `DetectedQuestion`, `GeneratedAnswer`.

## What is explicitly NOT implemented in this phase

- **Presenter privacy / capture exclusion / capture-compatibility testing**
  of any kind. Not present anywhere in `Windowing/` or elsewhere. This is a
  separate subsystem being built by the project owner outside this prompt.
  Anywhere a design decision brushed up against this, a
  `// TODO: presenter-privacy — out of scope, see project owner` comment
  was left instead of code.
- Real audio capture (`Audio/` is a stub protocol only).
- Real transcription (`Transcription/` is a stub protocol only).
- Real question detection or LLM-backed answer generation
  (`Intelligence/` is a stub protocol only).
- RAG / document parsing / embeddings / retrieval (`Knowledge/` is a stub
  protocol only; `SessionDocument` stores metadata + file reference only).
- Coding-practice mode, system-design mode.
- Any anti-proctoring / anti-cheat-bypass / meeting-app-injection code.
  Not in scope now or ever, per the build prompt.

## Data flow (this phase, mocked)

```text
MockTranscriptSource (timer, canned script)
        │  produces
        ▼
TranscriptSegment  ──────────────► TranscriptRepository (persisted)
        │
        ▼
ConversationState (turns, timestamps) — @MainActor ObservableObject
        │  canned keyword match on "final" transcript segments
        ▼
DetectedQuestion  ────────────────► QuestionRepository (persisted)
        │
        ▼
MockAnswerGenerator (canned talking points, no LLM call)
        │  produces
        ▼
CopilotAnswer  ───────────────────► AnswerRepository (persisted)
        │
        ▼
OverlayWindowController (observes ConversationState.currentAnswer)
        │
        ▼
Overlay SwiftUI panel updates
```

Later phases replace `MockTranscriptSource` with a real `AudioCapture` +
`Transcriber` implementation, and `MockAnswerGenerator` with a real
`Knowledge`-backed `AnswerGenerator`, without changing `ConversationState`'s
public surface — that's the point of defining the models and the
`Providers`/`Transcription`/`Intelligence`/`Knowledge` stub protocols now.
