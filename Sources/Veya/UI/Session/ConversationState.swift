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
    /// Section 19: a running count of `transcript.rejected` events this
    /// session — noise/non-speech markers, low-quality ASR garbage, etc.
    /// Never carries the rejected text itself, only a safe count for a
    /// compact diagnostic ("N noise events filtered").
    @Published private(set) var rejectedTranscriptCount = 0

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

    /// One real, raw local-VAD measurement per audio chunk — only ever
    /// populated when Python's `VEYA_VAD_DIAGNOSTICS=1` is set (see
    /// `DeveloperDiagnosticsView`). Exists so real microphone behavior
    /// (was a chunk actually classified as speech, and why) can be
    /// verified directly, rather than inferred from whether an answer
    /// eventually appeared.
    struct VADDiagnosticSample: Identifiable, Sendable {
        let id = UUID()
        let rms: Double
        let threshold: Double
        let isInSpeech: Bool
        let speechSeconds: Double
        let silenceSeconds: Double
        let receivedAt: Date
    }
    @Published private(set) var vadDiagnostics: [VADDiagnosticSample] = []
    private static let maxRetainedVADDiagnostics = 200

    /// Section 17: one real latency record per answer round — only ever
    /// populated when the worker was launched with
    /// `VEYA_ANSWER_TIMING_DIAGNOSTICS=1`. Diagnostics-only, never shown
    /// in the normal interview UI.
    struct AnswerTimingSample: Identifiable, Sendable {
        let id = UUID()
        let stabilizedAt: Date
        let generationRequestStart: Date
        /// Diagnostics only — may correspond to hidden reasoning content,
        /// never rendered as an answer. Never shown as "first visible
        /// answer" — see `firstSpeakableCharAt`/`firstRenderedAt` for that.
        let firstRawTokenAt: Date?
        /// Section 18: the first character of clean, candidate-speakable
        /// prose on the Python side.
        let firstSpeakableCharAt: Date?
        /// Captured client-side, the moment Swift actually applied the
        /// first `answer.speakable_delta` to visible state — the real
        /// "first usable answer" moment as the user would experience it.
        let firstRenderedAt: Date?
        let completedAt: Date?
        var firstSpeakableLatencySeconds: Double? {
            guard let firstSpeakableCharAt else { return nil }
            return firstSpeakableCharAt.timeIntervalSince(stabilizedAt)
        }
        var firstRenderedLatencySeconds: Double? {
            guard let firstRenderedAt else { return nil }
            return firstRenderedAt.timeIntervalSince(stabilizedAt)
        }
        var totalLatencySeconds: Double? {
            guard let completedAt else { return nil }
            return completedAt.timeIntervalSince(stabilizedAt)
        }
    }
    @Published private(set) var answerTimingSamples: [AnswerTimingSample] = []
    private static let maxRetainedAnswerTimingSamples = 50
    /// The moment Swift actually applied the first speakable delta for a
    /// given answer-round sequence — captured client-side so
    /// `firstRenderedAt` reflects real render timing, not just when
    /// Python sent the event. Cleared once matched into a recorded
    /// `AnswerTimingSample` (or when it can never be, e.g. diagnostics
    /// were off) to avoid growing unbounded across a long session.
    private var pendingFirstRenderAt: [Int: Date] = [:]

    /// Called by `IPCEventRouter` alongside every `appendPartialAnswerDelta`/
    /// `appendDraftDelta` — records the real client-side render moment
    /// once per sequence, a no-op for every subsequent delta in that
    /// same round.
    func noteFirstRenderIfNeeded(sequence: Int) {
        if pendingFirstRenderAt[sequence] == nil {
            pendingFirstRenderAt[sequence] = Date()
        }
    }

    func recordAnswerTiming(_ sample: AnswerTimingSample) {
        answerTimingSamples.append(sample)
        if answerTimingSamples.count > Self.maxRetainedAnswerTimingSamples {
            answerTimingSamples.removeFirst(answerTimingSamples.count - Self.maxRetainedAnswerTimingSamples)
        }
    }

    /// Consumes (removes) the pending render timestamp for `sequence`,
    /// if one was recorded — used once, when the matching `answer.timing`
    /// event arrives.
    func takeFirstRenderTimestamp(sequence: Int) -> Date? {
        pendingFirstRenderAt.removeValue(forKey: sequence)
    }

    /// Section 15: the evolving "is this an answer request, and how sure
    /// are we" state, mirroring Python's `QuestionCandidateTracker`
    /// exactly — inferred here from which candidate/draft events have
    /// arrived, since Python doesn't send the state name directly.
    enum QuestionCandidateState: String, Equatable, Sendable {
        case idle, candidate, drafting, finalized, rejected
    }
    @Published private(set) var candidateState: QuestionCandidateState = .idle
    /// The still-evolving spoken text a candidate/draft was built from —
    /// distinct from `finalizedQuestionText`, which only ever reflects a
    /// turn that actually reached a real boundary.
    @Published private(set) var candidateQuestionText: String?
    @Published private(set) var finalizedQuestionText: String?
    /// True between `answer.draft_started`/`answer.draft_replaced` and
    /// that same sequence's `answer.completed`/`answer.cancelled` — a
    /// speculative draft may still be replaced or cancelled outright,
    /// unlike `isGeneratingAnswer`'s older, coarser signal.
    @Published private(set) var isDraftingAnswer = false
    /// True specifically when a *finalize*-triggered regeneration is
    /// superseding a still-visible draft (`answer.draft_replaced` arriving
    /// after `question.finalized`) — lets the UI say "Refining answer…"
    /// instead of the more generic "Drafting answer…".
    @Published private(set) var isRefiningAnswer = false
    @Published private(set) var draftAnswerText = ""
    /// The answer-round sequence the currently-visible draft belongs to —
    /// every draft/delta/replace/cancel event carries its own sequence,
    /// and anything whose sequence doesn't match this is stale and must
    /// never mutate visible state (a superseded draft's late events
    /// arriving after a replacement, for instance).
    @Published private(set) var draftSequence: Int?

    // MARK: - Section 17: answer failure / interviewer-turn queue

    /// Set when `answer.completed` arrives with `is_failed: true` —
    /// `currentAnswer` (the last real completed answer) is deliberately
    /// left untouched so it stays visible; the UI shows this as a
    /// separate, dismissable, retryable error instead of losing the
    /// prior answer. Cleared automatically once a new answer round
    /// actually starts or completes.
    @Published private(set) var lastAnswerFailureMessage: String?
    /// The question a failed generation was for — kept only so "Retry"
    /// can resend the exact same, already-classified question rather
    /// than requiring the interviewer to ask it again.
    @Published private(set) var lastFailedQuestionID: String?
    @Published private(set) var lastFailedQuestionText: String?
    /// How many finalized interviewer turns are currently waiting behind
    /// the one actively generating — 0 means nothing is queued.
    @Published private(set) var queuedQuestionsCount = 0
    /// Set when the bounded queue was already full and a newly finalized
    /// question could not be queued — an honest "this won't be answered"
    /// notice rather than a silent drop.
    @Published private(set) var queueOverflowMessage: String?
    /// Section 18: no clean speakable text has arrived yet after a
    /// bounded wait (`answer.slow_warning`) — the panel must keep
    /// showing the prior completed answer and offer Retry/Skip rather
    /// than a silent, indefinite spinner. Cleared as soon as real
    /// speakable text starts arriving or the round ends.
    @Published private(set) var isAnswerSlow = false
    /// The question_id/text of whatever answer round is currently active
    /// (drafting or generating) — lets the slow-warning banner's Retry
    /// control resend the exact same question rather than needing a
    /// separate, redundant tracking mechanism.
    @Published private(set) var activeGeneratingQuestionID: String?
    @Published private(set) var activeGeneratingQuestionText: String?

    // MARK: - Developer diagnostics (safe metadata only — see
    // `VADDiagnosticsView`; never transcript/prompt/answer text)

    /// `"streaming"` or `"degraded_batch"`, from `transcription.start`'s
    /// response — `nil` before a session has actually started.
    @Published private(set) var asrProvider: String?
    @Published private(set) var latestPartialReceivedAt: Date?
    @Published private(set) var latestFinalReceivedAt: Date?
    /// Counts every `question.candidate`/`question.updated` for the
    /// current turn — a rough "how many times did the hypothesis change"
    /// signal, reset whenever a fresh candidate begins from idle.
    @Published private(set) var candidateRevisionCount = 0
    enum DraftTransitionReason: String, Sendable {
        case started, replaced, cancelled
    }
    @Published private(set) var lastDraftTransitionReason: DraftTransitionReason?
    @Published private(set) var audioChunksSent = 0
    @Published private(set) var audioChunksDropped = 0

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
        if text != nil {
            latestPartialReceivedAt = Date()
        }
    }

    func setASRProvider(_ provider: String?) {
        asrProvider = provider
    }

    func setAudioChunkCounts(sent: Int, dropped: Int) {
        audioChunksSent = sent
        audioChunksDropped = dropped
    }

    func ingestTranscriptSegment(_ segment: TranscriptSegment) async {
        latestFinalReceivedAt = Date()
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

    func recordTranscriptRejection() {
        rejectedTranscriptCount += 1
    }

    func recordVADDiagnostic(_ sample: VADDiagnosticSample) {
        vadDiagnostics.append(sample)
        if vadDiagnostics.count > Self.maxRetainedVADDiagnostics {
            vadDiagnostics.removeFirst(vadDiagnostics.count - Self.maxRetainedVADDiagnostics)
        }
    }

    func ingestDetectedQuestion(_ question: DetectedQuestion) async {
        detectedQuestions.append(question)
        isAnalyzingQuestion = true
        isClassifyingQuestion = false
        try? await repository.save(question)
    }

    func setAnswerGenerating(_ generating: Bool, questionID: String? = nil, questionText: String? = nil) {
        isGeneratingAnswer = generating
        if generating {
            isAnalyzingQuestion = false
            partialAnswerText = nil
            lastAnswerFailureMessage = nil
            activeGeneratingQuestionID = questionID
            activeGeneratingQuestionText = questionText
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
        queuedQuestionsCount = 0
        resetCandidateTracking()
    }

    /// Section 18: appends a clean `answer.speakable_delta` chunk — the
    /// growing answer text, not a replacement of it. (Previously this
    /// overwrote `partialAnswerText` with only the latest raw chunk each
    /// call, so the panel would visibly jump/flicker instead of growing;
    /// every chunk Python now sends here is already clean speakable
    /// prose, never raw reasoning.)
    func appendPartialAnswerDelta(_ delta: String) {
        partialAnswerText = (partialAnswerText ?? "") + delta
        isAnswerSlow = false
    }

    func setAnswerSlow(_ slow: Bool) {
        isAnswerSlow = slow
    }

    func ingestAnswer(_ answer: CopilotAnswer) async {
        currentAnswer = answer
        isGeneratingAnswer = false
        partialAnswerText = nil
        lastAnswerFailureMessage = nil
        lastFailedQuestionID = nil
        lastFailedQuestionText = nil
        resetCandidateTracking()
        try? await repository.save(answer)
    }

    /// A generation round ended in failure/timeout (Section 17) —
    /// deliberately does **not** touch `currentAnswer`: the last real
    /// completed answer must stay visible, with this surfaced as a
    /// separate, retryable notice rather than overwriting it.
    func ingestAnswerFailure(_ message: String, questionID: String, questionText: String) {
        isGeneratingAnswer = false
        partialAnswerText = nil
        lastAnswerFailureMessage = message
        lastFailedQuestionID = questionID
        lastFailedQuestionText = questionText
        resetCandidateTracking()
    }

    func dismissAnswerFailure() {
        lastAnswerFailureMessage = nil
        lastFailedQuestionID = nil
        lastFailedQuestionText = nil
    }

    func setQueuedQuestionsCount(_ count: Int) {
        queuedQuestionsCount = count
    }

    func noteQueueOverflow(_ questionText: String) {
        queueOverflowMessage = "Too many questions came in at once — \"\(questionText)\" was not queued and will not be answered."
    }

    func dismissQueueOverflow() {
        queueOverflowMessage = nil
    }

    // MARK: - Question candidate / draft answer lifecycle (Section 15)

    func setQuestionCandidate(_ text: String) {
        candidateQuestionText = text
        candidateRevisionCount += 1
        if candidateState != .drafting {
            candidateState = .candidate
        }
    }

    func setQuestionUpdated(_ text: String) {
        candidateQuestionText = text
        candidateRevisionCount += 1
    }

    func setQuestionFinalized(_ text: String) {
        finalizedQuestionText = text
        candidateState = .finalized
    }

    /// `isReplacement`: `true` for `answer.draft_replaced`, `false` for
    /// `answer.draft_started` — a replacement after the turn already
    /// finalized is a refinement pass, not a fresh speculative draft.
    func beginDraftAnswer(sequence: Int, isReplacement: Bool, questionID: String? = nil) {
        draftSequence = sequence
        draftAnswerText = ""
        isDraftingAnswer = true
        isRefiningAnswer = isReplacement && candidateState == .finalized
        lastDraftTransitionReason = isReplacement ? .replaced : .started
        isAnswerSlow = false
        activeGeneratingQuestionID = questionID
        activeGeneratingQuestionText = finalizedQuestionText ?? candidateQuestionText
        if candidateState != .finalized {
            candidateState = .drafting
        }
    }

    func appendDraftDelta(_ delta: String, sequence: Int) {
        guard sequence == draftSequence else { return }  // stale/superseded — never mutate current state
        draftAnswerText += delta
        isAnswerSlow = false
    }

    func cancelDraftAnswer(sequence: Int) {
        guard sequence == draftSequence else { return }
        draftAnswerText = ""
        isDraftingAnswer = false
        isRefiningAnswer = false
        draftSequence = nil
        candidateState = .rejected
        lastDraftTransitionReason = .cancelled
    }

    /// Clears all candidate/draft transient state — called when a turn's
    /// answer actually completes (the durable `currentAnswer` takes over)
    /// or the session ends.
    func resetCandidateTracking() {
        candidateState = .idle
        candidateQuestionText = nil
        finalizedQuestionText = nil
        isDraftingAnswer = false
        isRefiningAnswer = false
        draftAnswerText = ""
        draftSequence = nil
        candidateRevisionCount = 0
        isAnswerSlow = false
        activeGeneratingQuestionID = nil
        activeGeneratingQuestionText = nil
    }

    // MARK: - Shared

    func end() {
        transcriptTask?.cancel()
        transcriptTask = nil
        phase = .ended
    }
}
