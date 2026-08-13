import Foundation
@testable import Veya

/// A deterministic, in-memory `AudioCapturing` for testing without any
/// real microphone/AVFoundation involvement. Tests push chunks in via
/// `simulateChunk(_:)` (as if the hardware tap produced them) after
/// `start()` has been called, and can make `start()` throw to simulate
/// engine-start failures.
final class FakeAudioCapture: AudioCapturing, @unchecked Sendable {
    private let lock = NSLock()
    private var continuation: AsyncStream<AudioChunk>.Continuation?
    private var cachedStream: AsyncStream<AudioChunk>?

    private(set) var startCallCount = 0
    private(set) var stopCallCount = 0
    private(set) var _droppedChunkCount = 0
    var startError: AudioCaptureError?
    /// Called synchronously the instant `start()` is entered — lets a
    /// test know precisely when it's safe to simulate an external event
    /// (e.g. killing the worker process) that must land *during*
    /// `start()`, before it returns.
    var onStartBegan: (@Sendable () -> Void)?
    /// If set, `start()` suspends here (after calling `onStartBegan`)
    /// until the gate is opened — makes the "something happens while
    /// audio capture is starting" window deterministic instead of
    /// timing-dependent.
    var startGate: SendGate?

    var droppedChunkCount: Int {
        withLock { _droppedChunkCount }
    }

    /// Calling `NSLock.lock()`/`unlock()` directly inside an `async`
    /// function body is flagged as unavailable under strict concurrency —
    /// wrapping the critical section in a synchronous helper sidesteps
    /// that (same pattern as `MicrophoneAudioCapture`'s `withLock`).
    private func withLock<T>(_ body: () -> T) -> T {
        lock.lock()
        defer { lock.unlock() }
        return body()
    }

    func chunks() -> AsyncStream<AudioChunk> {
        withLock {
            if let cachedStream { return cachedStream }
            let (stream, continuation) = AsyncStream<AudioChunk>.makeStream()
            self.continuation = continuation
            self.cachedStream = stream
            return stream
        }
    }

    func start() async throws {
        onStartBegan?()
        if let startGate {
            await startGate.waitUntilOpened()
        }
        let error = withLock {
            startCallCount += 1
            return startError
        }
        if let error {
            throw error
        }
    }

    func stop() async {
        let continuation = withLock {
            stopCallCount += 1
            let previous = self.continuation
            self.continuation = nil
            self.cachedStream = nil
            return previous
        }
        continuation?.finish()
    }

    func simulateChunk(_ chunk: AudioChunk) {
        let continuation = withLock { self.continuation }
        continuation?.yield(chunk)
    }

    func simulateDroppedChunk() {
        withLock { _droppedChunkCount += 1 }
    }
}

/// A deterministic `MicrophonePermissionChecking` for tests — never
/// touches `AVCaptureDevice`, never prompts.
final class FakeMicrophonePermission: MicrophonePermissionChecking, @unchecked Sendable {
    private let lock = NSLock()
    private var _currentStatus: MicrophoneAuthorizationState
    private(set) var requestAccessCallCount = 0

    var currentStatus: MicrophoneAuthorizationState {
        withLock { _currentStatus }
    }

    init(status: MicrophoneAuthorizationState = .undetermined) {
        self._currentStatus = status
    }

    private func withLock<T>(_ body: () -> T) -> T {
        lock.lock()
        defer { lock.unlock() }
        return body()
    }

    func setStatus(_ status: MicrophoneAuthorizationState) {
        withLock { _currentStatus = status }
    }

    func requestAccess() async -> MicrophoneAuthorizationState {
        withLock {
            requestAccessCallCount += 1
            return _currentStatus
        }
    }
}

func makeTestAudioChunk(sequence: Int = 0, byteCount: Int = 16000) -> AudioChunk {
    AudioChunk(
        sequence: sequence,
        startedAt: Double(sequence) * 0.5,
        duration: 0.5,
        pcm: Data(repeating: 0, count: byteCount),
        sampleRate: 16000,
        channels: 1
    )
}
