import Foundation
import Testing
@testable import Veya

@MainActor
@Suite("IPCEventRouter")
struct IPCEventRouterTests {
    private func makeEvent(_ name: String, _ dataJSON: String) throws -> IPCEvent {
        let data = try IPCCoding.decoder.decode(IPCJSONValue.self, from: Data(dataJSON.utf8))
        return IPCEvent(name: name, data: data)
    }

    /// Also inserts a matching `Session` row — `TranscriptSegment`/
    /// `DetectedQuestion`/`CopilotAnswer` all have a foreign key to
    /// `session(id)` (see `DatabaseManager`'s migration), so persisting
    /// any of them for a session that doesn't exist yet silently fails
    /// (`ConversationState` swallows persistence errors with `try?`).
    private func makeState(sessionID: UUID) async -> (ConversationState, ConversationRepository) {
        let db = DatabaseManager.makeInMemory()
        let sessionRepository = SessionRepository(db: db)
        var session = Session.makeTestSession(title: "Router Test")
        session.id = sessionID
        try? await sessionRepository.create(session)

        let repository = ConversationRepository(db: db)
        return (ConversationState(sessionID: sessionID, repository: repository), repository)
    }

    @Test("transcript.partial sets transient partial transcript text without persisting")
    func transcriptPartial() async throws {
        let sessionID = UUID()
        let (state, _) = await makeState(sessionID: sessionID)
        let router = IPCEventRouter()
        router.attach(state: state, sessionID: sessionID)

        let event = try makeEvent(
            "transcript.partial",
            #"{"session_id":"\#(sessionID.uuidString)","text":"partial words"}"#
        )
        await router.route(event)

        #expect(state.partialTranscriptText == "partial words")
        #expect(state.segments.isEmpty)
    }

    @Test("transcript.rejected increments the safe rejected-noise count only, never the transcript")
    func transcriptRejected() async throws {
        let sessionID = UUID()
        let (state, _) = await makeState(sessionID: sessionID)
        let router = IPCEventRouter()
        router.attach(state: state, sessionID: sessionID)

        let event = try makeEvent(
            "transcript.rejected",
            #"{"session_id":"\#(sessionID.uuidString)","reason":"transcript_rejected_non_speech_marker"}"#
        )
        await router.route(event)

        #expect(state.rejectedTranscriptCount == 1)
        #expect(state.segments.isEmpty)
        #expect(state.partialTranscriptText == nil)
    }

