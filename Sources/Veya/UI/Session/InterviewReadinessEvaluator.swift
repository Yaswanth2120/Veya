import Foundation

/// Pure evaluation of whether an Interview Copilot session's attached
/// documents are ready to start on. Extracted out of `CreateSessionView` so
/// this logic — the actual gating rule — is directly testable without
/// instantiating a SwiftUI view.
enum InterviewReadinessState: Equatable {
    /// At least one document is still `.notIndexed`/`.indexing`, and none
    /// have settled into a terminal failure state yet.
    case indexing
    /// At least one document has settled into `.failed`/`.unsupported` —
    /// it will never become ready on its own. The normal Start button
    /// stays disabled; only the explicit "Start anyway" escape hatch can
    /// proceed.
    case blocked
    /// Every attached document finished indexing successfully.
    case ready(hasReadyResume: Bool)
}

enum InterviewReadinessEvaluator {
    static func evaluate(
        documents: [SessionDocument],
        status: (UUID) -> DocumentIngestionStatus
    ) -> InterviewReadinessState {
        if documents.allSatisfy({ status($0.id) == .ready }) {
            let hasReadyResume = documents.contains { $0.documentKind == DocumentKind.resume.rawValue }
            return .ready(hasReadyResume: hasReadyResume)
        }

        let hasBlockingIssue = documents.contains { document in
            let value = status(document.id)
            return value == .failed || value == .unsupported
        }
        return hasBlockingIssue ? .blocked : .indexing
    }
}
