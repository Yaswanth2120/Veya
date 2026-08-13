import Foundation
import Testing
@testable import Veya

@Suite("InterviewPreflightStatus readiness")
struct InterviewAudioModeTests {
    private func baseStatus(mode: InterviewAudioMode) -> InterviewPreflightStatus {
        InterviewPreflightStatus(
            mode: mode,
            microphone: .ready,
            meetingAudio: .ready,
            selectedMeetingSourceName: "Zoom",
            streamingASR: .ready,
            localAnswerModel: .ready,
            resumeContext: .ready,
            jobDescriptionContext: .optional
        )
    }

    @Test("fully ready microphone-only session reports ready")
    func microphoneOnlyReady() {
        var status = baseStatus(mode: .microphoneOnly)
        status.meetingAudio = .unavailable // irrelevant in this mode
        #expect(status.isReadyForInterview == true)
    }

    @Test("microphone-only mode never blocks on meeting-audio readiness")
    func microphoneOnlyIgnoresMeetingAudio() {
        var status = baseStatus(mode: .microphoneOnly)
        status.meetingAudio = .permissionRequired
        #expect(status.isReadyForInterview == true)
    }

    @Test("meeting audio + microphone mode blocks until both tracks are ready")
    func dualModeRequiresBothTracks() {
        var status = baseStatus(mode: .meetingAudioPlusMicrophone)
        status.meetingAudio = .permissionRequired
        #expect(status.isReadyForInterview == false)

        status.meetingAudio = .ready
        #expect(status.isReadyForInterview == true)
    }

    @Test("meeting-audio-only mode never blocks on microphone readiness")
    func meetingAudioOnlyIgnoresMicrophone() {
        var status = baseStatus(mode: .meetingAudioOnly)
        status.microphone = .unavailable
        #expect(status.isReadyForInterview == true)
    }

    @Test("streaming ASR unavailable blocks start regardless of mode")
    func asrUnavailableBlocksStart() {
        var status = baseStatus(mode: .microphoneOnly)
        status.streamingASR = .unavailable
        #expect(status.isReadyForInterview == false)
    }

    @Test("local answer model not ready blocks start")
    func answerModelNotReadyBlocksStart() {
        var status = baseStatus(mode: .microphoneOnly)
        status.localAnswerModel = .unavailable
        #expect(status.isReadyForInterview == false)
    }

    @Test("missing resume blocks start by default")
    func missingResumeBlocksStartByDefault() {
        var status = baseStatus(mode: .microphoneOnly)
        status.resumeContext = .missing
        #expect(status.isReadyForInterview == false)
    }

    @Test("explicit start-without-resume opt-out allows a missing resume through")
    func explicitOptOutAllowsMissingResume() {
        var status = baseStatus(mode: .microphoneOnly)
        status.resumeContext = .missing
        status.resumeRequired = false
        #expect(status.isReadyForInterview == true)
    }

    @Test("resume still indexing always blocks start, even with the opt-out")
    func indexingResumeAlwaysBlocks() {
        var status = baseStatus(mode: .microphoneOnly)
        status.resumeContext = .indexing
        status.resumeRequired = false
        #expect(status.isReadyForInterview == false)
    }

    @Test("job description never gates start, ready or not")
    func jobDescriptionNeverGates() {
        var status = baseStatus(mode: .microphoneOnly)
        status.jobDescriptionContext = .missing
        #expect(status.isReadyForInterview == true)
    }

    @Test("only meeting-audio + microphone mode claims reliable speaker separation")
    func speakerSeparationOnlyInDualMode() {
        #expect(InterviewAudioMode.meetingAudioPlusMicrophone.hasReliableSpeakerSeparation == true)
        #expect(InterviewAudioMode.microphoneOnly.hasReliableSpeakerSeparation == false)
        #expect(InterviewAudioMode.meetingAudioOnly.hasReliableSpeakerSeparation == false)
    }
}
