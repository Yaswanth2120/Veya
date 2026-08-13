import Foundation
import Testing
@testable import Veya

@MainActor
@Suite("KnowledgeIngestionTracker")
struct KnowledgeIngestionTrackerTests {
    @Test("an unknown document defaults to not indexed")
    func unknownDocumentDefaultsToNotIndexed() {
        let tracker = KnowledgeIngestionTracker()
        #expect(tracker.status(forDocumentID: UUID()) == .notIndexed)
    }

    @Test("setStatus updates the tracked status for that document only")
    func setStatusUpdatesOnlyThatDocument() {
        let tracker = KnowledgeIngestionTracker()
        let documentA = UUID()
        let documentB = UUID()

        tracker.setStatus(.indexing, forDocumentID: documentA)

        #expect(tracker.status(forDocumentID: documentA) == .indexing)
        #expect(tracker.status(forDocumentID: documentB) == .notIndexed)
    }

    @Test("status transitions from indexing to ready")
    func transitionsToReady() {
        let tracker = KnowledgeIngestionTracker()
        let document = UUID()

        tracker.setStatus(.indexing, forDocumentID: document)
        tracker.setStatus(.ready, forDocumentID: document)

        #expect(tracker.status(forDocumentID: document) == .ready)
    }

    @Test("a failure carries a reason, clearing it if the status later changes")
    func failureReasonLifecycle() {
        let tracker = KnowledgeIngestionTracker()
        let document = UUID()

        tracker.setStatus(.failed, forDocumentID: document, reason: "Document exceeds the maximum size.")
        #expect(tracker.status(forDocumentID: document) == .failed)
        #expect(tracker.failureReasonByDocumentID[document] == "Document exceeds the maximum size.")

        tracker.setStatus(.ready, forDocumentID: document)
        #expect(tracker.failureReasonByDocumentID[document] == nil)
    }

    @Test("unsupported is a distinct status from failed")
    func unsupportedIsDistinctFromFailed() {
        let tracker = KnowledgeIngestionTracker()
        let document = UUID()

        tracker.setStatus(.unsupported, forDocumentID: document, reason: "'.exe' is not supported.")

        #expect(tracker.status(forDocumentID: document) == .unsupported)
        #expect(tracker.status(forDocumentID: document) != .failed)
    }

    @Test("display text matches the exact build prompt wording for every status")
    func displayTextMatchesSpec() {
        #expect(DocumentIngestionStatus.notIndexed.displayText == "Not indexed")
        #expect(DocumentIngestionStatus.indexing.displayText == "Indexing…")
        #expect(DocumentIngestionStatus.ready.displayText == "Ready")
        #expect(DocumentIngestionStatus.failed.displayText == "Failed to index")
        #expect(DocumentIngestionStatus.unsupported.displayText == "Unsupported document")
    }
}
