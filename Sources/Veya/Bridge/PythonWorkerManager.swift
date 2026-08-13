import Foundation

enum PythonWorkerState: Equatable, Sendable {
    case stopped
    case starting
    case ready
    case unhealthy
    case restarting
    case failed(String)
}

enum PythonWorkerError: LocalizedError, Sendable {
    case readyTimeout
    case launchFailed(String)

    var errorDescription: String? {
        switch self {
        case .readyTimeout:
            return "The Python worker did not report ready in time."
        case .launchFailed(let reason):
            return "Failed to launch the Python worker: \(reason)"
        }
    }
}

/// Owns the Python worker's `Process` lifecycle: launch, health checks,
/// bounded crash-restart, and clean shutdown. Delegates the actual
/// request/response and event mechanics to `IPCClient`, and forwards
/// decoded events onward (see `eventHandler`) — this class only knows
/// about *process* lifecycle, not about `ConversationState` or any other
/// app-level model.
@MainActor
final class PythonWorkerManager: ObservableObject {
    @Published private(set) var state: PythonWorkerState = .stopped {
        didSet { stateChangeHandler?(state) }
    }

    /// Set by whoever wires this manager into the app (see
    /// `PythonIntelligenceCoordinator`) to receive every worker event
    /// except `worker.ready` (consumed internally) and the synthetic
    /// `_protocol.malformed` diagnostic (logged, not forwarded). Called
    /// sequentially, in arrival order, and awaited before the next event
    /// is processed — callers can rely on in-order delivery.
    var eventHandler: ((IPCEvent) async -> Void)?

    /// Set by `PythonIntelligenceCoordinator` to learn about *every* state
    /// transition, including ones that happen mid-session (a crash-restart
    /// while a Python-driven Live Session is active). Called synchronously,
    /// on the main actor, right after `state` changes.
    var stateChangeHandler: ((PythonWorkerState) -> Void)?

    /// Mutable so `PythonIntelligenceCoordinator.prepareRealTranscriptionAssets()`
    /// can fill in `whisperBinaryPathURL`/`whisperModelPathURL` once
    /// they're resolved (possibly after an async model download) —
    /// always *before* `start()`'s first launch, since
    /// `launchProcessAndWaitForReady()` reads `configuration` fresh only
    /// at each process launch, not continuously.
    var configuration: PythonWorkerConfiguration

    private var process: Process?
    /// Incremented on every `launchProcess()` call. `Process.
    /// terminationHandler` closures capture the generation they were
    /// created for and only act if it still matches `self
    /// .processGeneration` — see `launchProcess()`'s doc comment for why
    /// this is more robust than comparing `self.process === terminatedProcess`.
    private var processGeneration = 0
    /// Generation whose termination has already been handled. Foundation's
    /// `Process.terminationHandler` was observed, in testing, to sometimes
    /// invoke its closure twice for the same underlying process exit —
    /// this makes `handleUnexpectedTermination` idempotent per generation
    /// regardless of that, so a duplicate callback can never double-tear-down
    /// state or trigger two restarts for one real crash.
    private var handledTerminationGeneration: Int?
    private var client: IPCClient?
    private var eventConsumerTask: Task<Void, Never>?
    private var healthCheckTask: Task<Void, Never>?
    private var readyContinuation: CheckedContinuation<Void, Error>?
    /// Identifies which `waitForReady()` call `readyContinuation` belongs
    /// to. Needed because restarts can start a *new* `waitForReady()` call
    /// (overwriting `readyContinuation`) before an *old* call's timeout
    /// task fires — without this, the stale timeout task would see a
    /// non-nil `readyContinuation`, assume it's its own, and resume the
    /// wrong (or already-resumed) continuation, crashing with "resumed
    /// its continuation more than once."
    private var readyRequestToken: UUID?
    private var consecutivePingFailures = 0
    private var restartAttempts = 0

    /// Bounded, metadata-only diagnostic buffer. Never retain or forward
    /// raw worker stderr: a dependency can emit document/parser details
    /// despite the worker's own logging policy.
    private(set) var recentStderrLines: [String] = []
    private let maxStderrLines = 20

