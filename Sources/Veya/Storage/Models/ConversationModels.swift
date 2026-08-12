import Foundation
import GRDB

/// A single chunk of transcript. Produced today by `MockTranscriptSource`;
/// will be produced by the real `Transcription` subsystem later without any
/// change to this shape.
struct TranscriptSegment: Identifiable, Codable, Equatable, FetchableRecord, PersistableRecord {
    let id: UUID
    let sessionID: UUID
    let text: String
    let startedAt: TimeInterval
    let endedAt: TimeInterval?
    let isFinal: Bool

    static let databaseTableName = "transcriptSegment"
}

/// A question the copilot believes was asked. Produced today by a canned
/// keyword match in `ConversationState`; will be produced by the real
/// `Intelligence` subsystem later.
///
/// `sessionID` is an addition on top of the spec's minimal shape, needed so
/// persisted questions can be associated back to a session.
struct DetectedQuestion: Identifiable, Codable, Equatable, FetchableRecord, PersistableRecord {
    let id: UUID
    let sessionID: UUID
    let text: String
    let detectedAt: Date

    static let databaseTableName = "detectedQuestion"
}

/// A generated answer for the overlay to display. Produced today by
/// `MockAnswerGenerator` (canned talking points, no LLM call); will be
/// produced by the real `Intelligence`/`Knowledge` subsystems later.
///
/// `sessionID` and `questionID` are additions on top of the spec's minimal
/// shape, needed so persisted answers can be associated back to a session
/// and the question that triggered them.
struct CopilotAnswer: Identifiable, Codable, Equatable, FetchableRecord, PersistableRecord {
    let id: UUID
    let sessionID: UUID
    let questionID: UUID
    let question: String
    let talkingPoints: [String]
    let sources: [String]
    let generatedAt: Date

    static let databaseTableName = "generatedAnswer"
}
