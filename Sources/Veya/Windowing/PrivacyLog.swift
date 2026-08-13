import os

/// Structured logging for the presenter-privacy subsystem. Logs lifecycle
/// events only (mode changes, test start/end, stream start/stop, errors) —
/// never screen contents, transcript contents, AI answers, or uploaded
/// documents.
enum PrivacyLog {
    private static let logger = Logger(subsystem: "com.veya.app", category: "presenter-privacy")

    static func info(_ message: String) {
        logger.info("\(message, privacy: .public)")
    }

    static func error(_ message: String) {
        logger.error("\(message, privacy: .public)")
    }
}
