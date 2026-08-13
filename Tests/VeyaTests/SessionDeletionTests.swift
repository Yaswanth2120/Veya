import Foundation
import Testing
@testable import Veya

/// Regression coverage for a review finding: `SessionRepository.delete(id:)`
/// existed but nothing in the UI ever called it, and there was no
/// confirmation flow. `AppCoordinator.deleteSession(_:)` is the actual
/// delete path the UI (`PreviousSessionsView`) now calls — this proves it
/// genuinely cascades every related row through a real (in-memory) SQLite
/// database, not just that the method compiles.
@MainActor
@Suite("AppCoordinator.deleteSession — cascade cleanup")
struct SessionDeletionTests {
    private func makeCoordinator(db: DatabaseManager) -> AppCoordinator {
        AppCoordinator(
            sessionRepository: SessionRepository(db: db),
            conversationRepository: ConversationRepository(db: db),
            sessionDocumentRepository: SessionDocumentRepository(db: db),
            presenterPrivacyManager: PresenterPrivacyManager(),
            pythonIntelligenceCoordinator: PythonIntelligenceCoordinator(workerManager: PythonWorkerManager(configuration: .resolveDefault()))
        )
    }

    @Test("deleting a session removes the session row and cascades transcript, questions, answers, and the report")
    func deletionCascadesAllRelatedRows() async throws {
        let db = DatabaseManager.makeInMemory()
        let sessionRepository = SessionRepository(db: db)
        let conversationRepository = ConversationRepository(db: db)
        let coordinator = makeCoordinator(db: db)

        let session = Session.makeTestSession(title: "To Be Deleted")
        try await sessionRepository.create(session)

        let segment = TranscriptSegment(id: UUID(), sessionID: session.id, text: "hello", startedAt: 0, endedAt: 1, isFinal: true)
        try await conversationRepository.save(segment)
        let question = DetectedQuestion(id: UUID(), sessionID: session.id, text: "why?", detectedAt: Date())
        try await conversationRepository.save(question)
        let answer = CopilotAnswer(id: UUID(), sessionID: session.id, questionID: question.id, question: "why?", talkingPoints: ["because"], sources: [], generatedAt: Date())
        try await conversationRepository.save(answer)
        let report = SessionReport(id: UUID(), sessionID: session.id, summary: "s", topics: [], questions: [], generatedAnswers: [], sources: [], decisions: [], actionItems: [], unansweredQuestions: [], preparationGaps: [], generatedAt: Date())
        try await conversationRepository.save(report)

        try await coordinator.deleteSession(session)

        let fetchedSession = try await sessionRepository.fetch(id: session.id)
        #expect(fetchedSession == nil)
        let remainingTranscript = try await conversationRepository.transcript(sessionID: session.id)
        #expect(remainingTranscript.isEmpty)
        let remainingQuestions = try await conversationRepository.questions(sessionID: session.id)
        #expect(remainingQuestions.isEmpty)
        let remainingAnswers = try await conversationRepository.answers(sessionID: session.id)
        #expect(remainingAnswers.isEmpty)
        let remainingReport = try await conversationRepository.report(sessionID: session.id)
        #expect(remainingReport == nil)
    }

    @Test("deleting a session removes its on-disk document files, not just the database row")
    func deletionRemovesDocumentFilesFromDisk() async throws {
        let db = DatabaseManager.makeInMemory()
        let sessionRepository = SessionRepository(db: db)
        let sessionDocumentRepository = SessionDocumentRepository(db: db)
        let coordinator = makeCoordinator(db: db)

        let session = Session.makeTestSession(title: "Has A Document")
        try await sessionRepository.create(session)

        let tmpFile = FileManager.default.temporaryDirectory.appendingPathComponent("veya-test-doc-\(UUID().uuidString).txt")
        try "hello world".write(to: tmpFile, atomically: true, encoding: .utf8)
        #expect(FileManager.default.fileExists(atPath: tmpFile.path))

        let document = SessionDocument(id: UUID(), sessionID: session.id, fileName: "notes.txt", fileExtension: "txt", storedPath: tmpFile.path, fileSizeBytes: 11, addedAt: Date())
        try await sessionDocumentRepository.create(document)

        try await coordinator.deleteSession(session)

        #expect(!FileManager.default.fileExists(atPath: tmpFile.path))
        let remainingDocuments = try await sessionDocumentRepository.fetchAll(sessionID: session.id)
        #expect(remainingDocuments.isEmpty)
    }
}
