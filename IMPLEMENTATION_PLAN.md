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
