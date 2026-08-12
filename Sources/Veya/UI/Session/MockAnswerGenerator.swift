import Foundation

/// Canned talking points, keyed by a keyword match against the detected
/// question's text. No LLM call, no retrieval — a placeholder for the real
/// `Intelligence`/`Knowledge` subsystems.
enum MockAnswerGenerator {
    static func answer(for question: DetectedQuestion, sessionID: UUID) -> CopilotAnswer {
        let lowercased = question.text.lowercased()

        let talkingPoints: [String]
        let sources: [String]

        if lowercased.contains("six weeks") || lowercased.contains("migration") {
            talkingPoints = [
                "Authentication service was the hard dependency everything else waited on",
                "Rolled out in stages, not a single cutover",
                "Backward compatibility was kept the whole way through",
                "Final migration completed safely with no downtime",
            ]
            sources = ["Migration Notes"]
        } else {
            talkingPoints = [
                "Restate the question to confirm scope before answering",
                "Lead with the outcome, then the reasoning",
                "Keep it to two or three points",
            ]
            sources = ["General Talking Points"]
        }

        return CopilotAnswer(
            id: UUID(),
            sessionID: sessionID,
            questionID: question.id,
            question: question.text,
            talkingPoints: talkingPoints,
            sources: sources,
            generatedAt: Date()
        )
    }
}
