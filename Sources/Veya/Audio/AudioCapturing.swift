import Foundation

/// Native microphone capture — real implementation is `MicrophoneAudioCapture`
/// (AVAudioEngine-backed). Kept as a protocol so `PythonIntelligenceCoordinator`
/// and its tests never depend on AVFoundation directly; a fake conforms to
/// this in tests to drive the real-transcription selection/session logic
/// deterministically and without touching real hardware.
protocol AudioCapturing: AnyObject, Sendable {
    /// Registers (or returns the already-registered) stream of captured
    /// chunks. Safe to call before `start()`; chunks only begin flowing
    /// once capture is actually running.
    func chunks() -> AsyncStream<AudioChunk>

    /// Requests microphone permission if needed, configures and starts the
    /// capture engine. Throws `AudioCaptureError` on any failure —
    /// permission denial, engine start failure, or an unsupported input
    /// format — without ever having emitted a chunk.
    func start() async throws

    /// Stops capture and finishes the `chunks()` stream. A no-op if not
    /// currently running.
    func stop() async

    /// Chunks that were produced by the hardware tap but discarded because
    /// the bounded internal buffer was full — metadata only, never the
    /// audio itself. Exposed for diagnostics/tests, not user-facing.
    var droppedChunkCount: Int { get }
}
