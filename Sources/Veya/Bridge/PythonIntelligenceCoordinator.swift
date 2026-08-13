import Foundation

/// Which pipeline is actually driving the current Live Session's
/// `ConversationState`. Shown to the user (see the fallback indicator in
/// `LiveSessionView`) so the app never silently claims Python-backed mock
/// intelligence — or real transcription — is active when it isn't.
enum ConversationDrivingSource: Equatable, Sendable {
    case none
    case realTranscription
    case pythonWorker
    case swiftFallback
}

/// Decides, for each Live Session, which of three pipelines drives
/// `ConversationState`, in priority order:
///
/// 1. **Real transcription** — worker `.ready`, a microphone `AudioCapturing`
///    is configured, permission is authorized, and the Python worker's
///    `transcription.start` handshake (which itself checks local Whisper
///    availability) succeeds.
/// 2. **Python mock feed** — worker `.ready`, but real transcription isn't
///    available (no `audioCapture` configured, permission denied, or
///    Python reports its Whisper setup is unavailable).
/// 3. **Swift fallback** — the worker itself is unavailable or the
///    `session.start` handshake fails.
///
/// Owns each decision's consequences (starting/stopping the worker's
/// per-session RPCs, microphone capture, health checking, and event
/// routing) — `AppCoordinator` calls this instead of talking to
/// `PythonWorkerManager`/`IPCEventRouter`/`AudioCapturing` directly, which
/// is also what keeps AVFoundation details out of SwiftUI views.
@MainActor
final class PythonIntelligenceCoordinator: ObservableObject {
    @Published private(set) var drivingSource: ConversationDrivingSource = .none
    @Published private(set) var microphoneAuthorizationState: MicrophoneAuthorizationState = .undetermined
    /// Non-nil only when Python most recently reported real transcription
    /// as unavailable (missing/misconfigured Whisper binary or model).
    /// Cleared as soon as real transcription successfully starts.
    @Published private(set) var transcriptionSetupError: String?
    /// Whether Python's local Ollama check succeeded for the current
    /// real-transcription session. Only meaningful while
    /// `drivingSource == .realTranscription` — real transcripts still
    /// flow either way (see the type doc comment's fallback order);
    /// this only affects whether questions get answered.
    @Published private(set) var answerIntelligenceAvailable = false
    /// The mixed/microphone-only mode "I'm answering" hold-to-talk/toggle
    /// state (Section 16) — `true` while the user's own mixed-track
    /// speech should be treated as authoritative answer context instead
    /// of a candidate/draft trigger. Always `false` in separated-track
    /// mode, where the microphone track is already reliably known to be
    /// the user regardless of this flag.
    @Published private(set) var isUserSpeaking = false

    let workerManager: PythonWorkerManager
    private let eventRouter: IPCEventRouter
    private let audioCapture: AudioCapturing?
    private let microphonePermission: MicrophonePermissionChecking
    private let whisperModelManager: WhisperModelManager
    private let localAIPreferencesStore: LocalAIPreferencesStore
    /// Published so the first-launch download (Section 13's packaging
    /// build prompt) can show real progress instead of the app silently
    /// sitting on "Listening" with no explanation for however long a
    /// ~75MB download takes.
    @Published private(set) var whisperModelDownloadState: WhisperModelDownloadState = .idle

    /// Same instance `eventRouter` updates from `knowledge.ingestion_*`
    /// events — exposed here so SwiftUI only ever needs to reach through
    /// this one coordinator, never `IPCEventRouter` directly.
    var knowledgeIngestionTracker: KnowledgeIngestionTracker { eventRouter.knowledgeTracker }

    private var activeSessionID: UUID?
    /// The `ConversationState` for the currently active session (Python
    /// mock or real-transcription driven), kept only so
    /// `handleWorkerStateChange(_:)` can switch it to the Swift fallback
    /// if the worker dies mid-session. Cleared whenever `activeSessionID`
    /// is cleared.
    private weak var activeConversationState: ConversationState?
    private var audioChunkSender: AudioChunkSender?
    private var audioForwardingTask: Task<Void, Never>?

