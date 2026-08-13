import AppKit
@preconcurrency import ScreenCaptureKit
import CoreMedia

/// A single captured frame. `CMSampleBuffer` isn't `Sendable` in the SDK,
/// but IOSurface-backed sample buffers from `ScreenCaptureKit` are safe to
/// hand across threads for read-only display — this wrapper documents that
/// assumption at the one place it's relied on.
struct SafeShareFrame: @unchecked Sendable {
    let sampleBuffer: CMSampleBuffer
}

struct SafeShareDiagnostics: Sendable {
    let isRunning: Bool
    let configuredFPS: Int
    let streamWidth: Int
    let streamHeight: Int
    let framesReceived: Int
    let framesDropped: Int
}

protocol SafeShareCapturing: Sendable {
    func start(displayID: UInt32, quality: SafeShareQuality, fps: Int) async throws -> AsyncStream<SafeShareFrame>
    func stop() async
    func diagnostics() async -> SafeShareDiagnostics
}

/// Owns a `ScreenCaptureKit` `SCStream` capturing one display while
/// excluding Veya's own application from the captured content — the core
/// mechanism behind Safe Share. Delivers frames through an `AsyncStream`
/// with a "keep only the newest frame" buffering policy, so a slow
/// consumer never causes unbounded memory growth (per the 8 GB target
/// machine's constraints — see `docs/PRESENTER_PRIVACY.md`).
actor SafeShareCaptureEngine: SafeShareCapturing {
    private var stream: SCStream?
    private var output: StreamOutputBridge?
    private var continuation: AsyncStream<SafeShareFrame>.Continuation?

    private var isRunning = false
    private var configuredFPS = 0
    private var streamWidth = 0
    private var streamHeight = 0
    private var framesReceived = 0
    private var framesDropped = 0

    private let streamQueue = DispatchQueue(label: "com.veya.safeshare.stream", qos: .userInitiated)

    func start(displayID: UInt32, quality: SafeShareQuality, fps: Int) async throws -> AsyncStream<SafeShareFrame> {
        guard !isRunning else {
            throw PresenterPrivacyError.safeShareAlreadyRunning
        }
        guard CGPreflightScreenCaptureAccess() else {
            throw PresenterPrivacyError.screenCapturePermissionDenied
        }

        let shareableContent: SCShareableContent
        do {
            shareableContent = try await SCShareableContent.current
        } catch {
            throw PresenterPrivacyError.captureInitializationFailed("Screen capture could not be initialized.")
        }

        guard let scDisplay = shareableContent.displays.first(where: { $0.displayID == displayID }) else {
            throw PresenterPrivacyError.displayUnavailable
        }

        let currentPID = ProcessInfo.processInfo.processIdentifier
        guard let veyaApp = shareableContent.applications.first(where: { $0.processID == currentPID }) else {
            throw PresenterPrivacyError.applicationFilterUnavailable
        }

        // The core Safe Share guarantee: capture the display MINUS Veya's
        // own application. Because Veya runs as a single process, this
        // excludes every Veya-owned window at once — overlay, Dashboard,
        // Settings, and the Safe Share window itself — which is also what
        // prevents the Safe Share window from recursively capturing
        // itself (see docs/PRESENTER_PRIVACY.md "Recursion prevention").
        let filter = SCContentFilter(display: scDisplay, excludingApplications: [veyaApp], exceptingWindows: [])

        let displayCGFrame = CGDisplayBounds(CGDirectDisplayID(displayID))
        let width = max(Int(displayCGFrame.width * quality.resolutionScale), 2)
        let height = max(Int(displayCGFrame.height * quality.resolutionScale), 2)

        let configuration = SCStreamConfiguration()
        configuration.width = width
        configuration.height = height
        configuration.minimumFrameInterval = CMTime(value: 1, timescale: CMTimeScale(max(fps, 1)))
        configuration.queueDepth = 3
        configuration.showsCursor = true
        configuration.pixelFormat = kCVPixelFormatType_32BGRA
        configuration.colorSpaceName = CGColorSpace.sRGB

        let (frameStream, continuation) = AsyncStream<SafeShareFrame>.makeStream(bufferingPolicy: .bufferingNewest(1))

        let bridge = StreamOutputBridge(
            continuation: continuation,
            onFrameReceived: { [weak self] in
                Task { await self?.recordFrameReceived() }
            },
            onFrameDropped: { [weak self] in
                Task { await self?.recordFrameDropped() }
            },
            onStreamStopped: { [weak self] error in
                Task { await self?.handleStreamStopped(error: error) }
            }
        )

        let newStream = SCStream(filter: filter, configuration: configuration, delegate: bridge)
        do {
            try newStream.addStreamOutput(bridge, type: .screen, sampleHandlerQueue: streamQueue)
            try await newStream.startCapture()
        } catch {
            continuation.finish()
            throw PresenterPrivacyError.captureInitializationFailed("Screen capture could not be initialized.")
        }

        continuation.onTermination = { [weak self] _ in
            Task { await self?.handleConsumerTerminated() }
        }

        self.stream = newStream
        self.output = bridge
        self.continuation = continuation
        self.isRunning = true
        self.configuredFPS = fps
        self.streamWidth = width
        self.streamHeight = height
        self.framesReceived = 0
        self.framesDropped = 0

        PrivacyLog.info("Safe Share capture started (\(width)x\(height) @ \(fps)fps)")
        return frameStream
    }

    func stop() async {
        guard isRunning else { return }
        if let stream {
            try? await stream.stopCapture()
        }
        continuation?.finish()
        continuation = nil
        stream = nil
        output = nil
        isRunning = false
        PrivacyLog.info("Safe Share capture stopped")
    }

    func diagnostics() -> SafeShareDiagnostics {
        SafeShareDiagnostics(
            isRunning: isRunning,
            configuredFPS: configuredFPS,
            streamWidth: streamWidth,
            streamHeight: streamHeight,
            framesReceived: framesReceived,
            framesDropped: framesDropped
        )
    }

    private func recordFrameReceived() {
        framesReceived += 1
    }

    private func recordFrameDropped() {
        framesDropped += 1
    }

    private func handleStreamStopped(error: Error) {
        guard isRunning else { return }
        PrivacyLog.error("Safe Share stream stopped unexpectedly, errorType=\(String(reflecting: type(of: error)))")
        continuation?.finish()
        continuation = nil
        stream = nil
        output = nil
        isRunning = false
    }

    private func handleConsumerTerminated() {
        // The frame consumer (SafeShareManager) went away without calling
        // `stop()` explicitly (e.g. it was deallocated). Make sure the
        // underlying stream doesn't keep running.
        guard isRunning else { return }
        Task { await stop() }
    }
}

/// Bridges `ScreenCaptureKit`'s Objective-C delegate callbacks (which
/// arrive off-actor, on `streamQueue`) into the actor via plain closures.
private final class StreamOutputBridge: NSObject, SCStreamOutput, SCStreamDelegate, @unchecked Sendable {
    private let continuation: AsyncStream<SafeShareFrame>.Continuation
    private let onFrameReceived: @Sendable () -> Void
    private let onFrameDropped: @Sendable () -> Void
    private let onStreamStopped: @Sendable (Error) -> Void

    init(
        continuation: AsyncStream<SafeShareFrame>.Continuation,
        onFrameReceived: @escaping @Sendable () -> Void,
        onFrameDropped: @escaping @Sendable () -> Void,
        onStreamStopped: @escaping @Sendable (Error) -> Void
    ) {
        self.continuation = continuation
        self.onFrameReceived = onFrameReceived
        self.onFrameDropped = onFrameDropped
        self.onStreamStopped = onStreamStopped
    }

    func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .screen, sampleBuffer.isValid else { return }
        onFrameReceived()
        let result = continuation.yield(SafeShareFrame(sampleBuffer: sampleBuffer))
        if case .dropped = result {
            onFrameDropped()
        }
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        onStreamStopped(error)
    }
}
