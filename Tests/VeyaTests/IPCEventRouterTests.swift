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

    @Test("answer.delta sets the transient partial-answer text")
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
            "answer.delta",
            #"{"session_id":"\#(sessionID.uuidString)","question_id":"q1","delta":"Staged rollout","sequence":1}"#
        )
        await router.route(event)

        #expect(state.partialAnswerText == "Staged rollout")
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
            "answer.delta",
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
                "answer.delta",
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
        state.setPartialAnswer("partial")
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
