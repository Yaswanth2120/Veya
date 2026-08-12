import Foundation

/// STUB — interface only. Real system/microphone audio capture is a
/// separate, not-yet-scoped subsystem. Do not implement here.
protocol AudioCapturing {
    func startCapture() async throws
    func stopCapture() async
}
