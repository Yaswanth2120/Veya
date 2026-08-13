import Foundation

/// Everything `IPCClient` needs from a transport. The business-level
/// client and `IPCEventRouter` only ever talk to this protocol — swapping
/// the managed-subprocess stdio transport for, say, a Unix domain socket
/// later means writing a new `IPCTransport` conformance, not touching
/// `IPCClient`, `IPCEventRouter`, or any session/UI code.
protocol IPCTransport: Sendable {
    /// Called once by `IPCClient` to begin consuming lines. Each element
    /// is one complete line (no trailing newline). The stream finishes
    /// when the underlying transport closes.
    func lineStream() -> AsyncStream<String>

    /// Writes one line to the transport. `line` should not include a
    /// trailing newline — the transport appends it.
    func send(_ line: String) async throws
}

/// Request/response RPC with correlation and timeouts, plus an
/// asynchronous event stream — the transport-agnostic "business-level"
/// half of the bridge. One `IPCClient` per worker process.
actor IPCClient {
    private struct PendingEntry {
        let method: String
        let resume: (Swift.Result<IPCJSONValue, Error>) -> Void
    }

    private let transport: any IPCTransport
    private var pending: [String: PendingEntry] = [:]
    private var timeoutTasks: [String: Task<Void, Never>] = [:]
    private var readTask: Task<Void, Never>?
    private let eventsContinuation: AsyncStream<IPCEvent>.Continuation

    /// Every non-request-response message the worker sends (`event`
    /// messages) — including the synthetic `_protocol.malformed`
    /// diagnostic event for lines that couldn't be parsed at all. Single
    /// consumer by design (see `PythonWorkerManager`, which is the only
    /// thing that iterates this and fans events out further).
    nonisolated let events: AsyncStream<IPCEvent>

    init(transport: any IPCTransport) {
        self.transport = transport
        var continuation: AsyncStream<IPCEvent>.Continuation!
        self.events = AsyncStream { continuation = $0 }
        self.eventsContinuation = continuation
    }

    /// Begins consuming the transport's line stream. Safe to call once;
    /// subsequent calls are no-ops.
    func start() {
        guard readTask == nil else { return }
        let transport = transport
        readTask = Task { [weak self] in
            for await line in transport.lineStream() {
                if Task.isCancelled { break }
                await self?.handle(line: line)
            }
            await self?.handleTransportClosed()
        }
    }

    /// Cancels the read loop, fails every pending request with
    /// `.workerUnavailable`, and finishes the event stream. Idempotent.
    func stop() {
        readTask?.cancel()
        readTask = nil
        for task in timeoutTasks.values {
            task.cancel()
        }
        timeoutTasks.removeAll()
        failAllPending(with: IPCClientError.workerUnavailable)
        eventsContinuation.finish()
    }

    /// Sends a request and awaits its correlated response, decoded as
    /// `Response`. Throws `IPCClientError.timeout` if no response arrives
    /// within `timeout` seconds, or `.protocolError` if the worker
    /// returned an `error` message.
    func call<Params: Encodable & Sendable, Response: Decodable & Sendable>(
        method: String,
        params: Params,
        timeout: TimeInterval
    ) async throws -> Response {
        let id = UUID().uuidString
        let request = IPCOutgoingRequest(id: id, method: method, params: params)

        let json: IPCJSONValue = try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<IPCJSONValue, Error>) in
            pending[id] = PendingEntry(method: method) { result in
                continuation.resume(with: result)
            }

            timeoutTasks[id] = Task { [weak self] in
                try? await Task.sleep(nanoseconds: UInt64(max(timeout, 0) * 1_000_000_000))
                guard !Task.isCancelled else { return }
                await self?.timeoutPending(id: id, method: method)
            }

            let transport = transport
            Task { [weak self] in
                do {
                    let data = try IPCCoding.encoder.encode(request)
                    guard let line = String(data: data, encoding: .utf8) else {
                        throw IPCClientError.malformedLine("Could not utf8-encode outgoing request.")
                    }
                    try await transport.send(line)
                } catch {
                    await self?.failPending(id: id, error: error)
                }
            }
        }

        do {
            return try json.decoded(as: Response.self)
        } catch {
            throw IPCClientError.decodingFailed("\(error)")
        }
    }

    private func handle(line: String) {
        let envelope: IPCIncomingEnvelope
        do {
            guard let data = line.data(using: .utf8) else {
                throw IPCClientError.malformedLine("Non-UTF8 line.")
            }
            envelope = try IPCCoding.decoder.decode(IPCIncomingEnvelope.self, from: data)
        } catch {
            eventsContinuation.yield(IPCEvent(name: "_protocol.malformed", data: .string("\(error)")))
            return
        }

        guard envelope.version == IPCProtocolVersion.current else {
            eventsContinuation.yield(
                IPCEvent(name: "_protocol.malformed", data: .string("Unsupported version \(envelope.version)"))
            )
            return
        }

        switch envelope.type {
        case "response":
            guard let id = envelope.id else { return }
            resolvePending(id: id, result: .success(envelope.result ?? .object([:])))
        case "error":
            guard let id = envelope.id, let error = envelope.error else { return }
            resolvePending(
                id: id,
                result: .failure(IPCClientError.protocolError(code: error.code, message: error.message))
            )
        case "event":
            guard let name = envelope.event else { return }
            eventsContinuation.yield(IPCEvent(name: name, data: envelope.data ?? .object([:])))
        default:
            eventsContinuation.yield(IPCEvent(name: "_protocol.malformed", data: .string("Unknown type \(envelope.type)")))
        }
    }

    private func resolvePending(id: String, result: Swift.Result<IPCJSONValue, Error>) {
        timeoutTasks[id]?.cancel()
        timeoutTasks[id] = nil
        guard let entry = pending.removeValue(forKey: id) else { return }
        entry.resume(result)
    }

    private func failPending(id: String, error: Error) {
        resolvePending(id: id, result: .failure(error))
    }

    private func timeoutPending(id: String, method: String) {
        guard pending[id] != nil else { return }
        resolvePending(id: id, result: .failure(IPCClientError.timeout(method: method)))
    }

    private func failAllPending(with error: Error) {
        for id in Array(pending.keys) {
            resolvePending(id: id, result: .failure(error))
        }
    }

    private func handleTransportClosed() {
        failAllPending(with: IPCClientError.workerUnavailable)
        eventsContinuation.finish()
    }
}
