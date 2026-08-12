import Foundation
import Testing
@testable import Veya

@Suite("SessionRepository")
struct SessionRepositoryTests {
    private func makeRepository() -> SessionRepository {
        SessionRepository(db: DatabaseManager.makeInMemory())
    }

    @Test("create then fetch round-trips a session")
    func createThenFetch() async throws {
        let repository = makeRepository()
        let session = Session(
            id: UUID(),
            title: "Migration Recap",
            company: "Acme",
            roleOrTopic: "Staff Engineer",
            sessionDescription: "Quarterly migration recap",
            expectedParticipants: "Eng leadership",
            sessionType: .meeting,
            notes: "",
            preferredAnswerStyle: .concise,
            preferredProgrammingLanguage: "Swift",
            customInstructions: "",
            status: .notStarted,
            createdAt: Date(),
            endedAt: nil
        )

        try await repository.create(session)
        let fetched = try await repository.fetch(id: session.id)

        // GRDB persists `Date` with millisecond precision, so compare the
        // timestamp separately with a tolerance rather than relying on
        // exact `Date` equality via `Session: Equatable`.
        #expect(fetched?.id == session.id)
        #expect(fetched?.title == session.title)
        #expect(fetched?.company == session.company)
        #expect(fetched?.status == session.status)
        #expect(abs((fetched?.createdAt ?? .distantPast).timeIntervalSince(session.createdAt)) < 0.01)
    }

    @Test("fetchAll orders most recently created first")
    func fetchAllOrdering() async throws {
        let repository = makeRepository()
        let older = Session.makeTestSession(title: "Older", createdAt: Date(timeIntervalSince1970: 1000))
        let newer = Session.makeTestSession(title: "Newer", createdAt: Date(timeIntervalSince1970: 2000))

        try await repository.create(older)
        try await repository.create(newer)

        let all = try await repository.fetchAll()

        #expect(all.map(\.id) == [newer.id, older.id])
    }

    @Test("update persists status transitions")
    func updateStatus() async throws {
        let repository = makeRepository()
        var session = Session.makeTestSession(title: "Live Test")
        try await repository.create(session)

        session.status = .live
        try await repository.update(session)

        let fetched = try await repository.fetch(id: session.id)
        #expect(fetched?.status == .live)
    }

    @Test("delete removes the session")
    func delete() async throws {
        let repository = makeRepository()
        let session = Session.makeTestSession(title: "To Delete")
        try await repository.create(session)

        try await repository.delete(id: session.id)

        let fetched = try await repository.fetch(id: session.id)
        #expect(fetched == nil)
    }
}

extension Session {
    static func makeTestSession(
        title: String = "Test Session",
        createdAt: Date = Date()
    ) -> Session {
        Session(
            id: UUID(),
            title: title,
            company: "",
            roleOrTopic: "",
            sessionDescription: "",
            expectedParticipants: "",
            sessionType: .meeting,
            notes: "",
            preferredAnswerStyle: .concise,
            preferredProgrammingLanguage: "",
            customInstructions: "",
            status: .notStarted,
            createdAt: createdAt,
            endedAt: nil
        )
    }
}
