import Foundation
@testable import Veya

/// A deterministic, in-memory `IPCTransport` for testing `IPCClient`
/// without a real subprocess — the "fake IPC transport" the build prompt
/// asks for. Tests push lines in via `simulateIncoming(_:)` (as if the
/// worker wrote them to stdout) and inspect `sentLines` (what the client
/// wrote, as if to the worker's stdin).
final class FakeIPCTransport: IPCTransport, @unchecked Sendable {
    private let lock = NSLock()
    private var _sentLines: [String] = []
    private let continuation: AsyncStream<String>.Continuation
    private let stream: AsyncStream<String>

    var sentLines: [String] {
        lock.lock()
        defer { lock.unlock() }
        return _sentLines
    }

    init() {
        var continuation: AsyncStream<String>.Continuation!
        stream = AsyncStream { continuation = $0 }
        self.continuation = continuation
    }

    func lineStream() -> AsyncStream<String> {
        stream
    }

    func send(_ line: String) async throws {
        appendSent(line)
    }

    private func appendSent(_ line: String) {
        lock.lock()
        _sentLines.append(line)
        lock.unlock()
    }

    func simulateIncoming(_ line: String) {
        continuation.yield(line)
    }

    func simulateClose() {
        continuation.finish()
    }

    /// Decodes the most recently sent line's `id` field — convenient for
    /// tests that need to reply to "whatever request was just sent"
    /// without hardcoding a UUID.
    func lastSentRequestID() -> String? {
        guard let line = sentLines.last, let data = line.data(using: .utf8) else { return nil }
        struct IDOnly: Decodable { let id: String }
        return try? JSONDecoder().decode(IDOnly.self, from: data).id
    }
}
