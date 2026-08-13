import Foundation

/// Tracks per-document ingestion status/failure-reason as
/// `knowledge.ingestion_*` events arrive — independent of any single Live
/// Session's lifecycle (a document ingested once should keep reading
/// "Ready" whether or not a session is currently live), so this is a
/// persistent, app-lifetime object rather than something `IPCEventRouter`
/// attaches/detaches per session.
///
/// Never stores document content — only status, a document ID, and (on
/// failure) the same safe/typed reason string Python already guarantees
/// never contains document text (see `docs/KNOWLEDGE_RETRIEVAL.md`).
@MainActor
final class KnowledgeIngestionTracker: ObservableObject {
    @Published private(set) var statusByDocumentID: [UUID: DocumentIngestionStatus] = [:]
    @Published private(set) var failureReasonByDocumentID: [UUID: String] = [:]

    func status(forDocumentID id: UUID) -> DocumentIngestionStatus {
        statusByDocumentID[id] ?? .notIndexed
    }

    func setStatus(_ status: DocumentIngestionStatus, forDocumentID id: UUID, reason: String? = nil) {
        statusByDocumentID[id] = status
        failureReasonByDocumentID[id] = reason
    }
}
