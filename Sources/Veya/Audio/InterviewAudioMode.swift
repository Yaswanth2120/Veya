import Foundation

/// Section 16: the three interview-audio input modes. Distinct from raw
/// audio-source plumbing — this is what the preflight screen and
/// `CreateSessionViewModel` reason about.
enum InterviewAudioMode: String, CaseIterable, Identifiable, Sendable {
    case microphoneOnly
    case meetingAudioPlusMicrophone
    case meetingAudioOnly

    var id: String { rawValue }

    var displayName: String {
        switch self {
        case .microphoneOnly: return "Microphone only"
        case .meetingAudioPlusMicrophone: return "Meeting audio + microphone"
        case .meetingAudioOnly: return "System/meeting audio only"
        }
    }

    /// The user actually speaks into a real microphone track in this mode
    /// — used to decide whether the "I'm answering" mixed-mode fallback
    /// control is relevant, and whether a microphone track needs to start
    /// at all.
    var usesMicrophone: Bool {
        self != .meetingAudioOnly
    }

    var usesMeetingAudio: Bool {
        self != .microphoneOnly
    }

    /// `true` for the one mode where Python can reliably tell interviewer
    /// speech from the user's own — the other two need the "I'm
    /// answering" fallback control and are honestly labeled "unknown"
    /// speaker identity on the wire (see `SpeakerRole`).
    var hasReliableSpeakerSeparation: Bool {
        self == .meetingAudioPlusMicrophone
    }
}

/// Per-component readiness the preflight screen shows verbatim — never
/// lets the UI claim "Ready for interview" until every component this
/// mode actually needs reports `.ready`.
enum ReadinessState: Equatable, Sendable {
    case ready
    case unavailable
    case permissionRequired
    case indexing
    case missing
    case optional

    var displayText: String {
        switch self {
        case .ready: return "Ready"
        case .unavailable: return "Unavailable"
        case .permissionRequired: return "Permission required"
        case .indexing: return "Indexing…"
        case .missing: return "Missing"
        case .optional: return "Optional"
        }
    }
}

struct InterviewPreflightStatus: Equatable, Sendable {
    var mode: InterviewAudioMode
    var microphone: ReadinessState
    var meetingAudio: ReadinessState
    var selectedMeetingSourceName: String?
    var streamingASR: ReadinessState
    var localAnswerModel: ReadinessState
    var resumeContext: ReadinessState
    var jobDescriptionContext: ReadinessState
    /// `false` once the user has explicitly chosen "Start without
    /// resume" — the one way `resumeContext == .missing` stops blocking
    /// the start button. Job description never gates start either way
    /// (always optional).
    var resumeRequired: Bool = true

    /// The single gating question the "Start Interview" button asks.
    /// Only the components this *mode* actually needs are required —
    /// e.g. meeting-audio readiness is irrelevant in microphone-only
    /// mode. Job description is always optional, matching the product
    /// requirement ("strongly recommended," never required). Resume is
    /// required unless the caller explicitly opted out (see
    /// `CreateSessionViewModel`'s "Start without resume").
    var isReadyForInterview: Bool {
        if mode.usesMicrophone && microphone != .ready { return false }
        if mode.usesMeetingAudio && meetingAudio != .ready { return false }
        if streamingASR == .unavailable { return false }
        if localAnswerModel != .ready { return false }
        if resumeContext == .indexing { return false }
        if resumeRequired && resumeContext == .missing { return false }
        return true
    }
}
