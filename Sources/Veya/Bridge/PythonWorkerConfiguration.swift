import Foundation

/// Where/how to launch the Python worker, and the timing knobs that
/// govern its lifecycle. See `docs/PYTHON_PACKAGING.md` for the plan to
/// replace the dev-time defaults here with a bundled runtime.
struct PythonWorkerConfiguration: Sendable {
    var pythonExecutableURL: URL
    /// Arguments passed to `pythonExecutableURL` before the worker starts
    /// reading stdin. Dev default is `["python3", "-m", "veya"]` launched
    /// via `/usr/bin/env` (see `resolveDefault`).
    var pythonArguments: [String]
    var workerDirectoryURL: URL
    /// Where `CreateSessionViewModel` copies `SessionDocument` files —
    /// `~/Library/Application Support/Veya/SessionDocuments/`. Passed to
    /// the worker as `VEYA_DOCUMENTS_DIRECTORY` (Section 9) so Python can
    /// validate every `knowledge.ingest` `file_path` resolves beneath
    /// this directory before ever opening it — never an arbitrary path.
    var documentsDirectoryURL: URL
    /// Where Python's own derived knowledge index (chunk metadata +
    /// embeddings SQLite — never session/transcript/answer data, which
    /// stays exclusively in Swift/GRDB) lives —
    /// `~/Library/Application Support/Veya/KnowledgeIndex/`. Passed as
    /// `VEYA_KNOWLEDGE_INDEX_DIRECTORY`.
    var knowledgeIndexDirectoryURL: URL
    /// Where Python's durable, user-approved memory SQLite database lives
    /// (Section 13) — `~/Library/Application Support/Veya/Memory/`.
    /// Passed as `VEYA_MEMORY_DATABASE_PATH`. Tests override this to a
    /// temporary path so they never touch a developer's real memory store.
    var memoryDatabasePathURL: URL
    /// Where Python's durable, per-session `SessionReport` cache lives
    /// (Section 13) — `~/Library/Application Support/Veya/SessionReports/`.
    /// Passed as `VEYA_REPORT_STORE_DIRECTORY`. A worker restart must
    /// still be able to answer `session.report.get` from this directory.
    var reportStoreDirectoryURL: URL

    /// Real transcription's local Whisper binary/model, resolved by
    /// `PythonIntelligenceCoordinator.prepareRealTranscriptionAssets()`
    /// before the worker's first launch and passed through as
    /// `VEYA_WHISPER_BIN`/`VEYA_WHISPER_MODEL`. `nil` means "not resolved
    /// yet" or "unavailable" — `transcription.start` then reports real
    /// transcription unavailable and callers fall back to the Python mock
    /// feed, exactly as when these env vars are simply unset today. An
    /// explicit `VEYA_WHISPER_BIN`/`VEYA_WHISPER_MODEL` already present in
    /// the inherited process environment always wins over these (see
    /// `PythonWorkerManager.launchProcessAndWaitForReady`).
    var whisperBinaryPathURL: URL?
    var whisperModelPathURL: URL?

    var rpcTimeout: TimeInterval = 5
    var readyTimeout: TimeInterval = 10
    /// `system.ping` cadence — only runs while a Python-driven Live
    /// Session is active (see `PythonWorkerManager.beginHealthChecking`).
    var healthCheckInterval: TimeInterval = 10
    /// Consecutive ping failures required before `.ready` becomes
    /// `.unhealthy` — a single transient failure must not flip this.
    var maxConsecutivePingFailuresBeforeUnhealthy: Int = 3
    var maxRestartAttempts: Int = 3
    var restartBackoffBaseSeconds: Double = 1.0

    static let pythonExecutableEnvironmentKey = "VEYA_PYTHON_EXECUTABLE"
    static let workerDirectoryEnvironmentKey = "VEYA_WORKER_DIRECTORY"

    /// Resolves configuration from environment variables, falling back to
    /// development defaults. Never hardcodes `/usr/bin/python3` or a
    /// developer-specific absolute path:
    /// - `VEYA_PYTHON_EXECUTABLE` unset → launch `/usr/bin/env python3`,
    ///   which resolves `python3` dynamically via `PATH` at launch time
    ///   (the same mechanism a `#!/usr/bin/env python3` shebang uses),
    ///   rather than assuming any single fixed interpreter location.
    /// - `VEYA_WORKER_DIRECTORY` unset → a project-relative default
    ///   resolved from this source file's own compile-time path, valid
    ///   only for `swift run`/`swift test` against a checkout of this
    ///   repository.
    static func resolveDefault(environment: [String: String] = ProcessInfo.processInfo.environment) -> PythonWorkerConfiguration {
        let pythonExecutableURL: URL
        let pythonArguments: [String]

        // A packaged `.app`'s bundled runtime — checked before the
        // dev-time `/usr/bin/env python3` fallback — so a release build
        // launches its own vendored interpreter and never depends on the
        // end user having a compatible `python3` on `PATH`. Explicit
        // `VEYA_PYTHON_EXECUTABLE`/`VEYA_WORKER_DIRECTORY` overrides
        // always win over both, for development and CI.
        let bundledPythonExecutableURL = Bundle.main.resourceURL?
            .appendingPathComponent("python-runtime/bin/python3")
        let bundledWorkerDirectoryURL = Bundle.main.resourceURL?
            .appendingPathComponent("veya-worker", isDirectory: true)
        let bundledRuntimeIsUsable = bundledPythonExecutableURL.map { FileManager.default.isExecutableFile(atPath: $0.path) } ?? false

        if let overridePath = environment[pythonExecutableEnvironmentKey], !overridePath.isEmpty {
            pythonExecutableURL = URL(fileURLWithPath: overridePath)
            pythonArguments = ["-m", "veya"]
        } else if bundledRuntimeIsUsable, let bundledPythonExecutableURL {
            pythonExecutableURL = bundledPythonExecutableURL
            pythonArguments = ["-m", "veya"]
        } else {
            pythonExecutableURL = URL(fileURLWithPath: "/usr/bin/env")
            pythonArguments = ["python3", "-m", "veya"]
        }

        let workerDirectoryURL: URL
        if let overrideDirectory = environment[workerDirectoryEnvironmentKey], !overrideDirectory.isEmpty {
            workerDirectoryURL = URL(fileURLWithPath: overrideDirectory, isDirectory: true)
        } else if bundledRuntimeIsUsable, let bundledWorkerDirectoryURL {
            workerDirectoryURL = bundledWorkerDirectoryURL
        } else {
            workerDirectoryURL = projectRelativeDefaultWorkerDirectory()
        }

        return PythonWorkerConfiguration(
            pythonExecutableURL: pythonExecutableURL,
            pythonArguments: pythonArguments,
            workerDirectoryURL: workerDirectoryURL,
            documentsDirectoryURL: defaultDocumentsDirectory(),
            knowledgeIndexDirectoryURL: defaultKnowledgeIndexDirectory(),
            memoryDatabasePathURL: defaultMemoryDatabasePath(),
            reportStoreDirectoryURL: defaultReportStoreDirectory()
        )
    }

