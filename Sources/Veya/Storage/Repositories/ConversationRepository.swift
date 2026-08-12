import Foundation
import GRDB

/// Persists the three mocked-pipeline entities: transcript segments,
/// detected questions, and generated answers. One repository since they're
/// always written together as a session plays out.
final class ConversationRepository: Sendable {
    private let db: DatabaseManager

    init(db: DatabaseManager = .shared) {
        self.db = db
    }

    func save(_ segment: TranscriptSegment) async throws {
        try await db.dbWriter.write { db in
            try segment.insert(db)
        }
    }

    func save(_ question: DetectedQuestion) async throws {
        try await db.dbWriter.write { db in
            try question.insert(db)
        }
    }

    func save(_ answer: CopilotAnswer) async throws {
        try await db.dbWriter.write { db in
            try answer.insert(db)
        }
    }

    func transcript(sessionID: UUID) async throws -> [TranscriptSegment] {
        try await db.dbWriter.read { db in
            try TranscriptSegment
                .filter(Column("sessionID") == sessionID)
                .order(Column("startedAt"))
                .fetchAll(db)
        }
    }

    func questions(sessionID: UUID) async throws -> [DetectedQuestion] {
        try await db.dbWriter.read { db in
            try DetectedQuestion
                .filter(Column("sessionID") == sessionID)
                .order(Column("detectedAt"))
                .fetchAll(db)
        }
    }

    func answers(sessionID: UUID) async throws -> [CopilotAnswer] {
        try await db.dbWriter.read { db in
            try CopilotAnswer
                .filter(Column("sessionID") == sessionID)
                .order(Column("generatedAt"))
                .fetchAll(db)
        }
    }
}
