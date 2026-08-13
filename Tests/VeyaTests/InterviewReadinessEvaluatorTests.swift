import Foundation
import Testing
@testable import Veya

@Suite("InterviewReadinessEvaluator")
struct InterviewReadinessEvaluatorTests {
    private func makeDocument(kind: DocumentKind = .other) -> SessionDocument {
        SessionDocument(
            id: UUID(),
            sessionID: UUID(),
            fileName: "doc.pdf",
            fileExtension: "pdf",
            storedPath: "/tmp/doc.pdf",
            fileSizeBytes: 1024,
            addedAt: Date(),
            documentKind: kind.rawValue
        )
    }

    @Test("no documents means ready with no resume")
    func noDocumentsIsReady() {
        let state = InterviewReadinessEvaluator.evaluate(documents: [], status: { _ in .notIndexed })
        #expect(state == .ready(hasReadyResume: false))
    }

    @Test("all documents ready and one is a ready resume reports hasReadyResume")
    func allReadyWithResumeReportsResumeReady() {
        let resume = makeDocument(kind: .resume)
        let other = makeDocument(kind: .other)
        let state = InterviewReadinessEvaluator.evaluate(
            documents: [resume, other],
            status: { _ in .ready }
        )
        #expect(state == .ready(hasReadyResume: true))
    }

    @Test("still indexing reports .indexing, not ready")
    func stillIndexingIsNotReady() {
        let document = makeDocument()
        let state = InterviewReadinessEvaluator.evaluate(documents: [document], status: { _ in .indexing })
        #expect(state == .indexing)
    }

    /// This is the exact bug the reviewer found: a `.failed` document must
    /// never be treated as satisfying readiness, and must never silently
    /// unlock the normal Start button — only `.blocked` (which requires an
    /// explicit "Start anyway") is allowed here.
    @Test("a failed document blocks readiness instead of satisfying it")
    func failedDocumentBlocksReadiness() {
        let document = makeDocument(kind: .resume)
        let state = InterviewReadinessEvaluator.evaluate(documents: [document], status: { _ in .failed })
        #expect(state == .blocked)
        #expect(state != .ready(hasReadyResume: true))
    }

    @Test("an unsupported document blocks readiness instead of satisfying it")
    func unsupportedDocumentBlocksReadiness() {
        let document = makeDocument()
        let state = InterviewReadinessEvaluator.evaluate(documents: [document], status: { _ in .unsupported })
        #expect(state == .blocked)
    }

    @Test("one ready resume plus one still-indexing document reports .indexing, not ready")
    func mixedReadyAndIndexingIsNotReady() {
        let resume = makeDocument(kind: .resume)
        let jd = makeDocument(kind: .jobDescription)
        let state = InterviewReadinessEvaluator.evaluate(
            documents: [resume, jd],
            status: { id in id == resume.id ? .ready : .indexing }
        )
        #expect(state == .indexing)
    }

    @Test("a ready document alongside a failed document reports .blocked, not ready")
    func readyPlusFailedIsBlockedNotReady() {
        let resume = makeDocument(kind: .resume)
        let jd = makeDocument(kind: .jobDescription)
        let state = InterviewReadinessEvaluator.evaluate(
            documents: [resume, jd],
            status: { id in id == resume.id ? .ready : .failed }
        )
        #expect(state == .blocked)
    }
}
