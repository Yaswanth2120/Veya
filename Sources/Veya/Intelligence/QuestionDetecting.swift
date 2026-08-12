import Foundation

/// STUB — interface only. Real question detection over live transcript is a
/// separate, not-yet-scoped subsystem.
protocol QuestionDetecting {
    func detectQuestion(in segment: TranscriptSegment) -> DetectedQuestion?
}
