import CryptoKit
import Foundation
import Testing
@testable import Veya

/// Fallback-selection logic, tested without ever launching a real Python
/// process — `PythonWorkerManager` here is a plain, never-started instance
/// (so its `state` stays `.stopped`), which is exactly the "worker
/// unavailable" case `beginLiveSession` must fall back from.
@MainActor
@Suite("PythonIntelligenceCoordinator fallback selection")
struct PythonIntelligenceCoordinatorTests {
    private func makeSession() -> Session {
        var session = Session.makeTestSession(title: "Fallback Test")
        session.id = UUID()
        return session
    }

    @Test("bridge stderr diagnostics retain only metadata, never payload text")
    func stderrDiagnosticsArePayloadFree() {
        let sensitiveLine = "transcript=confidential answer; document=private notes"
        let diagnostic = PythonWorkerManager.stderrDiagnostic(for: sensitiveLine)

        #expect(diagnostic == "worker stderr bytes=\(sensitiveLine.utf8.count)")
        #expect(!diagnostic.contains("confidential"))
        #expect(!diagnostic.contains("private notes"))
    }

    @Test("worker never started (stopped): falls back to the Swift demo pipeline")
    func fallsBackWhenWorkerStopped() async {
        let workerManager = PythonWorkerManager(configuration: .resolveDefault())
        let coordinator = PythonIntelligenceCoordinator(workerManager: workerManager, eventRouter: IPCEventRouter())
        let session = makeSession()
        let state = ConversationState(sessionID: session.id, repository: ConversationRepository(db: .makeInMemory()))

        #expect(workerManager.state == .stopped)

        await coordinator.beginLiveSession(state: state, session: session)

        #expect(coordinator.drivingSource == .swiftFallback)
        #expect(state.phase == .live)
    }