    // Section 16: the separated meeting/system-audio track — always in
    // addition to (never instead of) the microphone track above. `nil`
    // `meetingAudioCapture` (the default) means this coordinator was
    // never given a system-audio capability at all; every meeting-audio
    // method below is then a harmless, honest no-op.
    private let meetingAudioCapture: AudioCapturing?
    @Published private(set) var meetingAudioActive = false
    private var meetingAudioChunkSender: AudioChunkSender?
    private var meetingAudioForwardingTask: Task<Void, Never>?

    init(
        workerManager: PythonWorkerManager = PythonWorkerManager(),
        eventRouter: IPCEventRouter = IPCEventRouter(),
        audioCapture: AudioCapturing? = nil,
        meetingAudioCapture: AudioCapturing? = nil,
        microphonePermission: MicrophonePermissionChecking = AVFoundationMicrophonePermission(),
        whisperModelManager: WhisperModelManager = WhisperModelManager(),
        localAIPreferencesStore: LocalAIPreferencesStore = LocalAIPreferencesStore()
    ) {
        self.workerManager = workerManager
        self.eventRouter = eventRouter
        self.audioCapture = audioCapture
        self.meetingAudioCapture = meetingAudioCapture
        self.microphonePermission = microphonePermission
        self.whisperModelManager = whisperModelManager
        self.localAIPreferencesStore = localAIPreferencesStore
        self.microphoneAuthorizationState = microphonePermission.currentStatus
        workerManager.eventHandler = { [weak eventRouter] event in
            await eventRouter?.route(event)
        }
        workerManager.stateChangeHandler = { [weak self] newState in
            self?.handleWorkerStateChange(newState)
        }
    }

    /// Launches the worker in the background so it's likely `.ready` by
    /// the time the user starts a session. Safe to call once at app
    /// launch; failures leave the worker `.failed` and every subsequent
    /// Live Session silently uses the Swift fallback instead — the app
    /// never blocks on this. Resolves/downloads the local Whisper model
    /// first (see `prepareRealTranscriptionAssets()`) so the *first*
    /// worker launch already has real transcription configured whenever
    /// possible, not only after a later restart.
    func launchWorkerInBackground() {
        let storedModel = localAIPreferencesStore.load().ollamaModel
        if !storedModel.isEmpty {
            workerManager.configuration.ollamaModelOverride = storedModel
        }
        Task {
            await prepareRealTranscriptionAssets()
            await workerManager.start()
        }
    }

    /// Real, un-mocked Ollama diagnostics for the Local AI status panel
    /// (Settings) — reachability, the currently configured model, whether
    /// that exact model is actually installed, and what is. Never throws;
    /// a worker that isn't `.ready` yet reports fully unreachable rather
    /// than blocking/erroring.
    func fetchLLMStatus() async -> LLMStatusResult {
        guard workerManager.state == .ready || workerManager.state == .unhealthy else {
            return LLMStatusResult(reachable: false, baseUrl: "", configuredModel: "", modelInstalled: false, availableModels: [], error: "worker_not_ready")
        }
        do {
            return try await workerManager.call(method: "system.llm_status", params: EmptyIPCParams())
        } catch {
            return LLMStatusResult(reachable: false, baseUrl: "", configuredModel: "", modelInstalled: false, availableModels: [], error: String(reflecting: type(of: error)))
        }
    }

    /// Persists `model` as the user's chosen Ollama model (Settings →
    /// Local AI) and restarts the worker so it takes effect immediately,
    /// rather than only on the next app launch. An empty string clears
    /// the override, reverting to the worker's own default.
    func setOllamaModelOverride(_ model: String) async {
        localAIPreferencesStore.save(LocalAIPreferences(ollamaModel: model))
        workerManager.configuration.ollamaModelOverride = model.isEmpty ? nil : model
        await workerManager.stop()
        await workerManager.start()
    }

