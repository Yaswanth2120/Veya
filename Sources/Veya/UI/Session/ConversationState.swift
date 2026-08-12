import Foundation

/// The state machine behind Live Session. Today it's driven by
/// `MockTranscriptSource`; later it will be driven by the real
/// `Transcription` subsystem through the same `ingest(_:)` entry point, so
/// none of this needs to change when that lands.
///
/// Pipeline: transcript segment → (canned) question detection → (canned)
/// answer generation → published for the overlay to observe.
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

    let sessionID: UUID
    private let repository: ConversationRepository
    private var transcriptTask: Task<Void, Never>?

    init(sessionID: UUID, repository: ConversationRepository = ConversationRepository()) {
        self.sessionID = sessionID
        self.repository = repository
    }

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

    func end() {
        transcriptTask?.cancel()
        transcriptTask = nil
        phase = .ended
    }

    /// The pipeline's single entry point: append the segment, run canned
    /// question detection, and if a question was detected, run the canned
    /// answer generator. Exposed (not private) so unit tests can drive the
    /// state machine directly without a real timer.
    func ingest(_ segment: TranscriptSegment) async {
        segments.append(segment)
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
    /// `Intelligence` question-detection subsystem.
    static func detectQuestion(in segment: TranscriptSegment, sessionID: UUID) -> DetectedQuestion? {
        let text = segment.text
        let lowercased = text.lowercased()
        let questionPrefixes = ["why", "how", "what", "when", "who", "so why", "so how"]
        let looksLikeQuestion = text.contains("?") || questionPrefixes.contains { lowercased.hasPrefix($0) }
        guard looksLikeQuestion else { return nil }
        return DetectedQuestion(id: UUID(), sessionID: sessionID, text: text, detectedAt: Date())
    }
}
