import Foundation

/// Centralized routing from worker events into the existing
/// `ConversationState`/`ConversationRepository` models — the only place
/// that translates Python's mock-pipeline events into Swift state
/// mutations. No SwiftUI view ever touches an `IPCEvent` directly.
///
/// One router instance is shared for the app's lifetime; `attach`/
/// `detach` bind it to whichever session is currently being driven by the
/// Python worker (never more than one at a time, in this phase).
///
/// `route(_:)` is `async` and awaited by its one caller
/// (`PythonWorkerManager`'s sequential event-consumer loop) rather than
/// spawning an unstructured `Task` — that's what guarantees events are
/// applied to `ConversationState` in the same order the worker emitted
/// them (transcript.partial → transcript.final → question.detected →
/// answer.started → answer.delta → answer.completed).
@MainActor
final class IPCEventRouter {
    private weak var conversationState: ConversationState?
    private var expectedSessionId: String?
    /// The sequence number of the answer generation round currently being
    /// streamed, if any (Section 8). `answer.started` only ever moves this
    /// forward; `answer.delta`/`answer.completed` are only applied when
    /// their `sequence` matches exactly — this is what lets a superseded
    /// or cancelled answer's late/duplicate events be dropped safely
    /// instead of corrupting a newer answer's in-progress state.
    private var currentAnswerSequence: Int?
    /// Document ingestion status (Section 9) is app-lifetime state, not
    /// per-session — unlike `conversationState`, never cleared by
    /// `attach`/`detach`, so `knowledge.ingestion_*` events update it
    /// whether or not a Live Session is currently attached. Internal
    /// (not `private`) so `PythonIntelligenceCoordinator` can expose the
    /// same instance to SwiftUI without constructing a second, disconnected
    /// tracker.
    let knowledgeTracker: KnowledgeIngestionTracker

    init(knowledgeTracker: KnowledgeIngestionTracker = KnowledgeIngestionTracker()) {
        self.knowledgeTracker = knowledgeTracker
    }

    func attach(state: ConversationState, sessionID: UUID) {
        conversationState = state
        expectedSessionId = sessionID.uuidString
        currentAnswerSequence = nil
    }

    func detach() {
        conversationState = nil
        expectedSessionId = nil
        currentAnswerSequence = nil
    }

    func route(_ event: IPCEvent) async {
        // Knowledge ingestion status is independent of any attached
        // session/`ConversationState` — handled first, unconditionally.
        switch event.name {
        case "knowledge.ingestion_started":
            guard let data: KnowledgeIngestionStartedEventData = try? event.data.decoded(),
                  let documentUUID = UUID(uuidString: data.documentId)
            else { return }
            knowledgeTracker.setStatus(.indexing, forDocumentID: documentUUID)
            return

        case "knowledge.ingestion_progress":
            // No UI state change — chunk/embedding progress isn't
            // surfaced more granularly than "Indexing…" in this section.
            return

        case "knowledge.ingestion_completed":
            guard let data: KnowledgeIngestionCompletedEventData = try? event.data.decoded(),
                  let documentUUID = UUID(uuidString: data.documentId)
            else { return }
            knowledgeTracker.setStatus(.ready, forDocumentID: documentUUID)
            return

        case "knowledge.ingestion_failed":
            guard let data: KnowledgeIngestionFailedEventData = try? event.data.decoded(),
                  let documentUUID = UUID(uuidString: data.documentId),
                  let status = DocumentIngestionStatus(rawValue: data.status)
            else { return }
            knowledgeTracker.setStatus(status, forDocumentID: documentUUID, reason: data.reason)
            return

        default:
            break
        }

        guard let state = conversationState else { return }

        switch event.name {
        case "session.started", "session.ended":
            // Informational only in this phase — Swift already owns
            // session status transitions via `AppCoordinator`.
            break

        case "transcript.partial":
            guard let data: TranscriptPartialEventData = decode(event, matching: \.sessionId) else { return }
            state.setPartialTranscript(data.text)

        case "transcript.final":
            guard let data: TranscriptFinalEventData = decode(event, matching: \.sessionId),
                  let sessionUUID = UUID(uuidString: data.sessionId)
            else { return }
            let segment = TranscriptSegment(
                id: UUID(uuidString: data.id) ?? UUID(),
                sessionID: sessionUUID,
                text: data.text,
                startedAt: data.startedAt,
                endedAt: data.endedAt,
                isFinal: data.isFinal
            )
            await state.ingestTranscriptSegment(segment)

        case "question.detected":
            guard let data: QuestionDetectedEventData = decode(event, matching: \.sessionId),
                  let sessionUUID = UUID(uuidString: data.sessionId),
                  let questionUUID = UUID(uuidString: data.questionId)
            else { return }
            let question = DetectedQuestion(id: questionUUID, sessionID: sessionUUID, text: data.text, detectedAt: Date())
            await state.ingestDetectedQuestion(question)

        case "answer.started":
            guard let data: AnswerStartedEventData = decode(event, matching: \.sessionId) else { return }
            // A new answer round only ever moves forward — a
            // started/duplicate/stale sequence (<= what's already
            // current) is dropped rather than restarting "Generating
            // answer…" for something already superseded.
            guard currentAnswerSequence == nil || data.sequence > currentAnswerSequence! else { return }
            currentAnswerSequence = data.sequence
            state.setAnswerGenerating(true)

        case "answer.delta":
            guard let data: AnswerDeltaEventData = decode(event, matching: \.sessionId),
                  data.sequence == currentAnswerSequence
            else { return }
            state.setPartialAnswer(data.delta)

        case "answer.completed":
            guard let data: AnswerCompletedEventData = decode(event, matching: \.sessionId),
                  data.sequence == currentAnswerSequence,
                  let sessionUUID = UUID(uuidString: data.sessionId),
                  let questionUUID = UUID(uuidString: data.questionId)
            else { return }
            var talkingPoints = data.talkingPoints
            if !data.caveat.isEmpty {
                talkingPoints.append("Caveat: \(data.caveat)")
            }
            // Structured sources (Section 9) are folded into compact
            // "filename: excerpt" display strings — `CopilotAnswer.sources`
            // stays `[String]` (no schema/migration change), and
            // `OverlayView` already renders `sources.first` as-is, so this
            // keeps the overlay's existing compact source display working
            // unchanged. Never more than what Python actually retrieved —
            // `data.sources` is `[]` whenever no retrieval occurred.
            let sources = data.sources.map { "\($0.fileName): \($0.excerpt)" }
            let answer = CopilotAnswer(
                id: UUID(),
                sessionID: sessionUUID,
                questionID: questionUUID,
                question: data.question,
                talkingPoints: talkingPoints,
                sources: sources,
                generatedAt: Date()
            )
            await state.ingestAnswer(answer)

        default:
            break
        }
    }

    /// Decodes `event.data` as `T` and returns it only if its `sessionId`
    /// (via `keyPath`) matches the currently-attached session — events for
    /// a stale/previous session (e.g. arriving after `detach()` raced with
    /// in-flight worker output) are silently dropped rather than mutating
    /// the wrong `ConversationState`.
    private func decode<T: Decodable>(_ event: IPCEvent, matching keyPath: (T) -> String) -> T? {
        guard let value: T = try? event.data.decoded() else { return nil }
        guard let expectedSessionId else { return value }
        guard keyPath(value) == expectedSessionId else { return nil }
        return value
    }
}
