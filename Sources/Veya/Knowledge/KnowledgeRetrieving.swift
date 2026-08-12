import Foundation

/// STUB — interface only. Real document ingestion / chunking / embedding /
/// RAG retrieval is a separate, not-yet-scoped subsystem. `SessionDocument`
/// today stores file metadata + a copy of the file only; nothing reads its
/// contents.
protocol KnowledgeRetrieving {
    func relevantPassages(for query: String, sessionID: UUID) async throws -> [String]
}
