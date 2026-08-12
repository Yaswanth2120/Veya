import Foundation

/// STUB — interface only. Real speech-to-text transcription is a separate,
/// not-yet-scoped subsystem. `MockTranscriptSource` (in UI/Session) stands
/// in for this during the current phase; it does not conform to this
/// protocol on purpose, so swapping in a real implementation later is an
/// explicit, deliberate change rather than an accidental one.
protocol Transcribing {
    func transcriptStream(sessionID: UUID) -> AsyncStream<TranscriptSegment>
}
