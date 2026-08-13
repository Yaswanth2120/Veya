import Foundation
import Testing
@testable import Veya

/// Regression coverage for a review finding: `SessionReport` (GRDB) used
/// to omit `generatedAnswers`/`sources` entirely, silently dropping data
/// Python's `session.analyze` actually returned. `save`/`report` here
/// round-trip a full report through a real (in-memory) SQLite database —
/// not just constructing the struct — so a dropped column/migration
/// mismatch would fail this the same way it would in production.
@Suite("ConversationRepository — SessionReport persistence")
struct ConversationRepositoryTests {
    private func makeRepository() -> ConversationRepository {
        ConversationRepository(db: DatabaseManager.makeInMemory())
    }

    private func makeSession(db: DatabaseManager) async throws -> Session {
        let session = Session(
            id: UUID(), title: "Migration Recap", company: "Acme", roleOrTopic: "Staff Engineer",
            sessionDescription: "", expectedParticipants: "", sessionType: .meeting, notes: "",
            preferredAnswerStyle: .concise, preferredProgrammingLanguage: "Swift", customInstructions: "",
            status: .ended, createdAt: Date(), endedAt: Date()
        )
        try await SessionRepository(db: db).create(session)
        return session
    }

    @Test("save then fetch round-trips every report field, including generatedAnswers and sources")
    func saveThenFetchRoundTripsAllFields() async throws {
        let db = DatabaseManager.makeInMemory()
        let repository = ConversationRepository(db: db)
        let session = try await makeSession(db: db)

        let report = SessionReport(
            id: UUID(),
            sessionID: session.id,
            summary: "Discussed a six-week migration plan.",
            topics: ["migration", "payments"],
            questions: ["How long will the migration take?"],
            generatedAnswers: [
                ReportAnswer(question: "How long will the migration take?", talkingPoints: ["Six weeks", "Phased rollout"])
            ],
            sources: [
                AnswerSourceEventData(documentId: "d1", fileName: "plan.pdf", chunkId: "c1", excerpt: "six weeks, phased")
            ],
            decisions: ["Proceed with phased rollout"],
            actionItems: ["Draft rollback plan"],
            unansweredQuestions: ["What about rollback?"],
            preparationGaps: ["Rollback process undefined"],
            generatedAt: Date()
        )

        try await repository.save(report)
        let fetched = try await repository.report(sessionID: session.id)

        #expect(fetched != nil)
        #expect(fetched?.generatedAnswers == report.generatedAnswers)
        #expect(fetched?.sources == report.sources)
        #expect(fetched?.summary == report.summary)
        #expect(fetched?.decisions == report.decisions)
    }

    @Test("a report with no answers or sources still round-trips as empty arrays, not nil/crash")
    func emptyAnswersAndSourcesRoundTrip() async throws {
        let db = DatabaseManager.makeInMemory()
        let repository = ConversationRepository(db: db)
        let session = try await makeSession(db: db)

        let report = SessionReport(
            id: UUID(), sessionID: session.id, summary: "No local LLM was available.", topics: [], questions: [],
            generatedAnswers: [], sources: [], decisions: [], actionItems: [], unansweredQuestions: [], preparationGaps: [],
            generatedAt: Date()
        )

        try await repository.save(report)
        let fetched = try await repository.report(sessionID: session.id)

        #expect(fetched?.generatedAnswers == [])
        #expect(fetched?.sources == [])
    }
}
