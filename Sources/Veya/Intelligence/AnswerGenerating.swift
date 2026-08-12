import Foundation

/// STUB — interface only. Real LLM-backed answer generation is a separate,
/// not-yet-scoped subsystem.
protocol AnswerGenerating {
    func generateAnswer(for question: DetectedQuestion, sessionID: UUID) async throws -> CopilotAnswer
}
