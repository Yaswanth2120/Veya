import Darwin
import Foundation
import Testing
@testable import Veya

/// Whether a real Python interpreter with the `veya` package importable is
/// available in this environment. Gates the integration suite below —
/// per the build prompt, ordinary Swift unit tests must not require a
/// real Python install, but a small *optional* integration/smoke suite
/// against the real worker is explicitly called for when Python *is*
/// available, which it is in this dev environment.
enum PythonAvailability {
    static let isAvailable: Bool = {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/env")
        process.arguments = ["python3", "-c", "import veya"]
        process.currentDirectoryURL = PythonWorkerConfiguration.projectRelativeDefaultWorkerDirectory()
        process.standardOutput = Pipe()
        process.standardError = Pipe()
        do {
            try process.run()
            process.waitUntilExit()
            return process.terminationStatus == 0
        } catch {
            return false
        }
    }()
}

/// Exercises `PythonWorkerManager` against the **real** `core/veya` worker
/// subprocess — genuine process launch, IPC handshake, ping, crash/restart,
/// and shutdown, not a fake transport. This is the "documented smoke test
/// for a real Python worker" the build prompt asks for; skipped
/// automatically wherever `python3`/`veya` aren't available, so it never
/// blocks an ordinary `swift test` run in a Python-less environment.
@MainActor
@Suite("PythonWorkerManager (real subprocess integration)", .enabled(if: PythonAvailability.isAvailable))
struct PythonWorkerManagerIntegrationTests {
    private func makeManager(
        maxRestartAttempts: Int = 3,
        restartBackoffBaseSeconds: Double = 0.3,
        documentsDirectoryURL: URL? = nil
    ) -> PythonWorkerManager {
        var configuration = PythonWorkerConfiguration.resolveDefault()
        configuration.readyTimeout = 5
        configuration.rpcTimeout = 3
        configuration.maxRestartAttempts = maxRestartAttempts
        configuration.restartBackoffBaseSeconds = restartBackoffBaseSeconds
        if let documentsDirectoryURL {
            configuration.documentsDirectoryURL = documentsDirectoryURL
        }
        return PythonWorkerManager(configuration: configuration)
    }

    @Test("starting the manager launches the real worker and reaches .ready")
    func startReachesReady() async {
        let manager = makeManager()
        await manager.start()
        #expect(manager.state == .ready)
        await manager.stop()
    }

    @Test("system.ping succeeds once the worker is ready")
    func pingSucceeds() async throws {
        let manager = makeManager()
        await manager.start()
        let result: PingResult = try await manager.call(method: "system.ping", params: EmptyIPCParams())
        #expect(result.pong == true)
        await manager.stop()
    }

    @Test("system.info reports the protocol version and a worker version")
    func infoReportsMetadata() async throws {
        let manager = makeManager()
        await manager.start()
        let info: SystemInfoResult = try await manager.call(method: "system.info", params: EmptyIPCParams())
        #expect(info.protocolVersion == IPCProtocolVersion.current)
        #expect(!info.workerVersion.isEmpty)
        await manager.stop()
    }

    @Test("graceful shutdown returns the manager to .stopped and the process exits")
    func gracefulShutdownStops() async throws {
        let manager = makeManager()
        await manager.start()
        #expect(manager.state == .ready)

        await manager.stop()

        #expect(manager.state == .stopped)
    }

    @Test("an unexpected process exit triggers a bounded restart back to .ready")
    func unexpectedExitTriggersRestart() async throws {
        let manager = makeManager(maxRestartAttempts: 3, restartBackoffBaseSeconds: 0.2)
        await manager.start()
        #expect(manager.state == .ready)

        let info: SystemInfoResult = try await manager.call(method: "system.info", params: EmptyIPCParams())
        kill(pid_t(info.pid), SIGKILL)

        // `state == .ready` is already true *before* the kill takes
        // effect (SIGKILL delivery and Foundation's SIGCHLD handling
        // aren't instantaneous), so waiting for "state == .ready" alone
        // would be trivially (and wrongly) satisfied immediately, racing
        // ahead of the actual crash+restart. Wait for the state to
        // actually *leave* .ready first, proving the crash was observed,
        // before waiting for it to settle back to .ready.
        try await waitUntil(timeout: 3) { manager.state != .ready }
        try await waitUntil(timeout: 5) { manager.state == .ready }

        #expect(manager.state == .ready)
        await manager.stop()
    }