    /// Resolves a local `whisper-cli` binary and ensures the manifest's
    /// Whisper model is downloaded/cached (Section 13 packaging build
    /// prompt's "first-launch model manager"), then configures
    /// `workerManager` to launch with them. A no-op, not a failure, when
    /// either is unavailable (no bundled/dev-relative binary found, no
    /// manifest configured, or the download/verification fails) — real
    /// transcription then simply stays unavailable and every Live Session
    /// falls back to the Python mock feed, exactly as before this method
    /// existed.
    func prepareRealTranscriptionAssets() async {
        guard let binaryURL = PythonWorkerConfiguration.resolveWhisperBinary() else { return }
        guard let manifestEntry = WhisperModelManifest.recommended() else { return }

        let modelURL = await whisperModelManager.ensureModelAvailable(for: manifestEntry)
        whisperModelDownloadState = whisperModelManager.state
        guard let modelURL else { return }

        workerManager.configuration.whisperBinaryPathURL = binaryURL
        workerManager.configuration.whisperModelPathURL = modelURL
    }

    /// Requests ingestion for each of `documents` — called after a session
    /// is created or documents are attached (see `CreateSessionView`).
    /// Fire-and-forget per document: a failure here (worker not ready, RPC
    /// error) only updates that document's tracked ingestion status — it
    /// never deletes the already-copied document or blocks session
    /// creation/start. See `docs/KNOWLEDGE_RETRIEVAL.md`.
    func ingestDocuments(session: Session, documents: [SessionDocument]) {
        for document in documents {
            Task {
                guard workerManager.state == .ready || workerManager.state == .unhealthy else {
                    knowledgeIngestionTracker.setStatus(
                        .failed, forDocumentID: document.id, reason: "The local worker isn't running."
                    )
                    return
                }
                do {
                    let _: OkResult = try await workerManager.call(
                        method: "knowledge.ingest",
                        params: KnowledgeIngestParams(
                            sessionId: session.id.uuidString,
                            documentId: document.id.uuidString,
                            fileName: document.fileName,
                            fileExtension: document.fileExtension,
                            filePath: document.storedPath
                        )
                    )
                } catch {
                    knowledgeIngestionTracker.setStatus(
                        .failed, forDocumentID: document.id, reason: "Document ingestion failed (\(String(reflecting: type(of: error))))."
                    )
                }
            }
        }
    }

    // MARK: - Local coding and architecture workbench

    func loadCodeFiles(sessionID: UUID) async throws -> [CodeFileResult] {
        let result: CodingFilesResult = try await workerManager.call(method: "coding.list_files", params: SessionIdentifierParams(sessionId: sessionID.uuidString))
        return result.files
    }

    func saveCodeFile(sessionID: UUID, name: String, language: String, content: String, baseVersion: Int?) async throws -> CodeFileResult {
        try await workerManager.call(method: "coding.upsert_file", params: CodingUpsertFileParams(sessionId: sessionID.uuidString, name: name, language: language, content: content, baseVersion: baseVersion))
    }

    func analyzeCodeFile(sessionID: UUID, name: String) async throws -> CodingAnalysisResult {
        try await workerManager.call(method: "coding.analyze", params: CodingFileParams(sessionId: sessionID.uuidString, name: name))
    }

    func runCodeFile(sessionID: UUID, name: String) async throws -> CodingRunResult {
        try await workerManager.call(method: "coding.run", params: CodingFileParams(sessionId: sessionID.uuidString, name: name))
    }

    func codingAssist(sessionID: UUID, name: String, operation: String, request: String) async throws -> CodingProposalResult {
        try await workerManager.call(method: "coding.\(operation)", params: CodingAssistParams(sessionId: sessionID.uuidString, name: name, request: request))
    }

