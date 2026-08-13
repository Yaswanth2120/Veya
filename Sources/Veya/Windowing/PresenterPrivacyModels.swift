import Foundation
import GRDB

/// Result of the most recent (or an in-progress) capture verification.
enum PresenterPrivacyStatus: String, Codable, Sendable {
    case disabled
    case notTested
    case testing
    case verified
    case uncertain
    case unsupported
    case overlayDetected
    case error

    var displayName: String {
        switch self {
        case .disabled: return "Disabled"
        case .notTested: return "Not tested"
        case .testing: return "Testing…"
        case .verified: return "Verified"
        case .uncertain: return "Uncertain"
        case .unsupported: return "Unsupported"
        case .overlayDetected: return "Overlay detected"
        case .error: return "Error"
        }
    }
}

/// Which privacy path Veya is using.
enum PresenterPrivacyMode: String, Codable, CaseIterable, Sendable, Identifiable {
    /// No privacy behavior.
    case normal
    /// Best-effort native window-sharing configuration, verified locally
    /// by `CaptureCompatibilityTester`. Not guaranteed against every
    /// third-party capture path.
    case directPrivateOverlay
    /// Veya owns a `ScreenCaptureKit` capture of the display, excludes its
    /// own application from that capture, and renders the result into the
    /// `Veya Safe Share` window for the user to share instead of their
    /// real desktop.
    case safeShare

    var id: String { rawValue }

    var displayName: String {
        switch self {
        case .normal: return "Normal"
        case .directPrivateOverlay: return "Private Overlay — Experimental"
        case .safeShare: return "Safe Share — Recommended"
        }
    }
}

/// User-configurable presenter-privacy settings. Persisted via
/// `PresenterPrivacyPreferencesStore`.
struct PresenterPrivacyPreferences: Codable, Equatable, Sendable {
    var enabled: Bool
    var selectedMode: PresenterPrivacyMode
    var runTestBeforeSession: Bool
    var warnWhenUnverified: Bool
    var preferredDisplayID: UInt32?
    var safeShareFPS: Int

    static let `default` = PresenterPrivacyPreferences(
        enabled: false,
        selectedMode: .safeShare,
        runTestBeforeSession: true,
        warnWhenUnverified: true,
        preferredDisplayID: nil,
        safeShareFPS: 30
    )
}

/// Result of a single capture-compatibility test run (possibly aggregating
/// several frames — see `CaptureCompatibilityTester`).
struct CaptureTestResult: Identifiable, Codable, Equatable, Sendable {
    let id: UUID
    let testedAt: Date
    let macOSVersion: String
    let appVersion: String
    let displayID: UInt32
    /// `nil` when the test could not run at all (e.g. permission denied).
    let overlayDetected: Bool?
    /// 0...1. How much of the multi-frame sample agreed with the final
    /// verdict — see `CaptureCompatibilityTester` for aggregation rules.
    let confidence: Double
    let status: PresenterPrivacyStatus
    let diagnosticMessage: String
}

/// A persisted historical compatibility test, scoped to the mode that was
/// active when it ran.
struct CaptureCompatibilityRecord: Identifiable, Codable, Equatable, Sendable, FetchableRecord, PersistableRecord {
    let id: UUID
    let testedAt: Date
    let macOSVersion: String
    let veyaVersion: String
    let displayID: UInt32
    let mode: PresenterPrivacyMode
    let result: CaptureTestResult

    static let databaseTableName = "captureCompatibilityRecord"
}

/// Safe Share capture quality presets. Frame rate and resolution scale
/// trade off against memory/CPU on the 8 GB M2 target — see
/// `docs/PRESENTER_PRIVACY.md`.
enum SafeShareQuality: String, Codable, CaseIterable, Sendable, Identifiable {
    case efficiency
    case balanced
    case quality

    var id: String { rawValue }

    var displayName: String {
        switch self {
        case .efficiency: return "Efficiency"
        case .balanced: return "Balanced"
        case .quality: return "Quality"
        }
    }

    var suggestedFPS: Int {
        switch self {
        case .efficiency: return 15
        case .balanced: return 30
        case .quality: return 30
        }
    }

    /// Fraction of the display's native pixel size to render at.
    var resolutionScale: CGFloat {
        switch self {
        case .efficiency: return 0.75
        case .balanced: return 1.0
        case .quality: return 1.0
        }
    }
}

enum PresenterPrivacyError: LocalizedError, Sendable {
    case noOverlayWindow
    case displayUnavailable
    case screenCapturePermissionDenied
    case captureInitializationFailed(String)
    case applicationFilterUnavailable
    case noFramesReceived
    case diagnosticMarkerUnavailable
    case detectionInconclusive
    case safeShareAlreadyRunning
    case safeShareNotRunning

    var errorDescription: String? {
        switch self {
        case .noOverlayWindow:
            return "Veya's overlay isn't on screen, so there's nothing to test. Start a Live Session and try again."
        case .displayUnavailable:
            return "The selected display is no longer available."
        case .screenCapturePermissionDenied:
            return "Veya needs Screen Recording permission to create a clean Safe Share view of your desktop with Veya excluded. Grant it in System Settings → Privacy & Security → Screen Recording."
        case .captureInitializationFailed(let reason):
            return "Couldn't start screen capture: \(reason)"
        case .applicationFilterUnavailable:
            return "Veya couldn't be identified in the list of shareable applications, so it can't be excluded from the capture."
        case .noFramesReceived:
            return "No frames were captured. Try the test again."
        case .diagnosticMarkerUnavailable:
            return "Veya's overlay isn't ready to display its diagnostic marker yet."
        case .detectionInconclusive:
            return "The capture test didn't produce a clear result."
        case .safeShareAlreadyRunning:
            return "Safe Share is already running."
        case .safeShareNotRunning:
            return "Safe Share isn't running."
        }
    }
}
