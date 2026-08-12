import Foundation

/// STUB — interface only. Placeholder for a future abstraction over
/// LLM/transcription providers (local model, hosted API, etc.). No
/// implementation belongs here yet.
protocol ModelProvider {
    var identifier: String { get }
}