    @Test("bounded restart gives up after the configured number of attempts")
    func boundedRestartGivesUpEventually() async throws {
        func isFailed(_ state: PythonWorkerState) -> Bool {
            if case .failed = state { return true }
            return false
        }

        let manager = makeManager(maxRestartAttempts: 1, restartBackoffBaseSeconds: 0.1)
        await manager.start()
        #expect(manager.state == .ready)

        // maxRestartAttempts=1 means: kill #1 restarts successfully back to
        // .ready, kill #2 exceeds the bound and lands on .failed. Each
        // iteration first waits for the state to actually *leave* .ready
        // (proving the crash was observed — `state == .ready` is already
        // true *before* the kill takes effect, so waiting for "ready or
        // failed" alone would be trivially satisfied immediately and race
        // ahead of the real crash+restart), then waits for it to settle.
        for _ in 0..<3 {
            guard manager.state == .ready else { break }
            guard let info: SystemInfoResult = try? await manager.call(method: "system.info", params: EmptyIPCParams()) else {
                break
            }
            kill(pid_t(info.pid), SIGKILL)
            try await waitUntil(timeout: 3) { manager.state != .ready }
            try await waitUntil(timeout: 5) { manager.state == .ready || isFailed(manager.state) }
        }

        try await waitUntil(timeout: 5) { isFailed(manager.state) }

        #expect(isFailed(manager.state))
    }

    @Test("a full Python-driven session integrates real worker events into ConversationState")
    func fullPythonDrivenSessionIntegration() async throws {
        let workerManager = makeManager()
        let eventRouter = IPCEventRouter()
        let coordinator = PythonIntelligenceCoordinator(workerManager: workerManager, eventRouter: eventRouter)

        await workerManager.start()
        #expect(workerManager.state == .ready)

        let db = DatabaseManager.makeInMemory()
        let session = Session.makeTestSession(title: "Real Worker Integration")
        try await SessionRepository(db: db).create(session)
        let state = ConversationState(sessionID: session.id, repository: ConversationRepository(db: db))

        await coordinator.beginLiveSession(state: state, session: session)
        #expect(coordinator.drivingSource == .pythonWorker)

        try await waitUntil(timeout: 10) { state.currentAnswer != nil }

        #expect(!state.segments.isEmpty)
        #expect(!state.detectedQuestions.isEmpty)
        #expect(state.currentAnswer?.sources.isEmpty == false)

        try await waitUntil(timeout: 5) { state.phase == .live }
        await coordinator.endLiveSession(sessionID: session.id)
        await workerManager.stop()
    }

