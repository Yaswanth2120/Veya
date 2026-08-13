import Foundation

/// Polls `condition` until it's true or `timeout` elapses. Used instead of
/// fixed sleeps throughout the bridge tests to avoid flakiness under load
/// while keeping tests fast when the condition is met quickly.
///
/// `@MainActor`-isolated (rather than requiring `condition` to be
/// `@Sendable`) since most callers poll `@MainActor`-isolated state
/// (`PythonWorkerManager.state`, `ConversationState.currentAnswer`, etc.)
/// via a plain closure.
@MainActor
func waitUntil(timeout: TimeInterval = 2, _ condition: () -> Bool) async throws {
    let deadline = Date().addingTimeInterval(timeout)
    while !condition() {
        if Date() > deadline {
            throw WaitUntilTimeoutError()
        }
        try await Task.sleep(nanoseconds: 5_000_000)
    }
}

struct WaitUntilTimeoutError: Error {}
