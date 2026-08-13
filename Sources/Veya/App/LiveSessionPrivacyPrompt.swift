import Foundation

/// A pending confirmation `AppCoordinator` surfaces before starting a Live
/// Session when Presenter Privacy is enabled but not yet ready — see build
/// prompt §5.22. The user is always given a way to continue anyway.
enum LiveSessionPrivacyPrompt: Identifiable {
    case confirmStartSafeShare(Session)
    case confirmUnverifiedDirectOverlay(Session)

    var id: String {
        switch self {
        case .confirmStartSafeShare: return "confirmStartSafeShare"
        case .confirmUnverifiedDirectOverlay: return "confirmUnverifiedDirectOverlay"
        }
    }

    var session: Session {
        switch self {
        case .confirmStartSafeShare(let session): return session
        case .confirmUnverifiedDirectOverlay(let session): return session
        }
    }
}
