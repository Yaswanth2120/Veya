import AppKit
import Combine

/// Snapshot of one active display, keyed by its `CGDirectDisplayID` (the
/// same ID `ScreenCaptureKit`'s `SCDisplay` uses, so `DisplayManager` output
/// can be handed straight to `SafeShareCaptureEngine`/
/// `CaptureCompatibilityTester`).
struct DisplayInfo: Identifiable, Equatable, Sendable {
    let id: UInt32
    let name: String
    let frame: CGRect
    let visibleFrame: CGRect
    let scaleFactor: CGFloat
    let isBuiltIn: Bool
}

/// Enumerates active displays and tracks topology changes (connect,
/// disconnect, resolution change). Read by `PresenterPrivacyManager` to
/// pick a default display and by the privacy/Safe Share UI to populate a
/// display picker.
@MainActor
final class DisplayManager: ObservableObject {
    @Published private(set) var displays: [DisplayInfo] = []

    private nonisolated(unsafe) var screenParametersObserver: NSObjectProtocol?

    init() {
        refresh()
        screenParametersObserver = NotificationCenter.default.addObserver(
            forName: NSApplication.didChangeScreenParametersNotification,
            object: nil,
            queue: .main
        ) { [weak self] _ in
            Task { @MainActor in
                self?.refresh()
            }
        }
    }

    deinit {
        if let screenParametersObserver {
            NotificationCenter.default.removeObserver(screenParametersObserver)
        }
    }

    /// Re-enumerates `NSScreen.screens`. Safe to call at any time; also
    /// called automatically on `didChangeScreenParametersNotification`
    /// (display connect/disconnect/resolution/arrangement change).
    func refresh() {
        displays = NSScreen.screens.compactMap(DisplayManager.makeDisplayInfo)
    }

    var builtInDisplay: DisplayInfo? {
        displays.first { $0.isBuiltIn }
    }

    func display(id: UInt32) -> DisplayInfo? {
        displays.first { $0.id == id }
    }

    /// Resolves the display Presenter Privacy should target: the user's
    /// preferred display if it's still connected, falling back to the
    /// built-in display, falling back to the first available display.
    func preferredDisplay(preferredDisplayID: UInt32?) -> DisplayInfo? {
        Self.selectPreferredDisplay(from: displays, preferredDisplayID: preferredDisplayID)
    }

    /// Pure selection logic, extracted so it can be unit tested against
    /// synthetic `DisplayInfo` lists without touching the real
    /// `NSScreen.screens` this manager otherwise depends on.
    nonisolated static func selectPreferredDisplay(from displays: [DisplayInfo], preferredDisplayID: UInt32?) -> DisplayInfo? {
        if let preferredDisplayID, let match = displays.first(where: { $0.id == preferredDisplayID }) {
            return match
        }
        return displays.first(where: \.isBuiltIn) ?? displays.first
    }

    /// The display a given window's frame is mostly contained in — used to
    /// find "which display is Veya's overlay currently on."
    func display(containing window: NSWindow) -> DisplayInfo? {
        guard let screen = window.screen, let info = DisplayManager.makeDisplayInfo(from: screen) else {
            return nil
        }
        return info
    }

    private static func makeDisplayInfo(from screen: NSScreen) -> DisplayInfo? {
        guard let screenNumber = screen.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? NSNumber else {
            return nil
        }
        let displayID = CGDirectDisplayID(screenNumber.uint32Value)
        return DisplayInfo(
            id: displayID,
            name: screen.localizedName,
            frame: screen.frame,
            visibleFrame: screen.visibleFrame,
            scaleFactor: screen.backingScaleFactor,
            isBuiltIn: CGDisplayIsBuiltin(displayID) != 0
        )
    }
}
