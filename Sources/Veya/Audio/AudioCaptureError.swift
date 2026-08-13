import Foundation

enum AudioCaptureError: LocalizedError, Sendable, Equatable {
    case microphonePermissionDenied
    case engineStartFailed(String)
    case unsupportedFormat(String)
    case captureStoppedUnexpectedly(String)

    var errorDescription: String? {
        switch self {
        case .microphonePermissionDenied:
            return "Microphone access was not granted."
        case .engineStartFailed(let reason):
            return "Could not start audio capture: \(reason)"
        case .unsupportedFormat(let reason):
            return "Unsupported audio format: \(reason)"
        case .captureStoppedUnexpectedly(let reason):
            return "Audio capture stopped unexpectedly: \(reason)"
        }
    }
}
