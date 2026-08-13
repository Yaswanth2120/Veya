import Foundation

/// STUB — interface only, still unused. Section 7 added real transcription,
/// but it runs in the Python worker (`core/veya/transcription/`) and
/// arrives back over the same worker-event path as the mock pipeline (see
/// `Bridge/IPCEventRouter.swift`), not through a Swift-side conformer of
/// this protocol. This stays reserved for a possible future *on-device*
/// Swift transcriber (e.g. the system Speech framework) that wouldn't go
/// through the Python worker at all — a separate, not-yet-scoped path.
protocol Transcribing {
    func transcriptStream(sessionID: UUID) -> AsyncStream<TranscriptSegment>
}
