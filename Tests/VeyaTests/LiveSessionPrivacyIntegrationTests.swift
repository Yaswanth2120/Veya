import AppKit
import Foundation
import Testing
@testable import Veya

@MainActor
@Suite("Live Session privacy integration")
struct LiveSessionPrivacyIntegrationTests {
    private func makeCoordinator(tester: MockCaptureCompatibilityTester = MockCaptureCompatibilityTester()) -> AppCoordinator {
        let suiteName = "com.veya.tests.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suiteName)!

        let privacyManager = PresenterPrivacyManager(
            preferencesStore: PresenterPrivacyPreferencesStore(defaults: defaults),
            compatibilityTester: tester,
            displayManager: DisplayManager(),
            safeShareManager: SafeShareManager(
                engine: MockSafeShareCapturing(),
                makeWindowController: { MockSafeShareWindowController() }
            ),
            historyRepository: CaptureCompatibilityRepository(db: DatabaseManager.makeInMemory())
        )
        // AppCoordinator.init overwrites the overlay window provider with
        // its own (window-only-during-a-live-session) wiring, which is the
        // correct production behavior — tests that need a compatibility
        // test to succeed *before* any session has started re-point the
        // provider afterward, simulating "a window was available when this
        // check last ran."
        let coordinator = AppCoordinator(
            sessionRepository: SessionRepository(db: DatabaseManager.makeInMemory()),
            presenterPrivacyManager: privacyManager
        )
        privacyManager.setOverlayWindowProvider {
            NSWindow(contentRect: NSRect(x: 0, y: 0, width: 100, height: 100), styleMask: [.borderless], backing: .buffered, defer: false)
        }
        return coordinator
    }

    @Test("privacy disabled: starts the Live Session immediately, no prompt")
    func privacyDisabled() {
        let coordinator = makeCoordinator()
        let session = Session.makeTestSession(title: "Disabled")

        coordinator.requestStartLiveSession(for: session)

        #expect(coordinator.pendingPrivacyPrompt == nil)
        #expect(coordinator.route == .liveSession)
    }

    @Test("direct mode, verified: starts immediately, no prompt")
    func directVerified() async throws {
        let tester = MockCaptureCompatibilityTester()
        tester.resultToReturn = MockCaptureCompatibilityTester.makeResult(status: .verified)
        let coordinator = makeCoordinator(tester: tester)
        await coordinator.presenterPrivacyManager.enable()
        await coordinator.presenterPrivacyManager.selectMode(.directPrivateOverlay)
        _ = try await coordinator.presenterPrivacyManager.runCompatibilityTest()

        let session = Session.makeTestSession(title: "Direct Verified")
        coordinator.requestStartLiveSession(for: session)

        #expect(coordinator.pendingPrivacyPrompt == nil)
        #expect(coordinator.route == .liveSession)
    }

    @Test("direct mode, unverified: surfaces a prompt instead of starting")
    func directUnverified() async {
        let coordinator = makeCoordinator()
        await coordinator.presenterPrivacyManager.enable()
        await coordinator.presenterPrivacyManager.selectMode(.directPrivateOverlay)
        // No test has been run — status is still .notTested.

        let session = Session.makeTestSession(title: "Direct Unverified")
        coordinator.requestStartLiveSession(for: session)

        #expect(coordinator.pendingPrivacyPrompt?.id == "confirmUnverifiedDirectOverlay")
        #expect(coordinator.route != .liveSession)
    }

    @Test("safe share mode, already running: starts immediately, no prompt")
    func safeShareRunning() async throws {
        let coordinator = makeCoordinator()
        await coordinator.presenterPrivacyManager.enable()
        await coordinator.presenterPrivacyManager.selectMode(.safeShare)
        try await coordinator.presenterPrivacyManager.startSafeShare()

        let session = Session.makeTestSession(title: "Safe Share Running")
        coordinator.requestStartLiveSession(for: session)

        #expect(coordinator.pendingPrivacyPrompt == nil)
        #expect(coordinator.route == .liveSession)
    }

    @Test("safe share mode, not running: surfaces a prompt instead of starting")
    func safeShareNotRunning() async {
        let coordinator = makeCoordinator()
        await coordinator.presenterPrivacyManager.enable()
        await coordinator.presenterPrivacyManager.selectMode(.safeShare)

        let session = Session.makeTestSession(title: "Safe Share Not Running")
        coordinator.requestStartLiveSession(for: session)

        #expect(coordinator.pendingPrivacyPrompt?.id == "confirmStartSafeShare")
        #expect(coordinator.route != .liveSession)
    }

    @Test("continuing without the recommended privacy action still starts the session")
    func continueWithoutPrivacyAction() {
        let coordinator = makeCoordinator()
        let session = Session.makeTestSession(title: "Continue Anyway")

        coordinator.continueLiveSessionWithoutPrivacyAction(session)

        #expect(coordinator.pendingPrivacyPrompt == nil)
        #expect(coordinator.route == .liveSession)
    }
}
