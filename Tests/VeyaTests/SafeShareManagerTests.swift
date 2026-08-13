import Testing
@testable import Veya

@MainActor
@Suite("SafeShareManager")
struct SafeShareManagerTests {
    private func makeManager() -> (SafeShareManager, MockSafeShareCapturing, MockSafeShareWindowController) {
        let engine = MockSafeShareCapturing()
        let windowController = MockSafeShareWindowController()
        let manager = SafeShareManager(engine: engine, makeWindowController: { windowController })
        return (manager, engine, windowController)
    }

    @Test("start shows the window and marks isRunning true")
    func start() async throws {
        let (manager, engine, windowController) = makeManager()

        try await manager.start(displayID: 1, quality: .balanced, fps: 30)

        #expect(manager.isRunning)
        #expect(engine.startCallCount == 1)
        #expect(windowController.showCallCount == 1)
    }

    @Test("stop hides the window and marks isRunning false")
    func stop() async throws {
        let (manager, engine, windowController) = makeManager()
        try await manager.start(displayID: 1, quality: .balanced, fps: 30)

        await manager.stop()

        #expect(!manager.isRunning)
        #expect(engine.stopCallCount == 1)
        #expect(windowController.hideCallCount == 1)
    }

    @Test("stop when not running is a harmless no-op")
    func stopWhenNotRunning() async {
        let (manager, engine, _) = makeManager()
        await manager.stop()
        #expect(engine.stopCallCount == 0)
    }

    @Test("double start throws safeShareAlreadyRunning and does not start a second capture")
    func doubleStart() async throws {
        let (manager, engine, _) = makeManager()
        try await manager.start(displayID: 1, quality: .balanced, fps: 30)

        await #expect(throws: PresenterPrivacyError.safeShareAlreadyRunning) {
            try await manager.start(displayID: 1, quality: .balanced, fps: 30)
        }
        #expect(engine.startCallCount == 1)
    }

    @Test("permission denied surfaces as lastError and does not mark isRunning")
    func permissionDenied() async {
        let (manager, _, _) = makeManager()
        let engine = MockSafeShareCapturing()
        engine.errorToThrow = PresenterPrivacyError.screenCapturePermissionDenied
        let deniedManager = SafeShareManager(engine: engine, makeWindowController: { MockSafeShareWindowController() })

        await #expect(throws: PresenterPrivacyError.screenCapturePermissionDenied) {
            try await deniedManager.start(displayID: 1, quality: .balanced, fps: 30)
        }
        #expect(!deniedManager.isRunning)
        #expect(deniedManager.lastError == .screenCapturePermissionDenied)
        _ = manager
    }

    @Test("selected display unavailable surfaces as lastError")
    func selectedDisplayUnavailable() async {
        let engine = MockSafeShareCapturing()
        engine.errorToThrow = PresenterPrivacyError.displayUnavailable
        let manager = SafeShareManager(engine: engine, makeWindowController: { MockSafeShareWindowController() })

        await #expect(throws: PresenterPrivacyError.displayUnavailable) {
            try await manager.start(displayID: 999, quality: .balanced, fps: 30)
        }
        #expect(!manager.isRunning)
    }

    @Test("a stream failure (engine finishes the frame stream on its own) ends the session")
    func streamFailureEndsSession() async throws {
        let (manager, engine, windowController) = makeManager()
        try await manager.start(displayID: 1, quality: .balanced, fps: 30)
        #expect(manager.isRunning)

        engine.finishStream()

        // The frame-consumer loop notices the stream ending asynchronously
        // — poll briefly instead of a single fixed sleep to avoid flakiness.
        for _ in 0..<50 {
            if !manager.isRunning { break }
            try? await Task.sleep(nanoseconds: 20_000_000)
        }

        #expect(!manager.isRunning)
        #expect(windowController.hideCallCount == 1)
    }
}

extension PresenterPrivacyError: Equatable {
    public static func == (lhs: PresenterPrivacyError, rhs: PresenterPrivacyError) -> Bool {
        lhs.errorDescription == rhs.errorDescription
    }
}
