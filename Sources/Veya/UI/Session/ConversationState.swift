import Foundation

/// The state machine behind Live Session. Two independent sources can
/// drive it — `MockTranscriptSource` (the Swift fallback, used when the
/// Python worker is unavailable) and `IPCEventRouter` (the Python-driven
/// pipeline, the normal path — see Section 6's `Sources/Veya/Bridge/`).
///
/// The two sources use different entry points on purpose:
/// - `start(source:)`/`ingest(_:)` run the *full* canned pipeline
///   (transcript → canned question detection → canned answer), since the
///   Swift fallback has no other source of questions/answers.
/// - `ingestTranscriptSegment(_:)`/`ingestDetectedQuestion(_:)`/
///   `ingestAnswer(_:)` are granular and do *not* run any detection —
///   Python already decided what's a question and what the answer is, so
///   `IPCEventRouter` calls these directly. Duplicating detection here
///   for the Python path would mean two disagreeing mock intelligences
///   running at once.
@MainActor
final class ConversationState: ObservableObject {
    enum Phase: Equatable {
        case idle
        case live
        case ended
    }

    @Published private(set) var phase: Phase = .idle
    @Published private(set) var segments: [TranscriptSegment] = []
    @Published private(set) var detectedQuestions: [DetectedQuestion] = []
    @Published private(set) var currentAnswer: CopilotAnswer?

    /// Transient (never persisted) partial transcript text — cleared as
    /// soon as the corresponding final segment arrives.
    @Published private(set) var partialTranscriptText: String?
    /// True between `question.detected` and `answer.started` — Python has
    /// detected a question but hasn't yet started streaming an answer for
    /// it (or never will, if answer intelligence isn't available). Only
    /// ever set by the Python-driven pipeline (`IPCEventRouter`); the
    /// Swift fallback's canned pipeline has no equivalent "analyzing" gap.
    @Published private(set) var isAnalyzingQuestion = false
    /// True between `answer.started` and `answer.completed`.
    @Published private(set) var isGeneratingAnswer = false
    /// Transient (never persisted) partial answer text from `answer.delta`.
    @Published private(set) var partialAnswerText: String?

    /// Section 14: the raw local-VAD-derived turn state — never claims
    /// AI understanding, only what the audio itself looks like right now.
    enum TurnState: String, Equatable, Sendable {
        case listening
        case speech
        case waitingForSilence = "waiting_for_silence"
    }
    @Published private(set) var turnState: TurnState = .listening
    /// True between `question.classifying` and either `question.detected`
    /// or `question.rejected` — the finalized turn is ambiguous enough
    /// that the (slower) semantic classification stage is running. Only
    /// ever set by the Python-driven pipeline; the Swift fallback's
    /// canned pipeline has no equivalent classification step.
    @Published private(set) var isClassifyingQuestion = false

    let sessionID: UUID
    private let repository: ConversationRepository
    private var transcriptTask: Task<Void, Never>?

    init(sessionID: UUID, repository: ConversationRepository = ConversationRepository()) {
        self.sessionID = sessionID
        self.repository = repository
    }

    // MARK: - Swift fallback pipeline

    func start(source: MockTranscriptSource = MockTranscriptSource()) {
        guard phase != .live else { return }
        phase = .live
        transcriptTask = Task { [weak self] in
            guard let self else { return }
            for await segment in source.segments(sessionID: sessionID) {
                if Task.isCancelled { break }
                await self.ingest(segment)
            }
        }
    }

    /// The Swift-fallback pipeline's single entry point: append the
    /// segment, run canned question detection, and if a question was
    /// detected, run the canned answer generator. Exposed (not private)
    /// so unit tests can drive the state machine directly without a real
    /// timer. Not used by the Python-driven pipeline — see
    /// `ingestTranscriptSegment(_:)` for that.
    func ingest(_ segment: TranscriptSegment) async {
        segments.append(segment)
        partialTranscriptText = nil
        try? await repository.save(segment)

        guard segment.isFinal, let question = Self.detectQuestion(in: segment, sessionID: sessionID) else {
            return
        }
        detectedQuestions.append(question)
        try? await repository.save(question)

        let answer = MockAnswerGenerator.answer(for: question, sessionID: sessionID)
        currentAnswer = answer
        try? await repository.save(answer)
    }