    init(configuration: PythonWorkerConfiguration = .resolveDefault()) {
        self.configuration = configuration
    }

    // MARK: - Lifecycle

    func start() async {
        guard state == .stopped || isFailed else { return }
        state = .starting
        restartAttempts = 0
        await launchAndWaitForReady()
    }

    private func launchAndWaitForReady() async {
        do {
            try await launchProcessAndWaitForReady()
            state = .ready
            BridgeLog.info("worker ready")
        } catch {
            BridgeLog.error("worker failed to start, errorType=\(String(reflecting: type(of: error)))")
            teardownProcess()
            state = .failed("Worker startup failed (\(String(reflecting: type(of: error))))")
        }
    }

    /// `Process.terminationHandler`'s closure is guarded by a generation
    /// counter, not by comparing `self.process === terminatedProcess` —
    /// robust regardless of exactly when `self.process` gets reassigned
    /// relative to when the OS reports an old process's exit.
    ///
    /// The ready continuation is registered — and the timeout task
    /// scheduled — *before* `process.run()`, inside the same synchronous
    /// closure that starts the process and kicks off its output-reading
    /// tasks. This closes a real "lost wakeup" race that showed up in
    /// testing: a restarted worker starts faster than the very first
    /// launch (warm interpreter/module caches), and could emit
    /// `worker.ready` before a *separately scheduled* continuation-setup
    /// step got a chance to run, silently dropping the ready signal and
    /// stalling until `readyTimeout`.
    private func launchProcessAndWaitForReady() async throws {
        processGeneration += 1
        let generation = processGeneration

        let process = Process()
        process.executableURL = configuration.pythonExecutableURL
        process.arguments = configuration.pythonArguments
        process.currentDirectoryURL = configuration.workerDirectoryURL
        // Inherits the parent's environment (Foundation default) plus the
        // two Section 9 knowledge-layer paths — never a *replacement* of
        // the parent env, since VEYA_PYTHON_EXECUTABLE/PATH/etc still need
        // to reach the subprocess exactly as before.
        var environment = ProcessInfo.processInfo.environment
        environment[PythonWorkerConfiguration.documentsDirectoryEnvironmentKey] = configuration.documentsDirectoryURL.path
        environment[PythonWorkerConfiguration.knowledgeIndexDirectoryEnvironmentKey] = configuration.knowledgeIndexDirectoryURL.path
        environment[PythonWorkerConfiguration.memoryDatabasePathEnvironmentKey] = configuration.memoryDatabasePathURL.path
        environment[PythonWorkerConfiguration.reportStoreDirectoryEnvironmentKey] = configuration.reportStoreDirectoryURL.path
        // An explicit VEYA_WHISPER_BIN/VEYA_WHISPER_MODEL already present
        // in the inherited environment (developer/CI convenience) always
        // wins over a resolved-by-Swift path — never silently overridden.
        if environment["VEYA_WHISPER_BIN"] == nil, let whisperBinaryPathURL = configuration.whisperBinaryPathURL {
            environment["VEYA_WHISPER_BIN"] = whisperBinaryPathURL.path
        }
        if environment["VEYA_WHISPER_MODEL"] == nil, let whisperModelPathURL = configuration.whisperModelPathURL {
            environment["VEYA_WHISPER_MODEL"] = whisperModelPathURL.path
        }
        process.environment = environment

        let stdinPipe = Pipe()
        let stdoutPipe = Pipe()
        let stderrPipe = Pipe()
        process.standardInput = stdinPipe
        process.standardOutput = stdoutPipe
        process.standardError = stderrPipe

        let transport = ProcessStdioTransport(
            stdinHandle: stdinPipe.fileHandleForWriting,
            stdoutHandle: stdoutPipe.fileHandleForReading
        )
        let client = IPCClient(transport: transport)

        process.terminationHandler = { [weak self] terminatedProcess in
            Task { @MainActor in
                guard let self,
                      self.processGeneration == generation,
                      self.handledTerminationGeneration != generation
                else { return }
                self.handledTerminationGeneration = generation
                // A process can exit before emitting `worker.ready`. In
                // that case the launch continuation has no stdout event
                // and its timeout is intentionally no longer the owner
                // once a restart begins. Resume it here so no startup task
                // is stranded (and so Swift does not report a leaked
                // checked continuation during crash-race tests).
                if let continuation = self.readyContinuation {
                    self.readyContinuation = nil
                    self.readyRequestToken = nil
                    continuation.resume(throwing: PythonWorkerError.launchFailed("worker exited before ready"))
                }
                self.handleUnexpectedTermination(exitCode: terminatedProcess.terminationStatus)
            }
        }

        try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
            let token = UUID()
            self.readyContinuation = continuation
            self.readyRequestToken = token

            do {
                try process.run()
            } catch {
                self.readyContinuation = nil
                self.readyRequestToken = nil
                continuation.resume(throwing: PythonWorkerError.launchFailed(String(reflecting: type(of: error))) )
                return
            }

            self.process = process
            self.client = client
            Task { await client.start() }
            self.startEventConsumption(client: client)
            self.startStderrCapture(handle: stderrPipe.fileHandleForReading)

            let timeout = self.configuration.readyTimeout
            Task { [weak self] in
                try? await Task.sleep(nanoseconds: UInt64(max(timeout, 0) * 1_000_000_000))
                guard let self, self.readyRequestToken == token, self.readyContinuation != nil else { return }
                self.readyContinuation = nil
                self.readyRequestToken = nil
                continuation.resume(throwing: PythonWorkerError.readyTimeout)
            }
        }
    }

    /// Best-effort graceful shutdown: sends `worker.shutdown`, then
    /// terminates the process regardless (a no-op if it already exited on
    /// its own). Sets `.stopped` *before* terminating so the termination
    /// handler recognizes this as expected and does not trigger a restart.
    func stop() async {
        guard state != .stopped else { return }
        state = .stopped
        if let continuation = readyContinuation {
            readyContinuation = nil
            readyRequestToken = nil
            continuation.resume(throwing: IPCClientError.workerUnavailable)
        }
        endHealthChecking()
        eventConsumerTask?.cancel()
        eventConsumerTask = nil

        if let client {
            let response: OkResult? = try? await client.call(
                method: "worker.shutdown",
                params: EmptyIPCParams(),
                timeout: 3
            )
            BridgeLog.info("worker.shutdown acknowledged=\(response?.ok == true)")
        }

        await client?.stop()
        teardownProcess()
    }

    private func teardownProcess() {
        client = nil
        if let process, process.isRunning {
            process.terminate()
        }
        process = nil
    }

    private func handleUnexpectedTermination(exitCode: Int32) {
        guard state != .stopped else { return }
        BridgeLog.error("worker exited unexpectedly, code=\(exitCode)")

        endHealthChecking()
        eventConsumerTask?.cancel()
        eventConsumerTask = nil
        Task { await client?.stop() }
        client = nil
        process = nil

        guard restartAttempts < configuration.maxRestartAttempts else {
            state = .failed("Worker exited unexpectedly (code \(exitCode)) and exceeded the maximum restart attempts.")
            return
        }

        restartAttempts += 1
        state = .restarting
        let backoffSeconds = configuration.restartBackoffBaseSeconds * pow(2, Double(restartAttempts - 1))
        BridgeLog.info("restarting worker, attempt=\(restartAttempts), backoff=\(backoffSeconds)s")
        Task { [weak self] in
            try? await Task.sleep(nanoseconds: UInt64(backoffSeconds * 1_000_000_000))
            await self?.launchAndWaitForReady()
        }
    }

    private var isFailed: Bool {
        if case .failed = state { return true }
        return false
    }

    // MARK: - RPC

    func call<Params: Encodable & Sendable, Response: Decodable & Sendable>(
        method: String,
        params: Params,
        timeout: TimeInterval? = nil
    ) async throws -> Response {
        guard let client, state == .ready || state == .unhealthy else {
            throw IPCClientError.workerUnavailable
        }
        return try await client.call(method: method, params: params, timeout: timeout ?? configuration.rpcTimeout)
    }

    // MARK: - Health checking

    /// Starts the periodic `system.ping` loop. Only meaningful while a
    /// Python-driven Live Session is active — callers (see
    /// `PythonIntelligenceCoordinator`) start this when
    /// `mock.start_live_feed` succeeds and stop it when the session ends.
    func beginHealthChecking() {
        guard healthCheckTask == nil, state == .ready || state == .unhealthy else { return }
        consecutivePingFailures = 0
        let interval = configuration.healthCheckInterval
        healthCheckTask = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: UInt64(max(interval, 1) * 1_000_000_000))
                if Task.isCancelled { break }
                await self?.performHealthCheckPing()
            }
        }
    }

    func endHealthChecking() {
        healthCheckTask?.cancel()
        healthCheckTask = nil
    }

    private func performHealthCheckPing() async {
        do {
            let _: PingResult = try await call(method: "system.ping", params: EmptyIPCParams())
            consecutivePingFailures = 0
            if state == .unhealthy {
                state = .ready
            }
        } catch {
            consecutivePingFailures += 1
            BridgeLog.error("health check ping failed, count=\(consecutivePingFailures), errorType=\(String(reflecting: type(of: error)))")
            if consecutivePingFailures >= configuration.maxConsecutivePingFailuresBeforeUnhealthy {
                state = .unhealthy
            }
        }
    }

    // MARK: - Event forwarding

    private func startEventConsumption(client: IPCClient) {
        eventConsumerTask = Task { [weak self] in
            for await event in client.events {
                if Task.isCancelled { break }
                await self?.handle(event: event)
            }
        }
    }

    private func handle(event: IPCEvent) async {
        switch event.name {
        case "worker.ready":
            readyContinuation?.resume(returning: ())
            readyContinuation = nil
            readyRequestToken = nil
        case "_protocol.malformed":
            BridgeLog.error("malformed worker output")
        default:
            await eventHandler?(event)
        }
    }

    private func startStderrCapture(handle: FileHandle) {
        Task { [weak self] in
            for await line in handle.linesStream() {
                if !line.isEmpty {
                    self?.recordStderrLine(line)
                }
            }
        }
    }

    private func recordStderrLine(_ line: String) {
        // Treat stderr as untrusted payload. Preserve only its size for
        // lifecycle diagnostics; raw lines can contain parser diagnostics,
        // document fragments, or exception messages from dependencies.
        let diagnostic = Self.stderrDiagnostic(for: line)
        recentStderrLines.append(diagnostic)
        if recentStderrLines.count > maxStderrLines {
            recentStderrLines.removeFirst(recentStderrLines.count - maxStderrLines)
        }
        BridgeLog.info(diagnostic)
    }

    /// Kept pure and internal so the privacy boundary is regression-tested
    /// without having to launch a process that emits sensitive stderr.
    nonisolated static func stderrDiagnostic(for line: String) -> String {
        "worker stderr bytes=\(line.utf8.count)"
    }
}

/// `Process`-backed stdio transport: the concrete `IPCTransport` used in
/// production. Reads stdout in chunks (see `FileHandle.linesStream()`)
/// and writes to stdin.
final class ProcessStdioTransport: IPCTransport {
    private let stdinHandle: FileHandle
    private let stdoutHandle: FileHandle

    init(stdinHandle: FileHandle, stdoutHandle: FileHandle) {
        self.stdinHandle = stdinHandle
        self.stdoutHandle = stdoutHandle
    }

    func lineStream() -> AsyncStream<String> {
        stdoutHandle.linesStream()
    }

    func send(_ line: String) async throws {
        let terminated = line.hasSuffix("\n") ? line : line + "\n"
        guard let data = terminated.data(using: .utf8) else {
            throw IPCClientError.malformedLine("Could not utf8-encode outgoing line.")
        }
        try stdinHandle.write(contentsOf: data)
    }
}