    @Test("transcript.final appends and persists a TranscriptSegment, clearing the partial text")
    func transcriptFinal() async throws {
        let sessionID = UUID()
        let (state, _) = await makeState(sessionID: sessionID)
        state.setPartialTranscript("partial words")
        let router = IPCEventRouter()
        router.attach(state: state, sessionID: sessionID)

        let segmentID = UUID()
        let event = try makeEvent(
            "transcript.final",
            #"""
            {"session_id":"\#(sessionID.uuidString)","id":"\#(segmentID.uuidString)","text":"final words","started_at":0.0,"ended_at":1.5,"is_final":true}
            """#
        )
        await router.route(event)

        #expect(state.segments.count == 1)
        #expect(state.segments.first?.text == "final words")
        #expect(state.segments.first?.id == segmentID)
        #expect(state.partialTranscriptText == nil)
    }

    @Test("question.detected appends and persists a DetectedQuestion")
    func questionDetected() async throws {
        let sessionID = UUID()
        let (state, _) = await makeState(sessionID: sessionID)
        let router = IPCEventRouter()
        router.attach(state: state, sessionID: sessionID)

        let questionID = UUID()
        let event = try makeEvent(
            "question.detected",
            #"{"session_id":"\#(sessionID.uuidString)","question_id":"\#(questionID.uuidString)","text":"why?","confidence":0.9,"detected_at":123.0}"#
        )
        await router.route(event)

        #expect(state.detectedQuestions.count == 1)
        #expect(state.detectedQuestions.first?.id == questionID)
        #expect(state.detectedQuestions.first?.text == "why?")
    }

    @Test("answer.started sets the loading/generating state")
    func answerStarted() async throws {
        let sessionID = UUID()
        let (state, _) = await makeState(sessionID: sessionID)
        let router = IPCEventRouter()
        router.attach(state: state, sessionID: sessionID)

        let event = try makeEvent(
            "answer.started",
            #"{"session_id":"\#(sessionID.uuidString)","question_id":"q1","sequence":1}"#
        )
        await router.route(event)

        #expect(state.isGeneratingAnswer == true)
    }

    @Test("answer.speakable_delta sets the transient partial-answer text")
    func answerDelta() async throws {
        let sessionID = UUID()
        let (state, _) = await makeState(sessionID: sessionID)
        let router = IPCEventRouter()
        router.attach(state: state, sessionID: sessionID)

        // A delta is only applied once its sequence matches the currently
        // active answer round — start that round first.
        await router.route(
            try makeEvent("answer.started", #"{"session_id":"\#(sessionID.uuidString)","question_id":"q1","sequence":1}"#)
        )
        let event = try makeEvent(
            "answer.speakable_delta",
            #"{"session_id":"\#(sessionID.uuidString)","question_id":"q1","delta":"Staged rollout","sequence":1}"#
        )
        await router.route(event)

        #expect(state.partialAnswerText == "Staged rollout")
    }

    @Test("consecutive answer.speakable_delta events accumulate, not replace")
    func answerSpeakableDeltaAccumulates() async throws {
        let sessionID = UUID()
        let (state, _) = await makeState(sessionID: sessionID)
        let router = IPCEventRouter()
        router.attach(state: state, sessionID: sessionID)

        await router.route(
            try makeEvent("answer.started", #"{"session_id":"\#(sessionID.uuidString)","question_id":"q1","sequence":1}"#)
        )
        await router.route(
            try makeEvent(
                "answer.speakable_delta",
                #"{"session_id":"\#(sessionID.uuidString)","question_id":"q1","delta":"I led the ","sequence":1}"#
            )
        )
        await router.route(
            try makeEvent(
                "answer.speakable_delta",
                #"{"session_id":"\#(sessionID.uuidString)","question_id":"q1","delta":"migration.","sequence":1}"#
            )
        )

        #expect(state.partialAnswerText == "I led the migration.")
    }

    @Test("a raw answer.delta event (legacy/unfiltered) is never rendered — only answer.speakable_delta updates visible text")
    func rawAnswerDeltaIsIgnored() async throws {
        let sessionID = UUID()
        let (state, _) = await makeState(sessionID: sessionID)
        let router = IPCEventRouter()
        router.attach(state: state, sessionID: sessionID)

        await router.route(
            try makeEvent("answer.started", #"{"session_id":"\#(sessionID.uuidString)","question_id":"q1","sequence":1}"#)
        )
        await router.route(
            try makeEvent(
                "answer.delta",
                #"{"session_id":"\#(sessionID.uuidString)","question_id":"q1","delta":"<think>hidden reasoning</think>","sequence":1}"#
            )
        )

        #expect(state.partialAnswerText == nil)
    }

    @Test("a raw answer.draft_delta event (legacy/unfiltered) is never rendered — only answer.speakable_draft_delta updates the draft")
    func rawAnswerDraftDeltaIsIgnored() async throws {
        let sessionID = UUID()
        let (state, _) = await makeState(sessionID: sessionID)
        let router = IPCEventRouter()
        router.attach(state: state, sessionID: sessionID)

        await router.route(
            try makeEvent("answer.draft_started", #"{"session_id":"\#(sessionID.uuidString)","question_id":"q1","sequence":1}"#)
        )
        await router.route(
            try makeEvent(
                "answer.draft_delta",
                #"{"session_id":"\#(sessionID.uuidString)","question_id":"q1","delta":"<think>hidden</think>","sequence":1}"#
            )
        )

        #expect(state.draftAnswerText == "")
    }

    @Test("answer.slow_warning sets isAnswerSlow, cleared by the next speakable delta")
    func answerSlowWarningRouting() async throws {
        let sessionID = UUID()
        let (state, _) = await makeState(sessionID: sessionID)
        let router = IPCEventRouter()
        router.attach(state: state, sessionID: sessionID)

        await router.route(
            try makeEvent("answer.started", #"{"session_id":"\#(sessionID.uuidString)","question_id":"q1","sequence":1}"#)
        )
        await router.route(
            try makeEvent("answer.slow_warning", #"{"session_id":"\#(sessionID.uuidString)","question_id":"q1","sequence":1}"#)
        )
        #expect(state.isAnswerSlow == true)

        await router.route(
            try makeEvent(
                "answer.speakable_delta",
                #"{"session_id":"\#(sessionID.uuidString)","question_id":"q1","delta":"Finally, an answer.","sequence":1}"#
            )
        )
        #expect(state.isAnswerSlow == false)
    }

    @Test("a delta with a stale sequence is dropped")
    func staleAnswerDeltaIsDropped() async throws {
        let sessionID = UUID()
        let (state, _) = await makeState(sessionID: sessionID)
        let router = IPCEventRouter()
        router.attach(state: state, sessionID: sessionID)

        await router.route(
            try makeEvent("answer.started", #"{"session_id":"\#(sessionID.uuidString)","question_id":"q1","sequence":2}"#)
        )
        // Sequence 1 is stale relative to the already-current sequence 2
        // (e.g. a late event from a question that was superseded).
        let staleEvent = try makeEvent(
            "answer.speakable_delta",
            #"{"session_id":"\#(sessionID.uuidString)","question_id":"q1","delta":"stale","sequence":1}"#
        )
        await router.route(staleEvent)

        #expect(state.partialAnswerText == nil)
    }

    @Test("an answer.started with a stale sequence does not restart generation")
    func staleAnswerStartedIsDropped() async throws {
        let sessionID = UUID()
        let (state, _) = await makeState(sessionID: sessionID)
        let router = IPCEventRouter()
        router.attach(state: state, sessionID: sessionID)

        await router.route(
            try makeEvent("answer.started", #"{"session_id":"\#(sessionID.uuidString)","question_id":"q2","sequence":2}"#)
        )
        await router.route(
            try makeEvent("answer.started", #"{"session_id":"\#(sessionID.uuidString)","question_id":"q1","sequence":1}"#)
        )
        // The stale (sequence 1) answer.started must not reset partial
        // state associated with the current (sequence 2) round.
        await router.route(
            try makeEvent(
                "answer.speakable_delta",
                #"{"session_id":"\#(sessionID.uuidString)","question_id":"q2","delta":"still going","sequence":2}"#
            )
        )

        #expect(state.partialAnswerText == "still going")
    }

    @Test("answer.completed sets currentAnswer, persists it, and clears loading/partial state")
    func answerCompleted() async throws {
        let sessionID = UUID()
        let (state, _) = await makeState(sessionID: sessionID)
        state.setAnswerGenerating(true)
        state.appendPartialAnswerDelta("partial")
        let router = IPCEventRouter()
        router.attach(state: state, sessionID: sessionID)

        await router.route(
            try makeEvent("answer.started", #"{"session_id":"\#(sessionID.uuidString)","question_id":"q1","sequence":1}"#)
        )

        let questionID = UUID()
        let event = try makeEvent(
            "answer.completed",
            #"""
            {"session_id":"\#(sessionID.uuidString)","question_id":"\#(questionID.uuidString)","question":"why?","talking_points":["a","b"],"sources":[{"document_id":"doc1","file_name":"Notes.pdf","chunk_id":"chunk1","excerpt":"a short excerpt"}],"sequence":1,"caveat":""}
            """#
        )
        await router.route(event)

        #expect(state.currentAnswer?.question == "why?")
        #expect(state.currentAnswer?.talkingPoints == ["a", "b"])
        #expect(state.currentAnswer?.sources == ["Notes.pdf: a short excerpt"])
        #expect(state.isGeneratingAnswer == false)
        #expect(state.partialAnswerText == nil)
    }

    @Test("answer.completed with is_failed true never overwrites currentAnswer, and surfaces a dismissable error")
    func answerCompletedFailurePreservesPriorAnswer() async throws {
        let sessionID = UUID()
        let (state, _) = await makeState(sessionID: sessionID)
        let router = IPCEventRouter()
        router.attach(state: state, sessionID: sessionID)

        await router.route(
            try makeEvent("answer.started", #"{"session_id":"\#(sessionID.uuidString)","question_id":"q1","sequence":1}"#)
        )
        let firstQuestionID = UUID()
        await router.route(
            try makeEvent(
                "answer.completed",
                #"""
                {"session_id":"\#(sessionID.uuidString)","question_id":"\#(firstQuestionID.uuidString)","question":"Tell me about yourself.","answer_text":"I'm a backend engineer...","talking_points":[],"sources":[],"sequence":1,"caveat":"","is_failed":false}
                """#
            )
        )
        #expect(state.currentAnswer?.answerText == "I'm a backend engineer...")

        await router.route(
            try makeEvent("answer.started", #"{"session_id":"\#(sessionID.uuidString)","question_id":"q2","sequence":2}"#)
        )
        let secondQuestionID = UUID()
        await router.route(
            try makeEvent(
                "answer.completed",
                #"""
                {"session_id":"\#(sessionID.uuidString)","question_id":"\#(secondQuestionID.uuidString)","question":"What did you mean by that?","answer_text":"Answer generation failed — the local LLM provider became unavailable mid-response.","talking_points":[],"sources":[],"sequence":2,"caveat":"","is_failed":true}
                """#
            )
        )

        // The real prior answer must still be the durable current answer
        // — the exact bug being fixed here: a failure silently
        // overwriting it with the failure status text.
        #expect(state.currentAnswer?.answerText == "I'm a backend engineer...")
        #expect(state.lastAnswerFailureMessage?.contains("became unavailable") == true)
        #expect(state.lastFailedQuestionID == secondQuestionID.uuidString)
        #expect(state.isGeneratingAnswer == false)
    }

    @Test("answer.completed folds a non-empty caveat into the talking points")
    func answerCompletedFoldsCaveat() async throws {
        let sessionID = UUID()
        let (state, _) = await makeState(sessionID: sessionID)
        let router = IPCEventRouter()
        router.attach(state: state, sessionID: sessionID)

        await router.route(
            try makeEvent("answer.started", #"{"session_id":"\#(sessionID.uuidString)","question_id":"q1","sequence":1}"#)
        )
        let event = try makeEvent(
            "answer.completed",
            #"""
            {"session_id":"\#(sessionID.uuidString)","question_id":"\#(UUID().uuidString)","question":"why?","talking_points":["a"],"sources":[],"sequence":1,"caveat":"assumes default config"}
            """#
        )
        await router.route(event)

        #expect(state.currentAnswer?.talkingPoints == ["a", "Caveat: assumes default config"])
    }

    @Test("turn.state routes to the matching ConversationState.TurnState")
    func turnStateRouting() async throws {
        let sessionID = UUID()
        let (state, _) = await makeState(sessionID: sessionID)
        let router = IPCEventRouter()
        router.attach(state: state, sessionID: sessionID)

        await router.route(try makeEvent("turn.state", #"{"session_id":"\#(sessionID.uuidString)","state":"speech"}"#))
        #expect(state.turnState == .speech)

        await router.route(try makeEvent("turn.state", #"{"session_id":"\#(sessionID.uuidString)","state":"waiting_for_silence"}"#))
        #expect(state.turnState == .waitingForSilence)

        await router.route(try makeEvent("turn.state", #"{"session_id":"\#(sessionID.uuidString)","state":"listening"}"#))
        #expect(state.turnState == .listening)
    }

    @Test("turn.debug appends a real VAD diagnostic sample")
    func turnDebugRouting() async throws {
        let sessionID = UUID()
        let (state, _) = await makeState(sessionID: sessionID)
        let router = IPCEventRouter()
        router.attach(state: state, sessionID: sessionID)

        await router.route(
            try makeEvent(
                "turn.debug",
                #"{"session_id":"\#(sessionID.uuidString)","rms":812.5,"threshold":400,"is_in_speech":true,"speech_seconds":1.2,"silence_seconds":0.0}"#
            )
        )

        #expect(state.vadDiagnostics.count == 1)
        #expect(state.vadDiagnostics[0].rms == 812.5)
        #expect(state.vadDiagnostics[0].threshold == 400)
        #expect(state.vadDiagnostics[0].isInSpeech == true)
    }

    @Test("question.candidate sets candidateQuestionText and candidateState")
    func questionCandidateRouting() async throws {
        let sessionID = UUID()
        let (state, _) = await makeState(sessionID: sessionID)
        let router = IPCEventRouter()
        router.attach(state: state, sessionID: sessionID)

        await router.route(
            try makeEvent("question.candidate", #"{"session_id":"\#(sessionID.uuidString)","text":"Tell me about yourself"}"#)
        )

        #expect(state.candidateQuestionText == "Tell me about yourself")
        #expect(state.candidateState == .candidate)
    }

    @Test("question.updated updates candidateQuestionText without changing state")
    func questionUpdatedRouting() async throws {
        let sessionID = UUID()
        let (state, _) = await makeState(sessionID: sessionID)
        let router = IPCEventRouter()
        router.attach(state: state, sessionID: sessionID)

        await router.route(
            try makeEvent("question.candidate", #"{"session_id":"\#(sessionID.uuidString)","text":"What was the bottleneck"}"#)
        )
        await router.route(
            try makeEvent(
                "question.updated",
                #"{"session_id":"\#(sessionID.uuidString)","text":"What was the bottleneck causing latency"}"#
            )
        )

        #expect(state.candidateQuestionText == "What was the bottleneck causing latency")
    }

    @Test("answer.draft_started begins a fresh draft and moves candidateState to drafting")
    func answerDraftStartedRouting() async throws {
        let sessionID = UUID()
        let (state, _) = await makeState(sessionID: sessionID)
        let router = IPCEventRouter()
        router.attach(state: state, sessionID: sessionID)

        await router.route(
            try makeEvent(
                "answer.draft_started",
                #"{"session_id":"\#(sessionID.uuidString)","question_id":"q1","sequence":1}"#
            )
        )

        #expect(state.isDraftingAnswer == true)
        #expect(state.draftSequence == 1)
        #expect(state.candidateState == .drafting)
    }

    @Test("answer.speakable_draft_delta appends to draftAnswerText only for the matching sequence")
    func answerDraftDeltaRouting() async throws {
        let sessionID = UUID()
        let (state, _) = await makeState(sessionID: sessionID)
        let router = IPCEventRouter()
        router.attach(state: state, sessionID: sessionID)

        await router.route(
            try makeEvent("answer.draft_started", #"{"session_id":"\#(sessionID.uuidString)","question_id":"q1","sequence":1}"#)
        )
        await router.route(
            try makeEvent(
                "answer.speakable_draft_delta",
                #"{"session_id":"\#(sessionID.uuidString)","question_id":"q1","delta":"I am ","sequence":1}"#
            )
        )
        await router.route(
            try makeEvent(
                "answer.speakable_draft_delta",
                #"{"session_id":"\#(sessionID.uuidString)","question_id":"q1","delta":"a backend engineer.","sequence":1}"#
            )
        )
        // A stale sequence (superseded draft's late delta) must never
        // mutate the currently-visible draft text.
        await router.route(
            try makeEvent(
                "answer.speakable_draft_delta",
                #"{"session_id":"\#(sessionID.uuidString)","question_id":"stale","delta":"SHOULD NOT APPEAR","sequence":0}"#
            )
        )

        #expect(state.draftAnswerText == "I am a backend engineer.")
    }

    @Test("answer.draft_replaced discards stale draft text and starts fresh under a new sequence")
    func answerDraftReplacedRouting() async throws {
        let sessionID = UUID()
        let (state, _) = await makeState(sessionID: sessionID)
        let router = IPCEventRouter()
        router.attach(state: state, sessionID: sessionID)

        await router.route(
            try makeEvent("answer.draft_started", #"{"session_id":"\#(sessionID.uuidString)","question_id":"q1","sequence":1}"#)
        )
        await router.route(
            try makeEvent("answer.speakable_draft_delta", #"{"session_id":"\#(sessionID.uuidString)","question_id":"q1","delta":"stale content","sequence":1}"#)
        )
        await router.route(
            try makeEvent("answer.draft_replaced", #"{"session_id":"\#(sessionID.uuidString)","question_id":"q2","sequence":2}"#)
        )

        // The old draft's content is gone — a fresh sequence, empty text.
        #expect(state.draftAnswerText == "")
        #expect(state.draftSequence == 2)

        // The old sequence's delta arriving late must not resurrect it.
        await router.route(
            try makeEvent("answer.speakable_draft_delta", #"{"session_id":"\#(sessionID.uuidString)","question_id":"q1","delta":"late stale delta","sequence":1}"#)
        )
        #expect(state.draftAnswerText == "")

        await router.route(
            try makeEvent("answer.speakable_draft_delta", #"{"session_id":"\#(sessionID.uuidString)","question_id":"q2","delta":"fresh content","sequence":2}"#)
        )
        #expect(state.draftAnswerText == "fresh content")
    }

    @Test("answer.draft_replaced after question.finalized marks the refinement as refining, not a fresh draft")
    func answerDraftReplacedAfterFinalizeIsRefining() async throws {
        let sessionID = UUID()
        let (state, _) = await makeState(sessionID: sessionID)
        let router = IPCEventRouter()
        router.attach(state: state, sessionID: sessionID)

        await router.route(
            try makeEvent("answer.draft_started", #"{"session_id":"\#(sessionID.uuidString)","question_id":"q1","sequence":1}"#)
        )
        await router.route(
            try makeEvent(
                "question.finalized",
                #"{"session_id":"\#(sessionID.uuidString)","question_id":"q1","text":"final text","confidence":0.9}"#
            )
        )
        #expect(state.candidateState == .finalized)
        #expect(state.finalizedQuestionText == "final text")

        await router.route(
            try makeEvent("answer.draft_replaced", #"{"session_id":"\#(sessionID.uuidString)","question_id":"q1","sequence":2}"#)
        )
        #expect(state.isRefiningAnswer == true)
    }

    @Test("answer.cancelled clears the matching draft, ignoring a stale sequence")
    func answerCancelledRouting() async throws {
        let sessionID = UUID()
        let (state, _) = await makeState(sessionID: sessionID)
        let router = IPCEventRouter()
        router.attach(state: state, sessionID: sessionID)

        await router.route(
            try makeEvent("answer.draft_started", #"{"session_id":"\#(sessionID.uuidString)","question_id":"q1","sequence":1}"#)
        )
        await router.route(
            try makeEvent("answer.cancelled", #"{"session_id":"\#(sessionID.uuidString)","question_id":"stale","sequence":0}"#)
        )
        #expect(state.isDraftingAnswer == true)  // stale sequence ignored

        await router.route(
            try makeEvent("answer.cancelled", #"{"session_id":"\#(sessionID.uuidString)","question_id":"q1","sequence":1}"#)
        )
        #expect(state.isDraftingAnswer == false)
        #expect(state.draftAnswerText == "")
    }

    @Test("a full candidate -> draft -> finalize -> completed sequence never shows a stale/empty state")
    func fullCandidateToCompletedSequence() async throws {
        let sessionID = UUID()
        let questionID = UUID()
        let (state, _) = await makeState(sessionID: sessionID)
        let router = IPCEventRouter()
        router.attach(state: state, sessionID: sessionID)

        await router.route(
            try makeEvent("question.candidate", #"{"session_id":"\#(sessionID.uuidString)","text":"Tell me about yourself"}"#)
        )
        #expect(state.candidateState == .candidate)

        await router.route(
            try makeEvent("answer.draft_started", #"{"session_id":"\#(sessionID.uuidString)","question_id":"\#(questionID.uuidString)","sequence":1}"#)
        )
        #expect(state.candidateState == .drafting)
        #expect(state.isDraftingAnswer == true)

        // Real orchestrator traffic always emits `answer.started`
        // alongside `answer.draft_started` — the older, stable event
        // `IPCEventRouter` already used for stale-sequence guarding on
        // `answer.completed`.
        await router.route(
            try makeEvent("answer.started", #"{"session_id":"\#(sessionID.uuidString)","question_id":"\#(questionID.uuidString)","sequence":1}"#)
        )

        await router.route(
            try makeEvent(
                "question.finalized",
                #"{"session_id":"\#(sessionID.uuidString)","question_id":"\#(questionID.uuidString)","text":"Tell me about yourself","confidence":0.9}"#
            )
        )
        #expect(state.candidateState == .finalized)

        await router.route(
            try makeEvent(
                "answer.completed",
                #"{"session_id":"\#(sessionID.uuidString)","question_id":"\#(questionID.uuidString)","question":"Tell me about yourself","talking_points":["a point"],"sources":[],"sequence":1,"caveat":""}"#
            )
        )
        #expect(state.currentAnswer != nil)
        #expect(state.isDraftingAnswer == false)
        #expect(state.candidateState == .idle)
    }

    @Test("question.classifying sets isClassifyingQuestion, cleared by question.detected")
    func questionClassifyingThenDetected() async throws {
        let sessionID = UUID()
        let (state, _) = await makeState(sessionID: sessionID)
        let router = IPCEventRouter()
        router.attach(state: state, sessionID: sessionID)

        await router.route(try makeEvent("question.classifying", #"{"session_id":"\#(sessionID.uuidString)"}"#))
        #expect(state.isClassifyingQuestion == true)

        await router.route(
            try makeEvent(
                "question.detected",
                #"{"session_id":"\#(sessionID.uuidString)","question_id":"\#(UUID().uuidString)","text":"why?","confidence":0.9,"detected_at":0.0}"#
            )
        )
        #expect(state.isClassifyingQuestion == false)
    }

    @Test("question.classifying cleared by question.rejected when the turn isn't an answer request")
    func questionClassifyingThenRejected() async throws {
        let sessionID = UUID()
        let (state, _) = await makeState(sessionID: sessionID)
        let router = IPCEventRouter()
        router.attach(state: state, sessionID: sessionID)

        await router.route(try makeEvent("question.classifying", #"{"session_id":"\#(sessionID.uuidString)"}"#))
        #expect(state.isClassifyingQuestion == true)

        await router.route(try makeEvent("question.rejected", #"{"session_id":"\#(sessionID.uuidString)"}"#))
        #expect(state.isClassifyingQuestion == false)
    }

    @Test("turn-state events for a different session id than the attached one are dropped")
    func turnStateMismatchedSessionIsDropped() async throws {
        let attachedSessionID = UUID()
        let otherSessionID = UUID()
        let (state, _) = await makeState(sessionID: attachedSessionID)
        let router = IPCEventRouter()
        router.attach(state: state, sessionID: attachedSessionID)

        await router.route(try makeEvent("turn.state", #"{"session_id":"\#(otherSessionID.uuidString)","state":"speech"}"#))
        #expect(state.turnState == .listening)
    }

    @Test("events for a different session id than the attached one are dropped")
    func mismatchedSessionIsDropped() async throws {
        let attachedSessionID = UUID()
        let otherSessionID = UUID()
        let (state, _) = await makeState(sessionID: attachedSessionID)
        let router = IPCEventRouter()
        router.attach(state: state, sessionID: attachedSessionID)

        let event = try makeEvent(
            "transcript.partial",
            #"{"session_id":"\#(otherSessionID.uuidString)","text":"should be ignored"}"#
        )
        await router.route(event)

        #expect(state.partialTranscriptText == nil)
    }

    @Test("events routed after detach() are dropped")
    func eventsAfterDetachAreDropped() async throws {
        let sessionID = UUID()
        let (state, _) = await makeState(sessionID: sessionID)
        let router = IPCEventRouter()
        router.attach(state: state, sessionID: sessionID)
        router.detach()

        let event = try makeEvent(
            "transcript.partial",
            #"{"session_id":"\#(sessionID.uuidString)","text":"should be ignored"}"#
        )
        await router.route(event)

        #expect(state.partialTranscriptText == nil)
    }

    @Test("a full ordered sequence of events integrates end-to-end into ConversationState")
    func fullSequenceIntegration() async throws {
        let sessionID = UUID()
        let (state, repository) = await makeState(sessionID: sessionID)
        state.beginPythonDrivenSession()
        let router = IPCEventRouter()
        router.attach(state: state, sessionID: sessionID)

        let questionID = UUID()
        let segmentID = UUID()

        await router.route(try makeEvent("session.started", #"{"session_id":"\#(sessionID.uuidString)"}"#))
        await router.route(
            try makeEvent("transcript.partial", #"{"session_id":"\#(sessionID.uuidString)","text":"why"}"#)
        )
        await router.route(
            try makeEvent(
                "transcript.final",
                #"""
                {"session_id":"\#(sessionID.uuidString)","id":"\#(segmentID.uuidString)","text":"why did it take six weeks?","started_at":0.0,"ended_at":1.0,"is_final":true}
                """#
            )
        )
        await router.route(
            try makeEvent(
                "question.detected",
                #"{"session_id":"\#(sessionID.uuidString)","question_id":"\#(questionID.uuidString)","text":"why did it take six weeks?","confidence":0.9,"detected_at":0.0}"#
            )
        )
        await router.route(
            try makeEvent(
                "answer.started",
                #"{"session_id":"\#(sessionID.uuidString)","question_id":"\#(questionID.uuidString)","sequence":1}"#
            )
        )
        await router.route(
            try makeEvent(
                "answer.completed",
                #"""
                {"session_id":"\#(sessionID.uuidString)","question_id":"\#(questionID.uuidString)","question":"why did it take six weeks?","talking_points":["Authentication dependency"],"sources":[{"document_id":"doc1","file_name":"Migration Notes","chunk_id":"chunk1","excerpt":"staged rollout details"}],"sequence":1,"caveat":""}
                """#
            )
        )
        await router.route(try makeEvent("session.ended", #"{"session_id":"\#(sessionID.uuidString)"}"#))

        #expect(state.phase == .live)
        #expect(state.segments.count == 1)
        #expect(state.detectedQuestions.count == 1)
        #expect(state.currentAnswer?.sources == ["Migration Notes: staged rollout details"])

        // And it's actually persisted through the repository, not just held
        // transiently in @Published state.
        let persistedSegments = try await repository.transcript(sessionID: sessionID)
        let persistedQuestions = try await repository.questions(sessionID: sessionID)
        let persistedAnswers = try await repository.answers(sessionID: sessionID)
        #expect(persistedSegments.count == 1)
        #expect(persistedQuestions.count == 1)
        #expect(persistedAnswers.count == 1)
    }

    @Test("knowledge.ingestion_started marks the document as indexing")
    func knowledgeIngestionStartedMarksIndexing() async throws {
        let tracker = KnowledgeIngestionTracker()
        let router = IPCEventRouter(knowledgeTracker: tracker)
        let documentID = UUID()

        await router.route(
            try makeEvent(
                "knowledge.ingestion_started",
                #"{"session_id":"s1","document_id":"\#(documentID.uuidString)","file_name":"notes.txt"}"#
            )
        )

        #expect(tracker.status(forDocumentID: documentID) == .indexing)
    }

    @Test("knowledge.ingestion_completed marks the document as ready")
    func knowledgeIngestionCompletedMarksReady() async throws {
        let tracker = KnowledgeIngestionTracker()
        let router = IPCEventRouter(knowledgeTracker: tracker)
        let documentID = UUID()
        tracker.setStatus(.indexing, forDocumentID: documentID)

        await router.route(
            try makeEvent(
                "knowledge.ingestion_completed",
                #"{"session_id":"s1","document_id":"\#(documentID.uuidString)","file_name":"notes.txt","chunk_count":3}"#
            )
        )

        #expect(tracker.status(forDocumentID: documentID) == .ready)
    }

    @Test("knowledge.ingestion_failed marks the document with its typed status and reason")
    func knowledgeIngestionFailedMarksFailure() async throws {
        let tracker = KnowledgeIngestionTracker()
        let router = IPCEventRouter(knowledgeTracker: tracker)
        let documentID = UUID()

        await router.route(
            try makeEvent(
                "knowledge.ingestion_failed",
                #"{"session_id":"s1","document_id":"\#(documentID.uuidString)","file_name":"notes.exe","status":"unsupported","reason":"'.exe' is not supported."}"#
            )
        )

        #expect(tracker.status(forDocumentID: documentID) == .unsupported)
        #expect(tracker.failureReasonByDocumentID[documentID] == "'.exe' is not supported.")
    }

    @Test("knowledge events are routed regardless of whether a Live Session is attached")
    func knowledgeEventsRouteWithoutAnAttachedSession() async throws {
        let tracker = KnowledgeIngestionTracker()
        let router = IPCEventRouter(knowledgeTracker: tracker)
        let documentID = UUID()
        // Deliberately never call `router.attach(...)`.

        await router.route(
            try makeEvent(
                "knowledge.ingestion_completed",
                #"{"session_id":"s1","document_id":"\#(documentID.uuidString)","file_name":"notes.txt","chunk_count":1}"#
            )
        )

        #expect(tracker.status(forDocumentID: documentID) == .ready)
    }
}