    /// Real subprocess, real file on disk, real path validation, real
    /// chunking — the embedding step may or may not succeed depending on
    /// whether this environment has a local embedding model configured
    /// (not required for this test, only `PythonAvailability` is), so
    /// this only asserts the document reaches a terminal, non-"not
    /// indexed" status and that a `knowledge.ingestion_started` really
    /// arrived — proving the RPC → event → `KnowledgeIngestionTracker`
    /// pipeline works end-to-end regardless of embedding availability.
    @Test("knowledge.ingest on a real document reaches a terminal status via real events")
    func knowledgeIngestRealDocumentReachesTerminalStatus() async throws {
        let tmp = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: tmp, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: tmp) }

        let documentPath = tmp.appendingPathComponent("notes.txt")
        try "The migration took six weeks because of a staged rollout.".write(to: documentPath, atomically: true, encoding: .utf8)

        let workerManager = makeManager(documentsDirectoryURL: tmp)
        let tracker = KnowledgeIngestionTracker()
        let eventRouter = IPCEventRouter(knowledgeTracker: tracker)
        workerManager.eventHandler = { [weak eventRouter] event in
            await eventRouter?.route(event)
        }

        await workerManager.start()
        #expect(workerManager.state == .ready)

        let sessionID = UUID()
        let documentID = UUID()
        let _: OkResult = try await workerManager.call(
            method: "knowledge.ingest",
            params: KnowledgeIngestParams(
                sessionId: sessionID.uuidString,
                documentId: documentID.uuidString,
                fileName: "notes.txt",
                fileExtension: "txt",
                filePath: documentPath.path
            )
        )

        let terminalStatuses: Set<DocumentIngestionStatus> = [.ready, .failed, .unsupported]
        try await waitUntil(timeout: 10) { terminalStatuses.contains(tracker.status(forDocumentID: documentID)) }
        #expect(terminalStatuses.contains(tracker.status(forDocumentID: documentID)))

        await workerManager.stop()
    }

    /// Proves the second rung of the three-way selection order: a `.ready`
    /// worker with real transcription *configured* (an `AudioCapturing` +
    /// authorized permission) still falls back to the Python mock feed —
    /// not the Swift fallback — when Python itself reports real
    /// transcription as unavailable (no `VEYA_WHISPER_BIN`/
    /// `VEYA_WHISPER_MODEL` configured for this worker process, which is
    /// the default here since `makeManager()` doesn't set them).
    @Test("transcription setup unavailable falls back to the Python mock feed, not the Swift fallback")
    func transcriptionSetupUnavailableFallsBackToMockFeed() async throws {
        let workerManager = makeManager()
        let eventRouter = IPCEventRouter()
        let audioCapture = FakeAudioCapture()
        let permission = FakeMicrophonePermission(status: .authorized)
        let coordinator = PythonIntelligenceCoordinator(
            workerManager: workerManager,
            eventRouter: eventRouter,
            audioCapture: audioCapture,
            microphonePermission: permission
        )

        await workerManager.start()
        #expect(workerManager.state == .ready)

        let db = DatabaseManager.makeInMemory()
        let session = Session.makeTestSession(title: "Transcription Setup Unavailable")
        try await SessionRepository(db: db).create(session)
        let state = ConversationState(sessionID: session.id, repository: ConversationRepository(db: db))

        await coordinator.beginLiveSession(state: state, session: session)

        #expect(coordinator.drivingSource == .pythonWorker)
        #expect(audioCapture.startCallCount == 0)
        #expect(coordinator.liveSessionIndicatorText == "Transcription setup unavailable")

        try await waitUntil(timeout: 5) { state.phase == .live }
        await coordinator.endLiveSession(sessionID: session.id)
        await workerManager.stop()
    }

    /// Proves microphone permission denial is treated the same as
    /// "real transcription unavailable" — falls back to the Python mock
    /// feed rather than skipping straight to the Swift fallback (the
    /// worker itself is fine) or throwing.
    @Test("denied microphone permission falls back to the Python mock feed")
    func microphonePermissionDeniedFallsBackToMockFeed() async throws {
        let workerManager = makeManager()
        let eventRouter = IPCEventRouter()
        let audioCapture = FakeAudioCapture()
        let permission = FakeMicrophonePermission(status: .denied)
        let coordinator = PythonIntelligenceCoordinator(
            workerManager: workerManager,
            eventRouter: eventRouter,
            audioCapture: audioCapture,
            microphonePermission: permission
        )

        await workerManager.start()
        #expect(workerManager.state == .ready)

        let db = DatabaseManager.makeInMemory()
        let session = Session.makeTestSession(title: "Microphone Permission Denied")
        try await SessionRepository(db: db).create(session)
        let state = ConversationState(sessionID: session.id, repository: ConversationRepository(db: db))

        await coordinator.beginLiveSession(state: state, session: session)

        #expect(coordinator.drivingSource == .pythonWorker)
        #expect(audioCapture.startCallCount == 0)
        #expect(permission.requestAccessCallCount == 1)
        #expect(coordinator.liveSessionIndicatorText == "Microphone permission required")

        try await waitUntil(timeout: 5) { state.phase == .live }
        await coordinator.endLiveSession(sessionID: session.id)
        await workerManager.stop()
    }

    /// Regression test for the event-attachment race a review flagged:
    /// `eventRouter.attach(...)` used to run only after `session.start`
    /// *and* `mock.start_live_feed` had already resolved, so a fast worker
    /// could emit `session.started`/the very first `transcript.partial`/
    /// `transcript.final` before Swift was attached, and those would be
    /// silently dropped. `DEFAULT_SCRIPT` has exactly 5 lines, so asserting
    /// all 5 final segments arrive (not 4, not fewer) proves the very
    /// first line — the one most likely to race ahead of a late attach —
    /// was not dropped.
    @Test("no transcript segments are dropped between session start and feed start")
    func noEarlyEventsAreDroppedAtSessionStart() async throws {
        let workerManager = makeManager()
        let eventRouter = IPCEventRouter()
        let coordinator = PythonIntelligenceCoordinator(workerManager: workerManager, eventRouter: eventRouter)

        await workerManager.start()
        #expect(workerManager.state == .ready)

        let db = DatabaseManager.makeInMemory()
        let session = Session.makeTestSession(title: "Early Event Race")
        try await SessionRepository(db: db).create(session)
        let state = ConversationState(sessionID: session.id, repository: ConversationRepository(db: db))

        await coordinator.beginLiveSession(state: state, session: session)
        #expect(coordinator.drivingSource == .pythonWorker)

        try await waitUntil(timeout: 10) { state.currentAnswer != nil }
        try await waitUntil(timeout: 3) { state.segments.count >= 5 }

        #expect(state.segments.count == 5)
        #expect(state.segments.first?.text == "Thanks everyone for joining, let's get started with the migration recap.")

        await coordinator.endLiveSession(sessionID: session.id)
        await workerManager.stop()
    }

    /// Regression test for the mid-session-crash gap a review flagged:
    /// `PythonWorkerManager` used to restart the process transparently on
    /// an unexpected exit, but nothing told `PythonIntelligenceCoordinator`
    /// this had happened, so an active session's `ConversationState` just
    /// stopped receiving events — the fallback was only ever selected at
    /// session *start*. Kills the worker mid-session and asserts the
    /// coordinator observes the resulting `.restarting` transition and
    /// switches the live session over to the Swift fallback timer, which
    /// keeps producing segments afterward.
    @Test("a worker crash during an active session switches to the Swift fallback")
    func workerCrashDuringSessionSwitchesToFallback() async throws {
        let workerManager = makeManager(maxRestartAttempts: 3, restartBackoffBaseSeconds: 0.2)
        let eventRouter = IPCEventRouter()
        let coordinator = PythonIntelligenceCoordinator(workerManager: workerManager, eventRouter: eventRouter)

        await workerManager.start()
        #expect(workerManager.state == .ready)

        let db = DatabaseManager.makeInMemory()
        let session = Session.makeTestSession(title: "Mid-Session Crash")
        try await SessionRepository(db: db).create(session)
        let state = ConversationState(sessionID: session.id, repository: ConversationRepository(db: db))

        await coordinator.beginLiveSession(state: state, session: session)
        #expect(coordinator.drivingSource == .pythonWorker)

        let info: SystemInfoResult = try await workerManager.call(method: "system.info", params: EmptyIPCParams())
        kill(pid_t(info.pid), SIGKILL)

        try await waitUntil(timeout: 3) { coordinator.drivingSource == .swiftFallback }

        let segmentCountAfterFallbackBegins = state.segments.count
        try await waitUntil(timeout: 5) { state.segments.count > segmentCountAfterFallbackBegins }

        #expect(coordinator.drivingSource == .swiftFallback)
        #expect(state.phase == .live)

        state.end()
        await workerManager.stop()
    }
}

