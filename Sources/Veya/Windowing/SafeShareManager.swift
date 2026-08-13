import AppKit

/// Coordinates the Safe Share lifecycle: owns the capture engine (behind
/// the `SafeShareCapturing` protocol so tests can mock it) and the
/// `SafeShareWindowController`, and pumps frames from one to the other.
@MainActor
final class SafeShareManager: ObservableObject {
    @Published private(set) var isRunning = false
    @Published private(set) var lastError: PresenterPrivacyError?

    private let engine: any SafeShareCapturing
    private let makeWindowController: () -> any SafeShareWindowControlling
    private(set) var windowController: (any SafeShareWindowControlling)?
    private var frameConsumerTask: Task<Void, Never>?

    init(
        engine: any SafeShareCapturing = SafeShareCaptureEngine(),
        makeWindowController: @escaping () -> any SafeShareWindowControlling = { SafeShareWindowController() }
    ) {
        self.engine = engine
        self.makeWindowController = makeWindowController
    }

    func start(displayID: UInt32, quality: SafeShareQuality, fps: Int) async throws {
        guard !isRunning else {
            throw PresenterPrivacyError.safeShareAlreadyRunning
        }

        do {
            let frames = try await engine.start(displayID: displayID, quality: quality, fps: fps)

            let controller = windowController ?? makeWindowController()
            windowController = controller
            controller.show()

            frameConsumerTask = Task { [weak self, weak controller] in
                for await frame in frames {
                    if Task.isCancelled { break }
                    controller?.render(frame)
                }
                self?.handleStreamEnded()
            }

            isRunning = true
            lastError = nil
        } catch let error as PresenterPrivacyError {
            lastError = error
            throw error
        } catch {
            let wrapped = PresenterPrivacyError.captureInitializationFailed(error.localizedDescription)
            lastError = wrapped
            throw wrapped
        }
    }

    func stop() async {
        guard isRunning else { return }
        frameConsumerTask?.cancel()
        frameConsumerTask = nil
        await engine.stop()
        windowController?.hide()
        isRunning = false
    }

    func diagnostics() async -> SafeShareDiagnostics {
        await engine.diagnostics()
    }

    /// Called when the frame stream finishes on its own (stream error,
    /// permission revoked mid-capture, display disconnected) rather than
    /// via an explicit `stop()`.
    private func handleStreamEnded() {
        guard isRunning else { return }
        frameConsumerTask = nil
        windowController?.hide()
        isRunning = false
    }
}
