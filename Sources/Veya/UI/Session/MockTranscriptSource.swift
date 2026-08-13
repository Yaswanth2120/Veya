import Foundation

/// Plays a canned script as a sequence of `TranscriptSegment`s on a timer,
/// standing in for real transcription during this phase. Intentionally does
/// not conform to `Transcribing` — see that protocol's doc comment.
final class MockTranscriptSource {
    struct Line {
        let text: String
        let durationSeconds: Double
    }

    private let script: [Line]
    private let intervalSeconds: Double

    init(
        script: [Line] = MockTranscriptSource.defaultScript,
        intervalSeconds: Double = 2.0
    ) {
        self.script = script
        self.intervalSeconds = intervalSeconds
    }

    /// Yields one final `TranscriptSegment` per script line, spaced
    /// `intervalSeconds` apart, then finishes.
    func segments(sessionID: UUID) -> AsyncStream<TranscriptSegment> {
        let script = script
        let intervalSeconds = intervalSeconds
        return AsyncStream { continuation in
            let task = Task {
                var elapsed: TimeInterval = 0
                for line in script {
                    if Task.isCancelled { break }
                    let segment = TranscriptSegment(
                        id: UUID(),
                        sessionID: sessionID,
                        text: line.text,
                        startedAt: elapsed,
                        endedAt: elapsed + line.durationSeconds,
                        isFinal: true,
                        speakerRole: SpeakerRole.unknown.rawValue
                    )
                    continuation.yield(segment)
                    elapsed += line.durationSeconds
                    try? await Task.sleep(nanoseconds: UInt64(intervalSeconds * 1_000_000_000))
                }
                continuation.finish()
            }
            continuation.onTermination = { _ in task.cancel() }
        }
    }

    static let defaultScript: [Line] = [
        Line(text: "Thanks everyone for joining, let's get started with the migration recap.", durationSeconds: 3),
        Line(text: "We moved the auth service first since everything else depended on it.", durationSeconds: 3),
        Line(text: "So why did the migration take six weeks in total?", durationSeconds: 3),
        Line(text: "That's a fair question, let me walk through the timeline.", durationSeconds: 3),
        Line(text: "We rolled it out in stages to keep backward compatibility the whole way through.", durationSeconds: 3),
    ]
}