    func applyCodeEdits(sessionID: UUID, name: String, baseVersion: Int, edits: [CodingEdit]) async throws -> CodeFileResult {
        try await workerManager.call(method: "coding.apply_edits", params: CodingApplyEditsParams(sessionId: sessionID.uuidString, name: name, baseVersion: baseVersion, edits: edits))
    }

    func deleteCodeFile(sessionID: UUID, name: String) async throws {
        let _: OkResult = try await workerManager.call(method: "coding.delete_file", params: CodingFileParams(sessionId: sessionID.uuidString, name: name))
    }

    func renameCodeFile(sessionID: UUID, name: String, newName: String) async throws -> CodeFileResult {
        try await workerManager.call(method: "coding.rename_file", params: CodingRenameFileParams(sessionId: sessionID.uuidString, name: name, newName: newName))
    }

    func loadArchitecture(sessionID: UUID) async throws -> ArchitectureStateResult {
        try await workerManager.call(method: "design.get", params: SessionIdentifierParams(sessionId: sessionID.uuidString))
    }

    func saveArchitecture(sessionID: UUID, state: ArchitectureStateResult) async throws -> ArchitectureStateResult {
        try await workerManager.call(method: "design.replace", params: ArchitectureReplaceParams(sessionId: sessionID.uuidString, baseVersion: state.version, title: state.title, nodes: state.nodes, edges: state.edges, decisions: state.decisions, assumptions: state.assumptions, requirements: state.requirements, risks: state.risks, tradeOffs: state.tradeOffs, actionItems: state.actionItems))
    }

    func designFollowup(sessionID: UUID, request: String) async throws -> ArchitectureStateResult {
        try await workerManager.call(method: "design.followup", params: ArchitectureFollowupParams(sessionId: sessionID.uuidString, request: request))
    }

    func exportArchitecture(sessionID: UUID, format: String) async throws -> ArchitectureExportResult {
        try await workerManager.call(method: "design.export", params: ArchitectureExportParams(sessionId: sessionID.uuidString, format: format))
    }

    // MARK: - Session reports and memory (Section 13)

    func analyzeSession(sessionID: UUID, transcript: [TranscriptSegmentPayload], questions: [DetectedQuestionPayload], answers: [AnswerPayload]) async throws -> SessionReportResult {
        try await workerManager.call(method: "session.analyze", params: SessionAnalyzeParams(sessionId: sessionID.uuidString, transcript: transcript, questions: questions, answers: answers))
    }

    func fetchSessionReport(sessionID: UUID) async throws -> SessionReportResult {
        try await workerManager.call(method: "session.report.get", params: SessionIdentifierParams(sessionId: sessionID.uuidString))
    }

    func listMemories(status: String? = nil) async throws -> [MemoryRecordResult] {
        let result: MemoryListResult = try await workerManager.call(method: "memory.list", params: MemoryListParams(status: status))
        return result.memories
    }

    func approveMemory(id: String) async throws -> MemoryRecordResult {
        try await workerManager.call(method: "memory.approve", params: MemoryIdentifierParams(memoryId: id))
    }

    func rejectMemory(id: String) async throws {
        let _: OkResult = try await workerManager.call(method: "memory.reject", params: MemoryIdentifierParams(memoryId: id))
    }

    func updateMemory(id: String, text: String) async throws -> MemoryRecordResult {
        try await workerManager.call(method: "memory.update", params: MemoryUpdateParams(memoryId: id, text: text))
    }

    func deleteMemory(id: String) async throws {
        let _: OkResult = try await workerManager.call(method: "memory.delete", params: MemoryIdentifierParams(memoryId: id))
    }

