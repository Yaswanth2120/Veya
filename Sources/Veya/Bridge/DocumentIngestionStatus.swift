import Foundation

/// Mirrors `core/veya/knowledge/models.py`'s `IngestionStatus` — a
/// document's raw wire value (`knowledge.status`'s result, or the
/// `status` field on `knowledge.ingestion_failed`) is one of these.
enum DocumentIngestionStatus: String, Sendable, Equatable {
    case notIndexed = "not_indexed"
    case indexing = "indexing"
    case ready = "ready"
    case failed = "failed"
    case unsupported = "unsupported"

    /// Short, non-technical label per the build prompt's exact UI state
    /// list.
    var displayText: String {
        switch self {
        case .notIndexed: return "Not indexed"
        case .indexing: return "Indexing…"
        case .ready: return "Ready"
        case .failed: return "Failed to index"
        case .unsupported: return "Unsupported document"
        }
    }
}
