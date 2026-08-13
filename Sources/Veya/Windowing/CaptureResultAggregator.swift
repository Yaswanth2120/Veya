import Foundation

/// Pure aggregation logic for `CaptureCompatibilityTester`'s multi-frame
/// verification — separated out so it can be unit tested without any real
/// `ScreenCaptureKit` I/O or Screen Recording permission. See build prompt
/// §5.8.
enum CaptureResultAggregator {
    struct Aggregate: Equatable {
        let status: PresenterPrivacyStatus
        let confidence: Double
        let message: String
    }

    /// - Parameters:
    ///   - detections: One entry per frame that was successfully captured
    ///     and analyzed — `true` if the marker was detected in that frame.
    ///   - captureFailures: Frames that could not be captured/analyzed at
    ///     all (not evidence either way).
    ///   - frameCount: Total frames requested.
    static func aggregate(detections: [Bool], captureFailures: Int, frameCount: Int) -> Aggregate {
        let total = detections.count
        let detectedCount = detections.filter { $0 }.count
        let minimumConclusiveSamples = max(3, frameCount - 1)

        guard total >= minimumConclusiveSamples else {
            return Aggregate(
                status: .uncertain,
                confidence: frameCount > 0 ? Double(total) / Double(frameCount) : 0,
                message: "Only \(total)/\(frameCount) frame(s) could be captured and analyzed (\(captureFailures) failure(s)) — not enough evidence for a conclusive result."
            )
        }

        if detectedCount == 0 {
            return Aggregate(
                status: .verified,
                confidence: Double(total) / Double(frameCount),
                message: "Veya's overlay was not visible in \(total)/\(total) local capture test frame(s)."
            )
        }

        if detectedCount == total {
            return Aggregate(
                status: .overlayDetected,
                confidence: Double(total) / Double(frameCount),
                message: "Veya's overlay was visible in \(total)/\(total) local capture test frame(s)."
            )
        }

        return Aggregate(
            status: .uncertain,
            confidence: Double(max(detectedCount, total - detectedCount)) / Double(total),
            message: "Inconsistent result: overlay visible in \(detectedCount)/\(total) frame(s)."
        )
    }
}