    /// Call when a Live Session starts. See the type doc comment for the
    /// three-way selection order.
    func beginLiveSession(state: ConversationState, session: Session) async {
        guard workerManager.state == .ready else {
            beginSwiftFallback(state: state)
            return
        }

        // Attach the router *before* any RPC that could cause the worker
        // to start emitting events (`mock.start_live_feed`/
        // `transcription.start`, and — in case a future handler emits
        // eagerly — `session.start` too). A fast worker can otherwise
        // emit `session.started`/early transcript events before Swift
        // gets around to attaching, and `IPCEventRouter.route(_:)`
        // silently drops events while unattached — so attaching first
        // makes that structurally impossible rather than merely unlikely.
        activeSessionID = session.id
        activeConversationState = state
        eventRouter.attach(state: state, sessionID: session.id)

        do {
            let _: OkResult = try await workerManager.call(
                method: "session.start",
                params: SessionStartParams(
                    sessionId: session.id.uuidString,
                    title: session.title,
                    sessionType: session.sessionType.rawValue,
                    company: session.company,
                    roleOrTopic: session.roleOrTopic,
                    sessionDescription: session.sessionDescription,
                    notes: session.notes,
                    preferredAnswerStyle: session.preferredAnswerStyle.rawValue,
                    preferredProgrammingLanguage: session.preferredProgrammingLanguage,
                    customInstructions: session.customInstructions
                )
            )
        } catch {
            // A worker crash while this very first RPC was in flight is
            // already fully handled by `handleWorkerStateChange` (see its
            // doc comment) by the time this `await` resumes — recognizable
            // here by `activeSessionID` no longer matching this session
            // (cleared, or reassigned to a newer one). Only run this
            // fallback for a genuine `session.start` failure that
            // `handleWorkerStateChange` hasn't already reacted to.
            guard activeSessionID == session.id else { return }
            BridgeLog.error("Python-driven session start failed, errorType=\(String(reflecting: type(of: error)))")
            eventRouter.detach()
            activeSessionID = nil
            activeConversationState = nil
            beginSwiftFallback(state: state)
            return
        }

        if await tryBeginRealTranscription(state: state, session: session) {
            return
        }

        do {
            let _: OkResult = try await workerManager.call(
                method: "mock.start_live_feed",
                params: SessionIdentifierParams(sessionId: session.id.uuidString)
            )
            // `handleWorkerStateChange` runs synchronously to completion
            // (no `await` inside it) once triggered, so if the worker
            // crashed while this RPC was in flight, that handler has
            // already fully run — including clearing `activeSessionID` —
            // by the time this `await` resumes. Without this check we'd
            // clobber its recovery by unconditionally committing to
            // `.pythonWorker` here anyway.
            guard activeSessionID == session.id else {
                let _: OkResult? = try? await workerManager.call(
                    method: "mock.stop_live_feed",
                    params: SessionIdentifierParams(sessionId: session.id.uuidString)
                )
                return
            }
            state.beginPythonDrivenSession()
            workerManager.beginHealthChecking()
            drivingSource = .pythonWorker
            BridgeLog.info("live session driven by Python worker (mock feed)")
        } catch {
            BridgeLog.error("mock.start_live_feed failed, errorType=\(String(reflecting: type(of: error)))")
            eventRouter.detach()
            activeSessionID = nil
            activeConversationState = nil
            beginSwiftFallback(state: state)
        }
    }