/// Whether a real local Whisper binary + model are configured. Opt-in only
/// (`VEYA_WHISPER_BIN`/`VEYA_WHISPER_MODEL` must be set explicitly, same
/// as `core/tests/test_whisper_smoke.py`'s manual invocation) — never
/// auto-detected, so an ordinary `./run-tests.sh` run never depends on a
/// whisper.cpp checkout being present. `Process` inherits its parent's
/// environment by default (see `PythonWorkerManager`'s process setup), so
/// these variables reach the real worker subprocess unchanged.
enum WhisperAvailability {
    static let isAvailable: Bool = {
        let environment = ProcessInfo.processInfo.environment
        guard let bin = environment["VEYA_WHISPER_BIN"], let model = environment["VEYA_WHISPER_MODEL"] else {
            return false
        }
        return FileManager.default.isExecutableFile(atPath: bin) && FileManager.default.fileExists(atPath: model)
    }()
}

/// Whether a real local Ollama instance is explicitly configured. Opt-in
/// only (`VEYA_OLLAMA_URL`/`VEYA_OLLAMA_MODEL` must both be set, same
/// pattern as `WhisperAvailability`) — Ollama's own "sensible local
/// defaults" (see `docs/QUESTION_AND_ANSWER_INTELLIGENCE.md`) are
/// deliberately not enough to enable this gated suite; a real integration
/// run must ask for it explicitly.
enum OllamaAvailability {
    static let isAvailable: Bool = {
        let environment = ProcessInfo.processInfo.environment
        return environment["VEYA_OLLAMA_URL"] != nil && environment["VEYA_OLLAMA_MODEL"] != nil
    }()
}

