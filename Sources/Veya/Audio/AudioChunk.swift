import Foundation

/// One timestamped slice of captured microphone audio, already normalized
/// to mono 16kHz signed 16-bit little-endian PCM (`pcm_s16le` on the wire
/// — see `docs/REALTIME_TRANSCRIPTION.md`). `sequence` is a strictly
/// increasing per-session counter the Python worker uses to detect
/// dropped/out-of-order delivery.
struct AudioChunk: Sendable, Equatable {
    let sequence: Int
    let startedAt: TimeInterval
    let duration: TimeInterval
    let pcm: Data
    let sampleRate: Int
    let channels: Int
}
