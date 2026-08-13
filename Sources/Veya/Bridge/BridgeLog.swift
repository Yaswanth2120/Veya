import os

/// Structured logging for the Swift↔Python bridge. Logs worker lifecycle
/// (startup/ready/exit/restart), request lifecycle metadata (method name,
/// id, timing), and protocol errors — never transcript text, answers,
/// prompts, or other sensitive payload content. Mirrors `PrivacyLog`'s
/// pattern (see `Windowing/PrivacyLog.swift`).
enum BridgeLog {
    private static let logger = Logger(subsystem: "com.veya.app", category: "python-bridge")

    static func info(_ message: String) {
        logger.info("\(message, privacy: .public)")
    }

    static func error(_ message: String) {
        logger.error("\(message, privacy: .public)")
    }
}
