import AppKit

/// Top-level Presenter Privacy coordinator: owns preferences, the current
/// verification `status`, and delegates the two privacy paths to dedicated
/// components — `CaptureCompatibilityTester` for Direct Private Overlay
/// verification, `SafeShareManager` for Safe Share. Does not implement
/// capture, rendering, or window management itself.
@MainActor
final class PresenterPrivacyManager: ObservableObject {
    @Published private(set) var preferences: PresenterPrivacyPreferences
    @Published private(set) var status: PresenterPrivacyStatus
    @Published private(set) var lastTestResult: CaptureTestResult?
    @Published private(set) var history: [CaptureCompatibilityRecord] = []

    let displayManager: DisplayManager
    let safeShareManager: SafeShareManager

    var isSafeShareRunning: Bool { safeShareManager.isRunning }

    private let preferencesStore: PresenterPrivacyPreferencesStore
    private let compatibilityTester: any CaptureCompatibilityTesting
    private let historyRepository: CaptureCompatibilityRepository

    /// Supplies the Veya-owned overlay window to test/configure. Wired by
    /// `AppCoordinator` to `overlayWindowController?.managedWindow` — `nil`
    /// whenever no Live Session overlay currently exists.
    private var overlayWindowProvider: (@MainActor () -> NSWindow?)?

    init(
        preferencesStore: PresenterPrivacyPreferencesStore = PresenterPrivacyPreferencesStore(),
        compatibilityTester: any CaptureCompatibilityTesting = CaptureCompatibilityTester(),
        displayManager: DisplayManager = DisplayManager(),
        safeShareManager: SafeShareManager = SafeShareManager(),
        historyRepository: CaptureCompatibilityRepository = CaptureCompatibilityRepository()
    ) {
        self.preferencesStore = preferencesStore
        let loaded = preferencesStore.load()
        self.preferences = loaded
        self.status = loaded.enabled ? .notTested : .disabled
        self.compatibilityTester = compatibilityTester
        self.displayManager = displayManager
        self.safeShareManager = safeShareManager
        self.historyRepository = historyRepository
    }

    func setOverlayWindowProvider(_ provider: @escaping @MainActor () -> NSWindow?) {
        overlayWindowProvider = provider
    }

    // MARK: - Enable / mode

    func enable() async {
        guard !preferences.enabled else { return }
        preferences.enabled = true
        status = .notTested
        persist()
        PrivacyLog.info("presenter privacy enabled")
    }

    func disable() {
        guard preferences.enabled else { return }
        preferences.enabled = false
        status = .disabled
        persist()
        PrivacyLog.info("presenter privacy disabled")
    }

    func selectMode(_ mode: PresenterPrivacyMode) async {
        guard preferences.selectedMode != mode else { return }
        preferences.selectedMode = mode
        persist()
        PrivacyLog.info("privacy mode changed to \(mode.rawValue)")
        applyWindowSharingPolicy()
    }

    /// Best-effort static capability check (no capture performed): are we
    /// even running on a macOS version with `ScreenCaptureKit`, and is an
    /// overlay window currently available to test against. Does not set
    /// `status` to `verified`/`overlayDetected` — only
    /// `runCompatibilityTest()` can do that.
    func evaluateSupport() async {
        guard preferences.enabled else {
            status = .disabled
            return
        }
        if overlayWindowProvider?() == nil {
            status = .notTested
        }
    }

    // MARK: - Direct Private Overlay

    /// Applies (or clears) the best-effort legacy `NSWindowSharingType`
    /// configuration on the *current* overlay window to match
    /// `preferences.selectedMode`. Successfully setting this property is
    /// NOT proof the window is excluded from any particular capture
    /// pipeline — only `runCompatibilityTest()`'s actual measured result
    /// determines `status`. See docs/PRESENTER_PRIVACY.md "Why direct
    /// exclusion is best-effort".
    ///
    /// Called from three places, so the window's actual sharing type never
    /// drifts from the selected mode: when the mode changes, when a
    /// compatibility test is about to run, and whenever `AppCoordinator`
    /// creates a *new* overlay window (a fresh `OverlayWindowController` is
    /// built per Live Session — without this, a window created after the
    /// user already selected Direct Private Overlay would silently keep
    /// AppKit's default `.readOnly` sharing type).
    func applyWindowSharingPolicy() {
        guard let window = overlayWindowProvider?() else { return }
        switch preferences.selectedMode {
        case .directPrivateOverlay:
            window.sharingType = .none
            PrivacyLog.info("direct privacy configured (best-effort)")
        case .normal, .safeShare:
            // Best-effort restore. Verified empirically: once an NSWindow's
            // sharingType has been set to .none, AppKit does not honor
            // setting it back to .readOnly on that *same* window instance
            // — the window server treats .none as sticky. This call is
            // still correct and takes effect for windows that never had
            // .none applied (e.g. Normal → Safe Share). For a window that
            // *did* have Direct Private Overlay applied, it stays excluded
            // from sharing until a new overlay window is created (Veya
            // creates a fresh one per Live Session), which arguably fails
            // safe rather than silently un-hiding a window the user
            // previously marked private. See docs/PRESENTER_PRIVACY.md
            // "Known limitations."
            window.sharingType = .readOnly
        }
    }

