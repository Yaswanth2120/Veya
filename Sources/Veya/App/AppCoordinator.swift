import Foundation
import Carbon.HIToolbox

/// Top-level navigation + lifecycle owner: which screen is showing, the
/// live session's `ConversationState`, the overlay window, and global
/// hotkeys. One instance, created by `AppDelegate`, shared via
/// `@EnvironmentObject`.
@MainActor
final class AppCoordinator: ObservableObject {
    enum Route: Hashable {
        case dashboard
        case createSession
        case liveSession
        case previousSessions
        case knowledgeBase
        case personalProfile
        case settings
    }

    @Published var route: Route = .dashboard
    @Published private(set) var currentSession: Session?
    @Published private(set) var conversationState: ConversationState?

    let hotkeyManager = GlobalHotkeyManager()
    private(set) var overlayWindowController: OverlayWindowController?

    private let sessionRepository: SessionRepository

    init(sessionRepository: SessionRepository = SessionRepository()) {
        self.sessionRepository = sessionRepository
    }

    func showDashboard() {
        route = .dashboard
    }

    func startLiveSession(for session: Session) {
        var startedSession = session
        startedSession.status = .live
        currentSession = startedSession
        Task { try? await sessionRepository.update(startedSession) }

        let state = ConversationState(sessionID: session.id)
        conversationState = state

        let overlay = OverlayWindowController(conversationState: state)
        overlayWindowController = overlay

        route = .liveSession
        state.start()
        overlay.show()
    }

    func endLiveSession() {
        conversationState?.end()
        overlayWindowController?.hide()

        if var session = currentSession {
            session.status = .ended
            session.endedAt = Date()
            Task { try? await sessionRepository.update(session) }
        }

        currentSession = nil
        conversationState = nil
        overlayWindowController = nil
        route = .dashboard
    }

    /// Registers the two hotkeys from the build prompt: show/hide overlay,
    /// compact/expand overlay. Safe to call once at launch even before a
    /// live session exists — the closures just no-op until an overlay
    /// window controller is created.
    func registerHotkeys() {
        hotkeyManager.register(keyCode: UInt32(kVK_ANSI_O), modifiers: UInt32(cmdKey | shiftKey)) { [weak self] in
            self?.overlayWindowController?.toggleVisibility()
        }
        hotkeyManager.register(keyCode: UInt32(kVK_ANSI_C), modifiers: UInt32(cmdKey | shiftKey)) { [weak self] in
            self?.overlayWindowController?.toggleCompactMode()
        }
    }
}
