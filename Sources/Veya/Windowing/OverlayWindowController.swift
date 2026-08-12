import AppKit
import SwiftUI

/// Owns the floating overlay panel: a borderless, non-activating `NSPanel`
/// at `.floating` level, hosting the SwiftUI `OverlayView`. Handles
/// show/hide, compact/expanded resizing, opacity, and frame persistence.
///
/// Nothing here touches capture exclusion, window sharing type, or any
/// other presenter-privacy behavior — that's a separate subsystem being
/// built outside this prompt. Only documented, standard `NSPanel`/
/// `NSWindow` APIs are used.
@MainActor
final class OverlayWindowController: NSWindowController {
    private let preferencesStore: OverlayPreferencesStore
    private(set) var preferences: OverlayPreferences

    private static let frameAutosaveName = "VeyaOverlayPanel"
    private static let compactSize = NSSize(width: 380, height: 190)
    private static let expandedSize = NSSize(width: 440, height: 440)

    init(
        conversationState: ConversationState,
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

        // TODO: presenter-privacy — out of scope, see project owner.
        // (Capture-exclusion / screen-share visibility behavior belongs to
        // a separate subsystem, not to standard window-level setup here.)

        super.init(window: panel)

        panel.setFrameAutosaveName(Self.frameAutosaveName)
        if !panel.setFrameUsingName(Self.frameAutosaveName) {
            panel.center()
        }

        let rootView = OverlayView(conversationState: conversationState, controller: self)
        panel.contentView = NSHostingView(rootView: rootView)
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

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