/// Manual-only: proves the full Swift → real worker subprocess → real
/// Ollama availability-check → `TranscriptionStartResult` wiring actually
/// works end-to-end. Deliberately does not attempt to force a real
/// spoken *question* through real speech audio (finding/recording
/// question-shaped audio deterministically is out of scope for an
/// automated test) — question-detection → real-answer-generation was
/// instead verified directly against a real local Ollama instance via
/// `ConversationOrchestrator` at the Python layer (see
/// `core/tests/test_ollama_smoke.py` and
/// docs/QUESTION_AND_ANSWER_INTELLIGENCE.md's verification notes).
@MainActor
@Suite(
    "Answer intelligence availability (Python worker + real Whisper + real Ollama)",
    .enabled(if: PythonAvailability.isAvailable && WhisperAvailability.isAvailable && OllamaAvailability.isAvailable)
)
struct AnswerIntelligenceAvailabilityIntegrationTests {
    @Test("answer intelligence is reported available end-to-end when both Whisper and Ollama are configured")
    func answerIntelligenceAvailableEndToEnd() async throws {
        var configuration = PythonWorkerConfiguration.resolveDefault()
        configuration.readyTimeout = 5
        configuration.rpcTimeout = 20
        let workerManager = PythonWorkerManager(configuration: configuration)
        let eventRouter = IPCEventRouter()
        let audioCapture = FakeAudioCapture()
        let permission = FakeMicrophonePermission(status: .authorized)
        let coordinator = PythonIntelligenceCoordinator(
            workerManager: workerManager,
            eventRouter: eventRouter,
            audioCapture: audioCapture,
            microphonePermission: permission
        )

        await workerManager.start()
        #expect(workerManager.state == .ready)

        let db = DatabaseManager.makeInMemory()
        let session = Session.makeTestSession(title: "Answer Intelligence Availability")
        try await SessionRepository(db: db).create(session)
        let state = ConversationState(sessionID: session.id, repository: ConversationRepository(db: db))

        await coordinator.beginLiveSession(state: state, session: session)

        #expect(coordinator.drivingSource == .realTranscription)
        #expect(coordinator.answerIntelligenceAvailable == true)
        #expect(coordinator.liveSessionIndicatorText == "Listening — live transcription")

        await coordinator.endLiveSession(sessionID: session.id)
        await workerManager.stop()
    }
}

