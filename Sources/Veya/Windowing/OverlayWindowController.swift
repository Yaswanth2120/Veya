import AppKit
import SwiftUI

/// Owns the floating overlay panel: a borderless, non-activating `NSPanel`
/// at `.floating` level, hosting the SwiftUI `OverlayView`. Handles
/// show/hide, compact/expanded resizing, opacity, and frame persistence.
///
/// This controller only handles window presentation — it does not set
/// `sharingType` or any other capture-exclusion behavior itself.
/// `PresenterPrivacyManager` reaches in via `managedWindow` to configure
/// that separately (see docs/PRESENTER_PRIVACY.md), keeping this class
/// focused on the same job it had before that subsystem existed.
@MainActor
final class OverlayWindowController: NSWindowController {
    private let preferencesStore: OverlayPreferencesStore
    private(set) var preferences: OverlayPreferences

    private static let frameAutosaveName = "VeyaOverlayPanel"
    private static let compactSize = NSSize(width: 380, height: 190)
    private static let expandedSize = NSSize(width: 440, height: 440)

    init(
        conversationState: ConversationState,
        privacyManager: PresenterPrivacyManager,
        preferencesStore: OverlayPreferencesStore = OverlayPreferencesStore()
    ) {
        self.preferencesStore = preferencesStore
        let loadedPreferences = preferencesStore.load()
        self.preferences = loadedPreferences

        let initialSize = loadedPreferences.compactMode
            ? OverlayWindowController.compactSize
            : OverlayWindowController.expandedSize

        let panel = NSPanel(
            contentRect: NSRect(origin: .zero, size: initialSize),
            styleMask: [.borderless, .nonactivatingPanel, .resizable],
            backing: .buffered,
            defer: false
        )
        panel.isFloatingPanel = true
        panel.level = loadedPreferences.alwaysOnTop ? .floating : .normal
        panel.hidesOnDeactivate = false
        panel.isMovableByWindowBackground = true
        panel.backgroundColor = .clear
        panel.isOpaque = false
        panel.hasShadow = true
        panel.alphaValue = loadedPreferences.opacity
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]

        // Capture-exclusion / screen-share visibility behavior for this
        // panel is configured by `PresenterPrivacyManager`
        // (`window.sharingType = .none`, best-effort — see
        // docs/PRESENTER_PRIVACY.md), not here. This controller only sets
        // up standard window-level appearance/behavior.

        super.init(window: panel)

        panel.setFrameAutosaveName(Self.frameAutosaveName)
        if !panel.setFrameUsingName(Self.frameAutosaveName) {
            // A first launch with no saved position must not default to
            // screen-center — that's exactly where the main window (and
            // its in-app answer panel) sits, so a centered overlay would
            // cover it in normal (non-screen-share) app mode. Anchoring
            // to a corner instead keeps the overlay non-obstructive by
            // default; the user can still drag it anywhere afterward,
            // and that position is what gets saved/restored from then on.
            if let visibleFrame = panel.screen?.visibleFrame ?? NSScreen.main?.visibleFrame {
                let margin: CGFloat = 24
                let origin = NSPoint(
                    x: visibleFrame.maxX - initialSize.width - margin,
                    y: visibleFrame.minY + margin
                )
                panel.setFrameOrigin(origin)
            } else {
                panel.center()
            }
        }

        let rootView = OverlayView(conversationState: conversationState, privacyManager: privacyManager, controller: self)
        panel.contentView = NSHostingView(rootView: rootView)
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    /// Exposes the managed panel read-only so other subsystems (Presenter
    /// Privacy) can act on the Veya-owned overlay window without this
    /// controller needing to know anything about privacy/capture.
    var managedWindow: NSWindow? { window }

    // MARK: - Visibility

    func show() {
        window?.orderFrontRegardless()
    }

    func hide() {
        window?.orderOut(nil)
    }

    func toggleVisibility() {
        guard let window else { return }
        window.isVisible ? hide() : show()
    }

    // MARK: - Preferences

    func toggleCompactMode() {
        preferences.compactMode.toggle()
        applyCompactMode()
        persist()
    }

    func setOpacity(_ opacity: Double) {
        let clamped = min(max(opacity, 0.2), 1.0)
        preferences.opacity = clamped
        window?.alphaValue = clamped
        persist()
    }

    func setAlwaysOnTop(_ alwaysOnTop: Bool) {
        preferences.alwaysOnTop = alwaysOnTop
        window?.level = alwaysOnTop ? .floating : .normal
        persist()
    }

    private func applyCompactMode() {
        guard let window else { return }
        let size = preferences.compactMode ? Self.compactSize : Self.expandedSize
        var frame = window.frame
        frame.origin.y += frame.size.height - size.height
        frame.size = size
        window.setFrame(frame, display: true, animate: true)
    }

    private func persist() {
        preferencesStore.save(preferences)
    }
}