    /// Real end-to-end wiring: resolves the real dev-relative
    /// `whisper.cpp/build/bin/whisper-cli` this checkout actually has
    /// built, downloads+verifies a manifest through a fixture `URLProtocol`
    /// serving genuine bytes with their real, freshly-computed SHA-256
    /// (never the real ~75MB production model in a unit test — that would
    /// make an ordinary `swift test` run depend on network access), and
    /// asserts the resolved paths land on `workerManager.configuration` —
    /// the same mutation `PythonWorkerManager.launchProcessAndWaitForReady`
    /// reads to set `VEYA_WHISPER_BIN`/`VEYA_WHISPER_MODEL` for the real
    /// worker process.
    @Test("prepareRealTranscriptionAssets resolves a real local binary and downloads+verifies the configured model")
    func prepareRealTranscriptionAssetsResolvesRealAssets() async throws {
        guard PythonWorkerConfiguration.resolveWhisperBinary() != nil else {
            // No local whisper.cpp build in this environment — the method
            // must still be a safe no-op rather than throwing or hanging.
            let workerManager = PythonWorkerManager(configuration: .resolveDefault())
            let coordinator = PythonIntelligenceCoordinator(workerManager: workerManager, eventRouter: IPCEventRouter())
            await coordinator.prepareRealTranscriptionAssets()
            #expect(workerManager.configuration.whisperModelPathURL == nil)
            return
        }

        let payload = Data("fixture whisper model weights".utf8)
        let modelURL = URL(string: "https://fixture.test/prepare-real-transcription-assets-model.bin")!
        FixtureURLProtocol.responses[modelURL] = payload
        defer { FixtureURLProtocol.responses.removeValue(forKey: modelURL) }
        let entry = WhisperModelManifestEntry(id: "fixture-model", url: modelURL, sha256: SHA256.hash(data: payload).map { String(format: "%02x", $0) }.joined(), architecture: "universal", sizeBytes: Int64(payload.count), version: "1")

        let manifestDirectory = FileManager.default.temporaryDirectory.appendingPathComponent("veya-test-manifest-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: manifestDirectory, withIntermediateDirectories: true)
        let manifestPath = manifestDirectory.appendingPathComponent("manifest.json")
        try JSONEncoder().encode(entry).write(to: manifestPath)
        setenv("VEYA_WHISPER_MODEL_MANIFEST_PATH", manifestPath.path, 1)
        defer { unsetenv("VEYA_WHISPER_MODEL_MANIFEST_PATH") }

        let sessionConfiguration = URLSessionConfiguration.ephemeral
        sessionConfiguration.protocolClasses = [FixtureURLProtocol.self]
        let cacheDirectory = FileManager.default.temporaryDirectory.appendingPathComponent("veya-test-whisper-model-\(UUID().uuidString)", isDirectory: true)
        let modelManager = WhisperModelManager(session: URLSession(configuration: sessionConfiguration), cacheDirectory: cacheDirectory)
        let workerManager = PythonWorkerManager(configuration: .resolveDefault())
        let coordinator = PythonIntelligenceCoordinator(workerManager: workerManager, eventRouter: IPCEventRouter(), whisperModelManager: modelManager)

        await coordinator.prepareRealTranscriptionAssets()

        #expect(workerManager.configuration.whisperBinaryPathURL != nil)
        #expect(workerManager.configuration.whisperModelPathURL != nil)
        if let modelPath = workerManager.configuration.whisperModelPathURL {
            #expect(try Data(contentsOf: modelPath) == payload)
        }
    }

    @Test("ending a session that used the Swift fallback is a harmless no-op")
    func endingFallbackSessionIsNoOp() async {
        let workerManager = PythonWorkerManager(configuration: .resolveDefault())
        let coordinator = PythonIntelligenceCoordinator(workerManager: workerManager, eventRouter: IPCEventRouter())
        let session = makeSession()
        let state = ConversationState(sessionID: session.id, repository: ConversationRepository(db: .makeInMemory()))

        await coordinator.beginLiveSession(state: state, session: session)
        #expect(coordinator.drivingSource == .swiftFallback)

        await coordinator.endLiveSession(sessionID: session.id)

        #expect(coordinator.drivingSource == .none)
    }

    @Test("driving source starts as .none before any session begins")
    func initialDrivingSourceIsNone() {
        let coordinator = PythonIntelligenceCoordinator(
            workerManager: PythonWorkerManager(configuration: .resolveDefault()),
            eventRouter: IPCEventRouter()
        )
        #expect(coordinator.drivingSource == .none)
    }

    @Test("a configured but never-authorized microphone never blocks the Swift fallback when the worker is unavailable")
    func workerUnavailableFallsBackEvenWithAudioCaptureConfigured() async {
        let audioCapture = FakeAudioCapture()
        let permission = FakeMicrophonePermission(status: .authorized)
        let workerManager = PythonWorkerManager(configuration: .resolveDefault())
        let coordinator = PythonIntelligenceCoordinator(
            workerManager: workerManager,
            eventRouter: IPCEventRouter(),
            audioCapture: audioCapture,
            microphonePermission: permission
        )
        let session = makeSession()
        let state = ConversationState(sessionID: session.id, repository: ConversationRepository(db: .makeInMemory()))

        await coordinator.beginLiveSession(state: state, session: session)

        #expect(coordinator.drivingSource == .swiftFallback)
        // Worker readiness is checked first — real transcription must
        // never be attempted (no permission prompt, no audio capture
        // start) when the worker itself isn't available.
        #expect(audioCapture.startCallCount == 0)
        #expect(permission.requestAccessCallCount == 0)
    }

    @Test("the live session indicator reads 'starting…' before any session begins")
    func indicatorTextBeforeAnySession() {
        let coordinator = PythonIntelligenceCoordinator(
            workerManager: PythonWorkerManager(configuration: .resolveDefault()),
            eventRouter: IPCEventRouter()
        )
        #expect(coordinator.liveSessionIndicatorText == "Intelligence: starting…")
    }

    @Test("the live session indicator reads 'Demo mode — Swift fallback' once the Swift fallback is active")
    func indicatorTextForSwiftFallback() async {
        let coordinator = PythonIntelligenceCoordinator(
            workerManager: PythonWorkerManager(configuration: .resolveDefault()),
            eventRouter: IPCEventRouter()
        )
        let session = makeSession()
        let state = ConversationState(sessionID: session.id, repository: ConversationRepository(db: .makeInMemory()))

        await coordinator.beginLiveSession(state: state, session: session)

        #expect(coordinator.liveSessionIndicatorText == "Demo mode — Swift fallback")
    }

    private func makeDocument(sessionID: UUID, fileName: String = "notes.txt") -> SessionDocument {
        SessionDocument(
            id: UUID(),
            sessionID: sessionID,
            fileName: fileName,
            fileExtension: "txt",
            storedPath: "/tmp/does-not-matter.txt",
            fileSizeBytes: 42,
            addedAt: Date()
        )
    }

    @Test("ingestDocuments with a worker that isn't ready marks each document failed, without throwing or blocking")
    func ingestDocumentsWithWorkerNotReadyMarksFailed() async throws {
        let coordinator = PythonIntelligenceCoordinator(
            workerManager: PythonWorkerManager(configuration: .resolveDefault()),
            eventRouter: IPCEventRouter()
        )
        let session = makeSession()
        let documents = [makeDocument(sessionID: session.id, fileName: "a.txt"), makeDocument(sessionID: session.id, fileName: "b.txt")]

        // Fire-and-forget: returns immediately, doesn't await the RPCs.
        coordinator.ingestDocuments(session: session, documents: documents)

        for document in documents {
            try await waitUntil(timeout: 2) {
                coordinator.knowledgeIngestionTracker.status(forDocumentID: document.id) == .failed
            }
            #expect(coordinator.knowledgeIngestionTracker.status(forDocumentID: document.id) == .failed)
        }
    }

    @Test("ingestDocuments with an empty document list does nothing")
    func ingestDocumentsWithEmptyListDoesNothing() {
        let coordinator = PythonIntelligenceCoordinator(
            workerManager: PythonWorkerManager(configuration: .resolveDefault()),
            eventRouter: IPCEventRouter()
        )
        let session = makeSession()

        coordinator.ingestDocuments(session: session, documents: [])  // must not crash

        #expect(coordinator.knowledgeIngestionTracker.statusByDocumentID.isEmpty)
    }

    @Test("the coordinator's knowledgeIngestionTracker is the same instance its event router updates")
    func knowledgeIngestionTrackerIsSharedWithTheEventRouter() async throws {
        let eventRouter = IPCEventRouter()
        let coordinator = PythonIntelligenceCoordinator(
            workerManager: PythonWorkerManager(configuration: .resolveDefault()),
            eventRouter: eventRouter
        )

        #expect(coordinator.knowledgeIngestionTracker === eventRouter.knowledgeTracker)
    }
}