    /// Attempts the real-transcription path. Returns `true` only if it
    /// actually started (`drivingSource == .realTranscription`); returns
    /// `false` for every "not available" reason (no `audioCapture`
    /// configured, permission not authorized, or Python's
    /// `transcription.start` failing) without throwing — those are all
    /// expected, non-error conditions the caller falls through to the
    /// Python mock feed from.
    private func tryBeginRealTranscription(state: ConversationState, session: Session) async -> Bool {
        guard let audioCapture else { return false }

        let authState = await microphonePermission.requestAccess()
        microphoneAuthorizationState = authState
        guard authState == .authorized else { return false }

        let startResult: TranscriptionStartResult
        do {
            startResult = try await workerManager.call(
                method: "transcription.start",
                params: TranscriptionStartParams(
                    sessionId: session.id.uuidString,
                    sampleRateHz: 16000,
                    channels: 1,
                    encoding: "pcm_s16le"
                )
            )
        } catch {
            transcriptionSetupError = String(reflecting: type(of: error))
            BridgeLog.error("transcription.start unavailable, errorType=\(String(reflecting: type(of: error)))")
            return false
        }

        do {
            try await audioCapture.start()
        } catch {
            // Python already has a transcription session open at this
            // point — best-effort tear it down before falling back so it
            // doesn't linger for the rest of the worker's lifetime.
            let _: OkResult? = try? await workerManager.call(
                method: "transcription.stop",
                params: SessionIdentifierParams(sessionId: session.id.uuidString)
            )
            transcriptionSetupError = String(reflecting: type(of: error))
            BridgeLog.error("microphone capture failed to start, errorType=\(String(reflecting: type(of: error)))")
            return false
        }

        // Same reasoning as the mock-feed path's identical check: a
        // worker crash landing while `audioCapture.start()` was in
        // flight (a real, potentially slow `AVAudioEngine` call) is
        // handled synchronously and completely by
        // `handleWorkerStateChange` before this `await` ever resumes —
        // committing to `.realTranscription` unconditionally here would
        // clobber that recovery right back to a claimed-but-dead session.
        guard activeSessionID == session.id else {
            await audioCapture.stop()
            let _: OkResult? = try? await workerManager.call(
                method: "transcription.stop",
                params: SessionIdentifierParams(sessionId: session.id.uuidString)
            )
            return false
        }

        transcriptionSetupError = nil
        answerIntelligenceAvailable = startResult.answerIntelligenceAvailable
        state.setASRProvider(startResult.asrProvider)
        let sender = AudioChunkSender(workerManager: workerManager, sessionId: session.id.uuidString)
        audioChunkSender = sender
        let stream = audioCapture.chunks()
        audioForwardingTask = Task {
            for await chunk in stream {
                await sender.send(chunk)
                let counts = await sender.counts()
                state.setAudioChunkCounts(sent: counts.sent, dropped: counts.dropped)
            }
        }

        state.beginPythonDrivenSession()
        workerManager.beginHealthChecking()
        drivingSource = .realTranscription
        BridgeLog.info("live session driven by real microphone transcription")
        return true
    }

    private func beginSwiftFallback(state: ConversationState) {
        drivingSource = .swiftFallback
        state.start()
    }

    /// Reacts to `PythonWorkerManager` state transitions that happen while
    /// a Python-driven (mock or real-transcription) session is active —
    /// including *during* `beginLiveSession`/`tryBeginRealTranscription`'s
    /// own async startup sequence, not just after `drivingSource` has been
    /// finalized to `.pythonWorker`/`.realTranscription`.
    ///
    /// Deliberately keyed on `activeSessionID`/`activeConversationState`
    /// alone, not on `drivingSource` — those two are set together, right
    /// before the very first Python-driven RPC of a session
    /// (`session.start`), and cleared together in every fallback/detach
    /// path, including the ones that run *before* `drivingSource` is ever
    /// set to `.pythonWorker`/`.realTranscription` (see
    /// `beginLiveSession`'s `catch` blocks). So `activeSessionID != nil`
    /// is true for the *entire* window a Python-driven session is
    /// claimed, including the real-transcription startup sequence
    /// (`transcription.start` → microphone start → `AudioChunkSender`
    /// setup) — a worker crash landing anywhere in that window, before
    /// `drivingSource` was ever set to `.realTranscription`, used to be
    /// silently ignored here (the old guard required `drivingSource` to
    /// already match), leaving the coordinator stuck with a claimed
    /// session and no active pipeline. Startup RPC failures are still
    /// separately handled by `beginLiveSession`'s own `catch` blocks;
    /// the two can race harmlessly (see the type's implementation notes)
    /// since every cleanup path here is idempotent.
    private func handleWorkerStateChange(_ newState: PythonWorkerState) {
        guard activeSessionID != nil, let state = activeConversationState else { return }

        switch newState {
        case .restarting, .failed, .stopped:
            BridgeLog.error("Python worker unavailable during active session, switching to Swift fallback")
            tearDownRealTranscriptionResources()
            state.cancelPendingAnswerActivity()
            workerManager.endHealthChecking()
            eventRouter.detach()
            activeSessionID = nil
            activeConversationState = nil
            drivingSource = .swiftFallback
            answerIntelligenceAvailable = false
            isUserSpeaking = false
            state.switchToSwiftFallback()
        case .starting, .ready, .unhealthy:
            break
        }
    }

