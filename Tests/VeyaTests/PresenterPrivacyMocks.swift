import AppKit
import Foundation
@testable import Veya

/// Test-only mock of `CaptureCompatibilityTesting`. `@unchecked Sendable`
/// is acceptable here because each test drives one mock from a single
/// task; there is no real concurrent access to guard against.
final class MockCaptureCompatibilityTester: CaptureCompatibilityTesting, @unchecked Sendable {
    var resultToReturn: CaptureTestResult?
    var errorToThrow: Error?
    private(set) var callCount = 0

    func test(overlayWindow: NSWindow, displayID: UInt32) async throws -> CaptureTestResult {
        callCount += 1
        if let errorToThrow { throw errorToThrow }
        guard let resultToReturn else {
            fatalError("MockCaptureCompatibilityTester used without configuring resultToReturn or errorToThrow")
        }
        return resultToReturn
    }

    static func makeResult(status: PresenterPrivacyStatus, displayID: UInt32 = 1) -> CaptureTestResult {
        CaptureTestResult(
            id: UUID(),
            testedAt: Date(),
            macOSVersion: "test",
            appVersion: "test",
            displayID: displayID,
            overlayDetected: status == .overlayDetected,
            confidence: 1.0,
            status: status,
            diagnosticMessage: "mock result"
        )
    }
}

/// Test-only mock of `SafeShareCapturing` — no real `ScreenCaptureKit` I/O,
/// no Screen Recording permission required.
final class MockSafeShareCapturing: SafeShareCapturing, @unchecked Sendable {
    var errorToThrow: Error?
    private(set) var startCallCount = 0
    private(set) var stopCallCount = 0
    private var continuation: AsyncStream<SafeShareFrame>.Continuation?

    func start(displayID: UInt32, quality: SafeShareQuality, fps: Int) async throws -> AsyncStream<SafeShareFrame> {
        startCallCount += 1
        if let errorToThrow {
            throw errorToThrow
        }
        let (stream, continuation) = AsyncStream<SafeShareFrame>.makeStream(bufferingPolicy: .bufferingNewest(1))
        self.continuation = continuation
        return stream
    }

    func stop() async {
        stopCallCount += 1
        continuation?.finish()
        continuation = nil
    }

    func diagnostics() async -> SafeShareDiagnostics {
        SafeShareDiagnostics(
            isRunning: continuation != nil,
            configuredFPS: 30,
            streamWidth: 0,
            streamHeight: 0,
            framesReceived: 0,
            framesDropped: 0
        )
    }

    func finishStream() {
        continuation?.finish()
    }
}

/// Test-only mock of `SafeShareWindowControlling` — no real `NSWindow`.
@MainActor
final class MockSafeShareWindowController: SafeShareWindowControlling {
    private(set) var showCallCount = 0
    private(set) var hideCallCount = 0
    private(set) var renderCallCount = 0

    func show() { showCallCount += 1 }
    func hide() { hideCallCount += 1 }
    func render(_ frame: SafeShareFrame) { renderCallCount += 1 }
}