    /// Canned keyword/punctuation match — a placeholder for the real
    /// `Intelligence` question-detection subsystem. Only used by the
    /// Swift fallback pipeline.
    static func detectQuestion(in segment: TranscriptSegment, sessionID: UUID) -> DetectedQuestion? {
        let text = segment.text
        let lowercased = text.lowercased()
        let questionPrefixes = ["why", "how", "what", "when", "who", "so why", "so how"]
        let looksLikeQuestion = text.contains("?") || questionPrefixes.contains { lowercased.hasPrefix($0) }
        guard looksLikeQuestion else { return nil }
        return DetectedQuestion(id: UUID(), sessionID: sessionID, text: text, detectedAt: Date())
    }

    /// Switches a Python-driven session over to the Swift fallback timer —
    /// used when the Python worker becomes unavailable while
    /// `PythonIntelligenceCoordinator` has a session claimed (see its
    /// `handleWorkerStateChange(_:)`). Accepts both `.idle` (the worker
    /// crashed during `tryBeginRealTranscription`'s own startup sequence,
    /// *before* `beginPythonDrivenSession()` ever ran) and `.live` (the
    /// ordinary mid-session crash case) — only guards against `.ended`
    /// and against starting a second fallback timer.
    func switchToSwiftFallback(source: MockTranscriptSource = MockTranscriptSource()) {
        guard phase != .ended, transcriptTask == nil else { return }
        phase = .live
        transcriptTask = Task { [weak self] in
            guard let self else { return }
            for await segment in source.segments(sessionID: sessionID) {
                if Task.isCancelled { break }
                await self.ingest(segment)
            }
        }
    }

    // MARK: - Python-driven pipeline

    /// Marks the session live without starting the Swift timer/canned
    /// pipeline — used when `IPCEventRouter` is about to start driving
    /// this state from worker events instead.
    func beginPythonDrivenSession() {
        guard phase != .live else { return }
        phase = .live
    }

    func setPartialTranscript(_ text: String?) {
        partialTranscriptText = text
    }

    func ingestTranscriptSegment(_ segment: TranscriptSegment) async {
        segments.append(segment)
        partialTranscriptText = nil
        try? await repository.save(segment)
    }

    func setTurnState(_ newState: TurnState) {
        turnState = newState
    }

    func setClassifyingQuestion(_ classifying: Bool) {
        isClassifyingQuestion = classifying
    }

    func ingestDetectedQuestion(_ question: DetectedQuestion) async {
        detectedQuestions.append(question)
        isAnalyzingQuestion = true
        isClassifyingQuestion = false
        try? await repository.save(question)
    }

    func setAnswerGenerating(_ generating: Bool) {
        isGeneratingAnswer = generating
        if generating {
            isAnalyzingQuestion = false
            partialAnswerText = nil
        }
    }

    /// Clears any in-progress "analyzing"/"generating" UI state without
    /// touching persisted data — called when a Live Session ends or the
    /// worker becomes unavailable mid-session, since a cancelled/abandoned
    /// answer generation will never send its own `answer.completed` to
    /// clear this naturally.
    func cancelPendingAnswerActivity() {
        isAnalyzingQuestion = false
        isGeneratingAnswer = false
        isClassifyingQuestion = false
        partialAnswerText = nil
    }

    func setPartialAnswer(_ text: String?) {
        partialAnswerText = text
    }

    func ingestAnswer(_ answer: CopilotAnswer) async {
        currentAnswer = answer
        isGeneratingAnswer = false
        partialAnswerText = nil
        try? await repository.save(answer)
    }

    // MARK: - Shared

    func end() {
        transcriptTask?.cancel()
        transcriptTask = nil
        phase = .ended
    }
}
