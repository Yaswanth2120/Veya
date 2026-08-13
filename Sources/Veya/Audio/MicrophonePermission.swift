import AVFoundation
import Foundation

enum MicrophoneAuthorizationState: Sendable, Equatable {
    case undetermined
    case authorized
    case denied
    case restricted
}

/// Abstraction over `AVCaptureDevice`'s microphone authorization API so
/// `PythonIntelligenceCoordinator` and its tests never touch AVFoundation
/// directly — tests inject a fake that never prompts or requires a real
/// microphone/TCC permission database.
protocol MicrophonePermissionChecking: Sendable {
    var currentStatus: MicrophoneAuthorizationState { get }
    /// Returns the current status if already determined; otherwise
    /// actually prompts the user and returns the result. Never prompts
    /// twice — macOS itself only allows one system prompt per app.
    func requestAccess() async -> MicrophoneAuthorizationState
}

struct AVFoundationMicrophonePermission: MicrophonePermissionChecking {
    var currentStatus: MicrophoneAuthorizationState {
        switch AVCaptureDevice.authorizationStatus(for: .audio) {
        case .authorized: return .authorized
        case .denied: return .denied
        case .restricted: return .restricted
        case .notDetermined: return .undetermined
        @unknown default: return .restricted
        }
    }

    func requestAccess() async -> MicrophoneAuthorizationState {
        let status = currentStatus
        guard status == .undetermined else { return status }
        let granted = await AVCaptureDevice.requestAccess(for: .audio)
        return granted ? .authorized : .denied
    }
}
