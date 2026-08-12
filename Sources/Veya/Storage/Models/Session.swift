import Foundation
import GRDB

enum SessionType: String, Codable, CaseIterable, Identifiable {
    case presentation
    case meeting
    case clientCall
    case technicalMeeting
    case interviewPractice
    case codingPractice
    case systemDesign

    var id: String { rawValue }

    var displayName: String {
        switch self {
        case .presentation: return "Presentation"
        case .meeting: return "Meeting"
        case .clientCall: return "Client Call"
        case .technicalMeeting: return "Technical Meeting"
        case .interviewPractice: return "Interview Practice"
        case .codingPractice: return "Coding Practice"
        case .systemDesign: return "System Design"
        }
    }
}

enum AnswerStyle: String, Codable, CaseIterable, Identifiable {
    case concise
    case detailed
    case bulletPoints

    var id: String { rawValue }

    var displayName: String {
        switch self {
        case .concise: return "Concise"
        case .detailed: return "Detailed"
        case .bulletPoints: return "Bullet Points"
        }
    }
}

enum SessionStatus: String, Codable {
    case notStarted
    case live
    case ended
}

struct Session: Identifiable, Codable, Equatable, FetchableRecord, PersistableRecord {
    var id: UUID
    var title: String
    var company: String
    var roleOrTopic: String
    var sessionDescription: String
    var expectedParticipants: String
    var sessionType: SessionType
    var notes: String
    var preferredAnswerStyle: AnswerStyle
    var preferredProgrammingLanguage: String
    var customInstructions: String
    var status: SessionStatus
    var createdAt: Date
    var endedAt: Date?

    static let databaseTableName = "session"
}

struct SessionDocument: Identifiable, Codable, Equatable, FetchableRecord, PersistableRecord {
    var id: UUID
    var sessionID: UUID
    var fileName: String
    var fileExtension: String
    /// Path to the copy Veya stores under Application Support. Metadata +
    /// file reference only — nothing parses this file in this phase.
    var storedPath: String
    var fileSizeBytes: Int64
    var addedAt: Date

    static let databaseTableName = "sessionDocument"
}

struct UserProfile: Identifiable, Codable, Equatable, FetchableRecord, PersistableRecord {
    var id: UUID
    var name: String
    var headline: String
    var background: String
    var defaultAnswerStyle: AnswerStyle
    var defaultProgrammingLanguage: String
    var updatedAt: Date

    static let databaseTableName = "userProfile"
}