    static let documentsDirectoryEnvironmentKey = "VEYA_DOCUMENTS_DIRECTORY"
    static let knowledgeIndexDirectoryEnvironmentKey = "VEYA_KNOWLEDGE_INDEX_DIRECTORY"
    static let memoryDatabasePathEnvironmentKey = "VEYA_MEMORY_DATABASE_PATH"
    static let reportStoreDirectoryEnvironmentKey = "VEYA_REPORT_STORE_DIRECTORY"

    static func defaultMemoryDatabasePath() -> URL {
        applicationSupportVeyaDirectory().appendingPathComponent("Memory/memory.sqlite", isDirectory: false)
    }

    static func defaultReportStoreDirectory() -> URL {
        applicationSupportVeyaDirectory().appendingPathComponent("SessionReports", isDirectory: true)
    }

    /// Resolves a usable local `whisper-cli` binary, if any: a packaged
    /// release's bundled copy (`Resources/whisper-bin/whisper-cli`, see
    /// `packaging/build_app.sh`), else the dev-time, checkout-relative
    /// `whisper.cpp/build/bin/whisper-cli` (only meaningful for `swift
    /// run`/`swift test` against a checkout that has actually built
    /// whisper.cpp). Returns `nil` — never a guessed/system path — if
    /// neither exists, so real transcription simply stays unavailable
    /// rather than launching an unverified binary.
    static func resolveWhisperBinary() -> URL? {
        if let bundled = Bundle.main.resourceURL?.appendingPathComponent("whisper-bin/whisper-cli"),
           FileManager.default.isExecutableFile(atPath: bundled.path) {
            return bundled
        }
        let devRelative = projectRelativeDefaultWorkerDirectory()
            .deletingLastPathComponent() // <repo root>
            .appendingPathComponent("whisper.cpp/build/bin/whisper-cli")
        if FileManager.default.isExecutableFile(atPath: devRelative.path) {
            return devRelative
        }
        return nil
    }

    /// Same Application Support path `CreateSessionViewModel` copies
    /// `SessionDocument` files into — the two must agree, since this is
    /// the boundary Python validates every `knowledge.ingest` path
    /// resolves beneath.
    static func defaultDocumentsDirectory() -> URL {
        applicationSupportVeyaDirectory().appendingPathComponent("SessionDocuments", isDirectory: true)
    }

    /// Sibling of `SessionDocuments/` and of `DatabaseManager`'s
    /// `veya.sqlite` — Python's own derived-data directory, never shared
    /// with Swift/GRDB's database file.
    static func defaultKnowledgeIndexDirectory() -> URL {
        applicationSupportVeyaDirectory().appendingPathComponent("KnowledgeIndex", isDirectory: true)
    }

    private static func applicationSupportVeyaDirectory() -> URL {
        let appSupport = (try? FileManager.default.url(
            for: .applicationSupportDirectory,
            in: .userDomainMask,
            appropriateFor: nil,
            create: true
        )) ?? FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support")
        return appSupport.appendingPathComponent("Veya", isDirectory: true)
    }

    /// Dev-only fallback: resolves `core/` as a sibling of `Package.swift`
    /// via this source file's compile-time path (`#filePath`). Only
    /// meaningful for `swift run`/`swift test` from a checkout of this
    /// repository — a packaged release resolves the worker directory from
    /// bundled resources instead (see `docs/PYTHON_PACKAGING.md`), never
    /// from this.
    static func projectRelativeDefaultWorkerDirectory() -> URL {
        let sourceFile = URL(fileURLWithPath: #filePath)
        let repoRoot = sourceFile
            .deletingLastPathComponent() // Bridge/
            .deletingLastPathComponent() // Veya/
            .deletingLastPathComponent() // Sources/
            .deletingLastPathComponent() // <repo root>
        return repoRoot.appendingPathComponent("core", isDirectory: true)
    }
}
