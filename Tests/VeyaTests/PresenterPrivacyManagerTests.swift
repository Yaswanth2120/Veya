import AppKit
import Foundation
import Testing
@testable import Veya

@MainActor
@Suite("PresenterPrivacyManager")
struct PresenterPrivacyManagerTests {
    private func makeManager(tester: MockCaptureCompatibilityTester = MockCaptureCompatibilityTester()) -> PresenterPrivacyManager {
        let suiteName = "com.veya.tests.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suiteName)!

        let manager = PresenterPrivacyManager(
            preferencesStore: PresenterPrivacyPreferencesStore(defaults: defaults),
            compatibilityTester: tester,
            displayManager: DisplayManager(),
            safeShareManager: SafeShareManager(
                engine: MockSafeShareCapturing(),
                makeWindowController: { MockSafeShareWindowController() }
            ),
            historyRepository: CaptureCompatibilityRepository(db: DatabaseManager.makeInMemory())
        )
        manager.setOverlayWindowProvider {
            NSWindow(contentRect: NSRect(x: 0, y: 0, width: 100, height: 100), styleMask: [.borderless], backing: .buffered, defer: false)
        }
        return manager
    }

    @Test("disabled to enabled")
    func disabledToEnabled() async {
        let manager = makeManager()
        #expect(manager.status == .disabled)

        await manager.enable()

        #expect(manager.preferences.enabled)
        #expect(manager.status == .notTested)
    }

    @Test("normal to direct private overlay")
    func normalToDirect() async {
        let manager = makeManager()
        await manager.enable()
        #expect(manager.preferences.selectedMode == .safeShare)

        await manager.selectMode(.directPrivateOverlay)

        #expect(manager.preferences.selectedMode == .directPrivateOverlay)
    }

    @Test("normal to safe share")
    func normalToSafeShare() async {
        let manager = makeManager()
        await manager.enable()
        await manager.selectMode(.normal)

        await manager.selectMode(.safeShare)

        #expect(manager.preferences.selectedMode == .safeShare)
    }

    @Test("direct to safe share")
    func directToSafeShare() async {
        let manager = makeManager()
        await manager.enable()
        await manager.selectMode(.directPrivateOverlay)

        await manager.selectMode(.safeShare)

        #expect(manager.preferences.selectedMode == .safeShare)
    }

    @Test("testing to verified")
    func testingToVerified() async throws {
        let tester = MockCaptureCompatibilityTester()
        tester.resultToReturn = MockCaptureCompatibilityTester.makeResult(status: .verified)
        let manager = makeManager(tester: tester)
        await manager.enable()

        let result = try await manager.runCompatibilityTest()

        #expect(result.status == .verified)
        #expect(manager.status == .verified)
        #expect(manager.lastTestResult?.status == .verified)
    }

    @Test("testing to overlayDetected")
    func testingToOverlayDetected() async throws {
        let tester = MockCaptureCompatibilityTester()
        tester.resultToReturn = MockCaptureCompatibilityTester.makeResult(status: .overlayDetected)
        let manager = makeManager(tester: tester)
        await manager.enable()

        _ = try await manager.runCompatibilityTest()

        #expect(manager.status == .overlayDetected)
    }

    @Test("testing to uncertain")
    func testingToUncertain() async throws {
        let tester = MockCaptureCompatibilityTester()
        tester.resultToReturn = MockCaptureCompatibilityTester.makeResult(status: .uncertain)
        let manager = makeManager(tester: tester)
        await manager.enable()

        _ = try await manager.runCompatibilityTest()

        #expect(manager.status == .uncertain)
    }