    /// Call when a Live Session ends, regardless of which pipeline drove
    /// it — a no-op for sessions that used the Swift fallback.
    func endLiveSession(sessionID: UUID) async {
        let stateForCancellation = activeConversationState
        defer {
            eventRouter.detach()
            activeSessionID = nil
            activeConversationState = nil
            drivingSource = .none
            answerIntelligenceAvailable = false
            isUserSpeaking = false
        }

        guard activeSessionID == sessionID else { return }
        let source = drivingSource
        guard source == .pythonWorker || source == .realTranscription else { return }

        workerManager.endHealthChecking()
        let sessionIdString = sessionID.uuidString

        if source == .realTranscription {
            tearDownRealTranscriptionResources()
            // Best-effort — cancels any answer still generating for this
            // session before tearing it down, so Python's generation
            // task doesn't linger past the session it belonged to. Swift
            // clears its own partial-answer UI state directly rather than
            // waiting for an event that a cancelled generation will never
            // send (see `ConversationState.cancelPendingAnswerActivity()`).
            let _: OkResult? = try? await workerManager.call(
                method: "answer.cancel",
                params: SessionIdentifierParams(sessionId: sessionIdString)
            )
            stateForCancellation?.cancelPendingAnswerActivity()
            let _: OkResult? = try? await workerManager.call(
                method: "transcription.stop",
                params: SessionIdentifierParams(sessionId: sessionIdString)
            )
        } else {
            let _: OkResult? = try? await workerManager.call(
                method: "mock.stop_live_feed",
                params: SessionIdentifierParams(sessionId: sessionIdString)
            )
        }

        let _: OkResult? = try? await workerManager.call(
            method: "session.stop",
            params: SessionIdentifierParams(sessionId: sessionIdString)
        )
    }

    private func tearDownRealTranscriptionResources() {
        audioForwardingTask?.cancel()
        audioForwardingTask = nil
        audioChunkSender = nil
        Task { await audioCapture?.stop() }
        tearDownMeetingAudioResources()
    }

    private func tearDownMeetingAudioResources() {
        meetingAudioForwardingTask?.cancel()
        meetingAudioForwardingTask = nil
        meetingAudioChunkSender = nil
        meetingAudioActive = false
        Task { await meetingAudioCapture?.stop() }
    }

    // MARK: - Section 16: meeting/system-audio track

