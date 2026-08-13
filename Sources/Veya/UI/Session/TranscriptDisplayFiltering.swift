import Foundation

/// Shared by every view that renders transcript text (Live Session,
/// Previous Sessions): hides whisper.cpp's non-speech tags
/// ("[BLANK_AUDIO]", "(silence)", "[ Music ]", ...) so raw capture
/// artifacts are never shown as if they were spoken content. The
/// transcription source itself also stops emitting these going forward
/// (see `core/veya/transcription/session.py`) — this covers sessions
/// transcribed before that fix, and is deliberately duplicated logic
/// (display-side defense in depth) rather than trusting only the source.
enum TranscriptDisplayFiltering {
    static func isNonSpeechMarker(_ text: String) -> Bool {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let first = trimmed.first, let last = trimmed.last else { return true }
        let isBracketed = (first == "[" && last == "]") || (first == "(" && last == ")")
        guard isBracketed else { return false }
        let inner = trimmed.dropFirst().dropLast()
        return !inner.contains("[") && !inner.contains("]") && !inner.contains("(") && !inner.contains(")")
    }

    static func displayable(_ segments: [TranscriptSegment]) -> [TranscriptSegment] {
        segments.filter { !isNonSpeechMarker($0.text) }
    }
}
