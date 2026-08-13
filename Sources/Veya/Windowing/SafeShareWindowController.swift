import AppKit
import SwiftUI

/// Abstraction over the Safe Share window so `SafeShareManager` is
/// testable without creating a real `NSWindow`.
@MainActor
protocol SafeShareWindowControlling: AnyObject {
    func show()
    func hide()
    func render(_ frame: SafeShareFrame)
}

/// The clean, shareable "Veya Safe Share" window — a standard titled
/// `NSWindow` (not a panel, not borderless) so it shows up like any other
/// window in a meeting app's window picker. Rendering itself
/// (`SafeShareDisplayNSView`/`SafeShareView`) lives in `UI/SafeShare/` —
/// this class only owns window presentation.
@MainActor
final class SafeShareWindowController: NSWindowController, SafeShareWindowControlling {
    private let displayView = SafeShareDisplayNSView()

    init() {
        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 960, height: 600),
            styleMask: [.titled, .closable, .resizable, .miniaturizable],
            backing: .buffered,
            defer: false
        )
        window.title = "Veya Safe Share"
        window.minSize = NSSize(width: 320, height: 200)
        super.init(window: window)

        window.setFrameAutosaveName("VeyaSafeShareWindow")
        window.contentView = NSHostingView(rootView: SafeShareView(displayView: displayView))
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    func show() {
        window?.makeKeyAndOrderFront(nil)
    }

    func hide() {
        window?.orderOut(nil)
    }

    nonisolated func render(_ frame: SafeShareFrame) {
        Task { @MainActor in
            displayView.enqueue(frame.sampleBuffer)
        }
    }
}