    @Test("a thrown error from the tester sets status to error and propagates")
    func errorHandling() async {
        let tester = MockCaptureCompatibilityTester()
        tester.errorToThrow = PresenterPrivacyError.screenCapturePermissionDenied
        let manager = makeManager(tester: tester)
        await manager.enable()

        await #expect(throws: PresenterPrivacyError.self) {
            _ = try await manager.runCompatibilityTest()
        }
        #expect(manager.status == .error)
    }

    @Test("running a compatibility test with no overlay window throws noOverlayWindow")
    func noOverlayWindowThrows() async {
        let manager = PresenterPrivacyManager(
            preferencesStore: PresenterPrivacyPreferencesStore(defaults: UserDefaults(suiteName: "com.veya.tests.\(UUID().uuidString)")!),
            compatibilityTester: MockCaptureCompatibilityTester(),
            displayManager: DisplayManager(),
            safeShareManager: SafeShareManager(engine: MockSafeShareCapturing(), makeWindowController: { MockSafeShareWindowController() }),
            historyRepository: CaptureCompatibilityRepository(db: DatabaseManager.makeInMemory())
        )
        // No overlayWindowProvider configured.
        await manager.enable()

        await #expect(throws: PresenterPrivacyError.noOverlayWindow) {
            _ = try await manager.runCompatibilityTest()
        }
    }

    @Test("shouldRecommendSafeShare is true only for direct mode with a poor result")
    func shouldRecommendSafeShare() async throws {
        let tester = MockCaptureCompatibilityTester()
        tester.resultToReturn = MockCaptureCompatibilityTester.makeResult(status: .overlayDetected)
        let manager = makeManager(tester: tester)
        await manager.enable()
        await manager.selectMode(.directPrivateOverlay)

        _ = try await manager.runCompatibilityTest()

        #expect(manager.shouldRecommendSafeShare)

        await manager.selectMode(.safeShare)
        #expect(!manager.shouldRecommendSafeShare)
    }

    @Test("selecting direct mode before any overlay window exists still applies the policy once a window appears")
    func policyAppliedToWindowCreatedAfterModeSelection() async {
        let manager = makeManager()
        // No overlayWindowProvider configured yet — simulates selecting
        // the mode before a Live Session (and its overlay) has started.

        await manager.enable()
        await manager.selectMode(.directPrivateOverlay)

        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 100, height: 100), styleMask: [.borderless], backing: .buffered, defer: false)
        #expect(window.sharingType == .readOnly) // AppKit's default

        manager.setOverlayWindowProvider { window }
        manager.overlayWindowDidBecomeAvailable()

        #expect(window.sharingType == .none)
    }

    @Test("switching to safe share on a window that was never marked direct-private applies readOnly")
    func switchingToSafeShareOnFreshWindowAppliesReadOnly() async {
        // A fresh window (never had .none applied) — see the "sticky
        // .none" test below for why a window that *was* marked private
        // behaves differently.
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 100, height: 100), styleMask: [.borderless], backing: .buffered, defer: false)
        let manager = makeManager()
        manager.setOverlayWindowProvider { window }

        await manager.enable()
        // Default mode is .safeShare, which is a no-op via the `guard
        // preferences.selectedMode != mode` early return in selectMode, so
        // explicitly re-select .normal first to force a real transition.
        await manager.selectMode(.normal)

        #expect(window.sharingType == .readOnly)
    }

    /// Verified empirically (see `applyWindowSharingPolicy`'s doc comment):
    /// once `NSWindow.sharingType` is set to `.none`, AppKit does not honor
    /// setting it back to `.readOnly` on that same window instance — this
    /// is a real platform constraint, not a bug in `PresenterPrivacyManager`.
    /// Veya's mitigation is that a fresh `NSWindow` is created per Live
    /// Session, so the very next session's overlay starts at the correct
    /// default regardless.
    @Test("a window already marked direct-private stays excluded even after switching mode away")
    func windowMarkedNoneStaysExcludedAfterModeSwitch() async {
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 100, height: 100), styleMask: [.borderless], backing: .buffered, defer: false)
        let manager = makeManager()
        manager.setOverlayWindowProvider { window }

        await manager.enable()
        await manager.selectMode(.directPrivateOverlay)
        #expect(window.sharingType == .none)

        await manager.selectMode(.safeShare)

        // PresenterPrivacyManager did attempt to restore .readOnly — this
        // assertion documents that AppKit doesn't let that attempt succeed
        // on this window instance, not that Veya failed to try.
        #expect(window.sharingType == .none)
    }
}