/// Manual-only, end-to-end real-transcription smoke test: a real Python
/// worker subprocess, a real local Whisper invocation, and real speech
/// audio (the `jfk.wav` sample bundled with whisper.cpp) — only
/// microphone *hardware* capture is faked (`FakeAudioCapture`), since this
/// dev environment has no audio input device/GUI session to grant a real
/// permission prompt against. Run deliberately with:
///
///     VEYA_WHISPER_BIN=/path/to/whisper-cli \
///     VEYA_WHISPER_MODEL=/path/to/ggml-base.en.bin \
///     ./run-tests.sh
///
/// Skipped otherwise, so it never affects an ordinary test run.
@MainActor
@Suite(
    "Real transcription (Python worker + real Whisper, fake microphone)",
    .enabled(if: PythonAvailability.isAvailable && WhisperAvailability.isAvailable)
)
struct RealTranscriptionIntegrationTests {
    /// Strips the standard 44-byte PCM WAV header, returning raw
    /// `pcm_s16le` samples — `jfk.wav` is already mono 16kHz 16-bit, so no
    /// resampling/format conversion is needed for this smoke test.
    private func loadJFKSamplePCM() throws -> Data {
        let sourceFile = URL(fileURLWithPath: #filePath)
        let repoRoot = sourceFile
            .deletingLastPathComponent() // VeyaTests/
            .deletingLastPathComponent() // Tests/
            .deletingLastPathComponent() // <repo root>
        let wavURL = repoRoot.appendingPathComponent("whisper.cpp/samples/jfk.wav")
        let fullData = try Data(contentsOf: wavURL)
        // `dropFirst` returns a slice whose indices are still relative to
        // the original `Data` (44..<count), not re-based to 0 — wrapping
        // in `Data(...)` copies it into a fresh, zero-based buffer so
        // `subdata(in:)` below can use plain 0-based offsets.
        return Data(fullData.dropFirst(44))
    }

    @Test("real speech audio produces a real transcript.final segment end-to-end")
    func realSpeechProducesTranscript() async throws {
        let pcm = try loadJFKSamplePCM()

        var configuration = PythonWorkerConfiguration.resolveDefault()
        configuration.readyTimeout = 5
        configuration.rpcTimeout = 10
        let workerManager = PythonWorkerManager(configuration: configuration)
        let eventRouter = IPCEventRouter()
        let audioCapture = FakeAudioCapture()
        let permission = FakeMicrophonePermission(status: .authorized)
        let coordinator = PythonIntelligenceCoordinator(
            workerManager: workerManager,
            eventRouter: eventRouter,
            audioCapture: audioCapture,
            microphonePermission: permission
        )

        await workerManager.start()
        #expect(workerManager.state == .ready)

        let db = DatabaseManager.makeInMemory()
        let session = Session.makeTestSession(title: "Real Whisper Smoke Test")
        try await SessionRepository(db: db).create(session)
        let state = ConversationState(sessionID: session.id, repository: ConversationRepository(db: db))

        await coordinator.beginLiveSession(state: state, session: session)
        #expect(coordinator.drivingSource == .realTranscription)
        #expect(audioCapture.startCallCount == 1)

        // Feed real speech PCM in 0.5s chunks — enough for the rolling
        // window (default 4s) to complete at least once. A small delay
        // between chunks mimics real capture's pacing (one chunk roughly
        // every `chunkDuration` of real time); without it, all ~22 chunks
        // would be handed to `AudioChunkSender` in a tight loop far faster
        // than `transcription.audio_chunk` RPCs can complete, saturating
        // its bounded `maxInFlight` and dropping most of them — an
        // artifact of this test's instant delivery, not of real capture.
        let sampleRate = 16000
        let bytesPerChunk = Int(0.5 * Double(sampleRate)) * 2
        var sequence = 0
        var offset = 0
        while offset < pcm.count {
            let end = min(offset + bytesPerChunk, pcm.count)
            let chunkData = pcm.subdata(in: offset..<end)
            audioCapture.simulateChunk(
                AudioChunk(
                    sequence: sequence,
                    startedAt: Double(sequence) * 0.5,
                    duration: 0.5,
                    pcm: chunkData,
                    sampleRate: sampleRate,
                    channels: 1
                )
            )
            sequence += 1
            offset = end
            try? await Task.sleep(nanoseconds: 30_000_000)
        }

        try await waitUntil(timeout: 20) { !state.segments.isEmpty }

        #expect(!state.segments.isEmpty)
        let combinedText = state.segments.map(\.text).joined(separator: " ").lowercased()
        #expect(combinedText.contains("country") || combinedText.contains("american") || combinedText.contains("ask"))

        // Exactly one intelligence source drives a session: the Python
        // mock feed's canned question/answer content (see
        // `core/veya/mock/live_feed.py`'s DEFAULT_SCRIPT) must never leak
        // in alongside real transcription content.
        #expect(state.detectedQuestions.isEmpty)
        #expect(state.currentAnswer == nil)

        await coordinator.endLiveSession(sessionID: session.id)
        #expect(audioCapture.stopCallCount == 1)

        await workerManager.stop()
    }

    /// Regression test for a review finding: `drivingSource` used to only
    /// become `.realTranscription` at the very end of
    /// `tryBeginRealTranscription`, so `handleWorkerStateChange`'s old
    /// guard (which required `drivingSource` to already match) silently
    /// ignored a worker crash landing *during* that startup sequence —
    /// leaving the coordinator with a claimed session, no active
    /// pipeline, and no fallback. Uses `FakeAudioCapture.startGate` to
    /// deterministically land the crash exactly inside that window: after
    /// `transcription.start` has already succeeded (so the worker really
    /// is mid-setup, not merely refusing a request) but before
    /// `audioCapture.start()` returns.
    @Test("a worker crash during real-transcription startup still falls back to Swift, without a later commit clobbering it")
    func workerCrashDuringRealTranscriptionStartupFallsBack() async throws {
        var configuration = PythonWorkerConfiguration.resolveDefault()
        configuration.readyTimeout = 5
        configuration.rpcTimeout = 10
        configuration.maxRestartAttempts = 3
        configuration.restartBackoffBaseSeconds = 0.2
        let workerManager = PythonWorkerManager(configuration: configuration)
        let eventRouter = IPCEventRouter()
        let audioCapture = FakeAudioCapture()
        let permission = FakeMicrophonePermission(status: .authorized)
        let coordinator = PythonIntelligenceCoordinator(
            workerManager: workerManager,
            eventRouter: eventRouter,
            audioCapture: audioCapture,
            microphonePermission: permission
        )

        await workerManager.start()
        #expect(workerManager.state == .ready)
        let info: SystemInfoResult = try await workerManager.call(method: "system.info", params: EmptyIPCParams())

        let startEnteredGate = SendGate()
        audioCapture.onStartBegan = { Task { await startEnteredGate.open() } }
        let proceedGate = SendGate()
        audioCapture.startGate = proceedGate

        let db = DatabaseManager.makeInMemory()
        let session = Session.makeTestSession(title: "Crash During Startup")
        try await SessionRepository(db: db).create(session)
        let state = ConversationState(sessionID: session.id, repository: ConversationRepository(db: db))

        let beginTask = Task { await coordinator.beginLiveSession(state: state, session: session) }

        // `audioCapture.start()` has now been entered — `transcription.start`
        // already succeeded (Whisper is really configured), and it's
        // blocked on `proceedGate`, i.e. exactly inside the vulnerable
        // window, before `drivingSource` is ever set.
        await startEnteredGate.waitUntilOpened()
        #expect(coordinator.drivingSource == .none)

        kill(pid_t(info.pid), SIGKILL)
        try await waitUntil(timeout: 3) { workerManager.state != .ready }

        // Only now let `tryBeginRealTranscription` resume — its own
        // "was this superseded?" check must stop it from clobbering the
        // fallback `handleWorkerStateChange` already performed above.
        await proceedGate.open()
        await beginTask.value

        #expect(coordinator.drivingSource == .swiftFallback)
        #expect(state.phase == .live)

        state.end()
        try await waitUntil(timeout: 5) { workerManager.state == .ready }
        await workerManager.stop()
    }

    /// Proves the indicator composition against a real `.realTranscription`
    /// session (only reachable with a real worker + real Whisper — the
    /// `drivingSource == .realTranscription` branch of
    /// `liveSessionIndicatorText` reads `ConversationState` directly).
    /// Drives the "Analyzing question…"/"Generating answer…" transitions
    /// directly via `ConversationState`'s own granular methods rather than
    /// via real speech content, since manufacturing question-shaped audio
    /// deterministically is out of scope here — those methods are exactly
    /// what `IPCEventRouter` calls in production for real question/answer
    /// events, so this exercises the same state transitions either way.
    @Test("live session indicator reflects Analyzing/Generating states during real transcription")
    func indicatorTextDuringRealTranscription() async throws {
        var configuration = PythonWorkerConfiguration.resolveDefault()
        configuration.readyTimeout = 5
        configuration.rpcTimeout = 10
        let workerManager = PythonWorkerManager(configuration: configuration)
        let eventRouter = IPCEventRouter()
        let audioCapture = FakeAudioCapture()
        let permission = FakeMicrophonePermission(status: .authorized)
        let coordinator = PythonIntelligenceCoordinator(
            workerManager: workerManager,
            eventRouter: eventRouter,
            audioCapture: audioCapture,
            microphonePermission: permission
        )

        await workerManager.start()
        #expect(workerManager.state == .ready)

        let db = DatabaseManager.makeInMemory()
        let session = Session.makeTestSession(title: "Indicator Composition")
        try await SessionRepository(db: db).create(session)
        let state = ConversationState(sessionID: session.id, repository: ConversationRepository(db: db))

        await coordinator.beginLiveSession(state: state, session: session)
        #expect(coordinator.drivingSource == .realTranscription)
        #expect(coordinator.liveSessionIndicatorText == "Listening — live transcription")

        await state.ingestDetectedQuestion(
            DetectedQuestion(id: UUID(), sessionID: session.id, text: "Why?", detectedAt: Date())
        )
        #expect(coordinator.liveSessionIndicatorText == "Analyzing question…")

        state.setAnswerGenerating(true)
        #expect(coordinator.liveSessionIndicatorText == "Generating answer…")

        state.cancelPendingAnswerActivity()
        #expect(coordinator.liveSessionIndicatorText == "Listening — live transcription")

        await coordinator.endLiveSession(sessionID: session.id)
        await workerManager.stop()
    }
}