    /// Starts the separated meeting-audio track — only valid once real
    /// transcription (the microphone track) is already driving the
    /// session, since Python's `transcription.start_meeting_audio`
    /// shares that track's `ConversationOrchestrator`. Returns `false`
    /// (never throws) for every "not available" reason — no
    /// `meetingAudioCapture` configured, no active real-transcription
    /// session, screen-recording permission denied, the selected source
    /// disappeared, or the RPC itself failing — so callers can show an
    /// actionable "Meeting audio unavailable" status instead of a crash.
    @discardableResult
    func beginMeetingAudioCapture(source: SystemAudioSource? = nil) async -> Bool {
        guard let meetingAudioCapture, drivingSource == .realTranscription, let sessionID = activeSessionID else {
            return false
        }
        guard !meetingAudioActive else { return true }

        if let systemCapture = meetingAudioCapture as? SystemAudioCapture {
            systemCapture.selectedSource = source
        }

        let sessionIdString = sessionID.uuidString
        let result: MeetingAudioTranscriptionStartResult?
        do {
            result = try await workerManager.call(
                method: "transcription.start_meeting_audio",
                params: TranscriptionStartParams(sessionId: sessionIdString, sampleRateHz: 16000, channels: 1, encoding: "pcm_s16le")
            )
        } catch {
            BridgeLog.error("transcription.start_meeting_audio failed, errorType=\(String(reflecting: type(of: error)))")
            return false
        }
        guard result?.ok == true else { return false }

        do {
            try await meetingAudioCapture.start()
        } catch {
            let _: OkResult? = try? await workerManager.call(
                method: "transcription.stop_meeting_audio",
                params: SessionIdentifierParams(sessionId: sessionIdString)
            )
            BridgeLog.error("meeting-audio capture failed to start, errorType=\(String(reflecting: type(of: error)))")
            return false
        }

        let sender = AudioChunkSender(sessionId: sessionIdString) { [weak self] params in
            guard let self else { return }
            let _: OkResult = try await self.workerManager.call(method: "transcription.meeting_audio_chunk", params: params)
        }
        meetingAudioChunkSender = sender
        let stream = meetingAudioCapture.chunks()
        meetingAudioForwardingTask = Task {
            for await chunk in stream {
                await sender.send(chunk)
            }
        }
        meetingAudioActive = true
        BridgeLog.info("meeting-audio capture started")
        return true
    }

    /// Stops only the meeting-audio track — the microphone track and the
    /// rest of the session keep running. The one-click "stop meeting
    /// audio" control the product's consent/privacy requirements call for.
    func endMeetingAudioCapture() async {
        guard meetingAudioActive, let sessionID = activeSessionID else { return }
        tearDownMeetingAudioResources()
        let _: OkResult? = try? await workerManager.call(
            method: "transcription.stop_meeting_audio",
            params: SessionIdentifierParams(sessionId: sessionID.uuidString)
        )
    }

    /// The mixed/microphone-only mode "I'm answering" hold-to-talk/toggle
    /// fallback control — while active, the microphone's own speech is
    /// treated as authoritative user context rather than a draft trigger
    /// (Python-side; see `ConversationOrchestrator.set_user_speaking`).
    /// A harmless no-op if no real-transcription session is active.
    func setUserSpeaking(_ active: Bool) async {
        guard let sessionID = activeSessionID, drivingSource == .realTranscription else { return }
        isUserSpeaking = active
        let _: OkResult? = try? await workerManager.call(
            method: "conversation.set_user_speaking",
            params: SetUserSpeakingParams(sessionId: sessionID.uuidString, active: active)
        )
    }

    /// The "I'm answering" hotkey/button's action — flips the current
    /// state. A plain toggle (not true hold-to-talk, which would need a
    /// paired key-down/key-up hotkey pair `GlobalHotkeyManager` doesn't
    /// support) — press once when starting to answer, press again when
    /// finished.
    func toggleUserSpeaking() async {
        await setUserSpeaking(!isUserSpeaking)
    }

    /// The single, non-technical string `LiveSessionView` shows — the only
    /// place that maps internal state to user-facing copy, per the build
    /// prompt's exact wording.
    var liveSessionIndicatorText: String {
        switch drivingSource {
        case .realTranscription:
            if let state = activeConversationState {
                if state.isGeneratingAnswer { return "Generating answer…" }
                if state.isAnalyzingQuestion { return "Analyzing question…" }
            }
            if !answerIntelligenceAvailable {
                return "Listening — answer intelligence unavailable"
            }
            return "Listening — live transcription"
        case .pythonWorker:
            if microphoneAuthorizationState == .denied || microphoneAuthorizationState == .restricted {
                return "Microphone permission required"
            }
            if transcriptionSetupError != nil {
                return "Transcription setup unavailable"
            }
            return "Demo mode — Python mock intelligence"
        case .swiftFallback:
            return "Demo mode — Swift fallback"
        case .none:
            return "Intelligence: starting…"
        }
    }
}
