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
    /// Coding Practice only: explicit user consent for local code
    /// execution *in this session*. Execution is still gated first by the
    /// worker-level `VEYA_CODE_EXECUTION_ENABLED` build setting — this is
    /// a second, session-scoped layer the Coding Workbench's Run action
    /// also requires, never a substitute for the build-level gate.
    var codeExecutionConsent: Bool = false
    /// System Design only: free-text expected scale/traffic context
    /// (e.g. "100M redirects/day"), shown to the user and folded into the
    /// design follow-up prompt alongside the description field.
    var expectedScale: String = ""

    static let databaseTableName = "session"
}

/// Section 16: which role an attached document plays for Interview
/// Copilot retrieval — `.other` (the default) means "no special
/// treatment," matching every document attached before this existed.
enum DocumentKind: String, Codable, CaseIterable, Identifiable, Sendable {
    case resume
    case jobDescription
    case other

    var id: String { rawValue }

    var displayName: String {
        switch self {
        case .resume: return "Resume"
        case .jobDescription: return "Job Description"
        case .other: return "Other"
        }
    }
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
    /// Section 16: "resume"/"jobDescription"/"other" — stored as the raw
    /// string (not `DocumentKind` directly) so an unrecognized future
    /// value never fails to decode a persisted row. Defaults to "other"
    /// for documents attached before this field existed.
    var documentKind: String = DocumentKind.other.rawValue

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