    /// Call whenever a new overlay window becomes available (i.e. right
    /// after `AppCoordinator` creates a fresh `OverlayWindowController`),
    /// so a window created while Direct Private Overlay is already
    /// selected gets the policy applied immediately rather than only on
    /// the next mode change or manual test.
    func overlayWindowDidBecomeAvailable() {
        applyWindowSharingPolicy()
    }

    func runCompatibilityTest() async throws -> CaptureTestResult {
        guard let window = overlayWindowProvider?() else {
            throw PresenterPrivacyError.noOverlayWindow
        }
        guard let displayInfo = displayManager.preferredDisplay(preferredDisplayID: preferences.preferredDisplayID) else {
            throw PresenterPrivacyError.displayUnavailable
        }

        applyWindowSharingPolicy()

        status = .testing
        PrivacyLog.info("compatibility test started")

        do {
            let result = try await compatibilityTester.test(overlayWindow: window, displayID: displayInfo.id)
            status = result.status
            lastTestResult = result
            PrivacyLog.info("compatibility test completed: \(result.status.rawValue)")
            await recordHistory(result: result)
            return result
        } catch {
            status = .error
            PrivacyLog.error("compatibility test failed, errorType=\(String(reflecting: type(of: error)))")
            throw error
        }
    }

    /// True when Direct Private Overlay's last known result means Safe
    /// Share should be suggested instead — see build prompt §5.21.
    var shouldRecommendSafeShare: Bool {
        guard preferences.selectedMode == .directPrivateOverlay else { return false }
        switch status {
        case .overlayDetected, .uncertain, .unsupported:
            return true
        default:
            return false
        }
    }

    // MARK: - Safe Share

    func startSafeShare() async throws {
        guard let displayInfo = displayManager.preferredDisplay(preferredDisplayID: preferences.preferredDisplayID) else {
            throw PresenterPrivacyError.displayUnavailable
        }
        let quality = SafeShareQuality.allCases.first { $0.suggestedFPS == preferences.safeShareFPS } ?? .balanced
        try await safeShareManager.start(displayID: displayInfo.id, quality: quality, fps: preferences.safeShareFPS)
        PrivacyLog.info("Safe Share started")
    }

    func stopSafeShare() async {
        await safeShareManager.stop()
        PrivacyLog.info("Safe Share stopped")
    }

    // MARK: - History

    func loadHistory() async {
        history = (try? await historyRepository.fetchAll()) ?? []
    }

    private func recordHistory(result: CaptureTestResult) async {
        let record = CaptureCompatibilityRecord(
            id: UUID(),
            testedAt: result.testedAt,
            macOSVersion: result.macOSVersion,
            veyaVersion: result.appVersion,
            displayID: result.displayID,
            mode: preferences.selectedMode,
            result: result
        )
        try? await historyRepository.save(record)
        await loadHistory()
    }

    // MARK: - Preferences

    func setPreferredDisplay(_ displayID: UInt32?) {
        preferences.preferredDisplayID = displayID
        persist()
    }

    func setSafeShareFPS(_ fps: Int) {
        preferences.safeShareFPS = fps
        persist()
    }

    func setRunTestBeforeSession(_ value: Bool) {
        preferences.runTestBeforeSession = value
        persist()
    }

    func setWarnWhenUnverified(_ value: Bool) {
        preferences.warnWhenUnverified = value
        persist()
    }

    private func persist() {
        preferencesStore.save(preferences)
    }
}
