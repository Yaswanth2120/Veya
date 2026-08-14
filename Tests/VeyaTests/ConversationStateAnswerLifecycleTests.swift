import Foundation
import Testing
@testable import Veya

/// Section 17: the exact failure mode the reviewer reported — a
/// completed answer disappearing behind "Generating answer…" for the
/// next question, and a failed generation silently overwriting a real
/// prior answer. These are state-machine invariants `LiveSessionView`/
/// `OverlayView` both build on.
@MainActor
@Suite("ConversationState answer lifecycle (Section 17)")
struct ConversationStateAnswerLifecycleTests {
    private func makeState(sessionID: UUID = UUID()) -> ConversationState {
        ConversationState(sessionID: sessionID, repository: ConversationRepository(db: DatabaseManager.makeInMemory()))
    }

    private func makeAnswer(sessionID: UUID, question: String, answerText: String) -> CopilotAnswer {
        CopilotAnswer(
            id: UUID(), sessionID: sessionID, questionID: UUID(), question: question,
            answerText: answerText, talkingPoints: [], sources: [], generatedAt: Date()
        )
    }

    @Test("a completed answer stays set while the next answer round starts generating")
    func completedAnswerSurvivesNextRoundStarting() async {
        let sessionID = UUID()
        let state = makeState(sessionID: sessionID)
        let firstAnswer = makeAnswer(sessionID: sessionID, question: "Tell me about yourself.", answerText: "I'm a backend engineer...")
        await state.ingestAnswer(firstAnswer)
        #expect(state.currentAnswer?.id == firstAnswer.id)

        // A new question starts drafting/generating — the prior answer
        // must remain the durable `currentAnswer` until a *new* one
        // actually completes; only `IPCEventRouter`'s `ingestAnswer` call
        // (on real completion) is allowed to replace it.
        state.setQuestionFinalized("What did you mean by that?")
        state.beginDraftAnswer(sequence: 2, isReplacement: false)
        state.appendDraftDelta("The pipeline was", sequence: 2)
        state.setAnswerGenerating(true)

        #expect(state.currentAnswer?.id == firstAnswer.id)
        #expect(state.isDraftingAnswer || state.isGeneratingAnswer)
    }

    @Test("a streamed delta is visible immediately, before the round completes")
    func streamedDeltaIsVisibleImmediately() {
        let state = makeState()
        state.setAnswerGenerating(true)
        state.setPartialAnswer("The pipeline was optimized")
        #expect(state.partialAnswerText == "The pipeline was optimized")
    }

    @Test("a streamed draft delta is visible immediately, before the draft finalizes")
    func streamedDraftDeltaIsVisibleImmediately() {
        let state = makeState()
        state.beginDraftAnswer(sequence: 1, isReplacement: false)
        state.appendDraftDelta("I led the", sequence: 1)
        state.appendDraftDelta(" migration project.", sequence: 1)
        #expect(state.draftAnswerText == "I led the migration project.")
    }

    @Test("a generation failure preserves the prior completed answer and surfaces a dismissable error")
    func failurePreservesPriorAnswer() async {
        let sessionID = UUID()
        let state = makeState(sessionID: sessionID)
        let firstAnswer = makeAnswer(sessionID: sessionID, question: "Tell me about yourself.", answerText: "I'm a backend engineer...")
        await state.ingestAnswer(firstAnswer)

        state.ingestAnswerFailure(
            "Answer generation failed — the local LLM provider became unavailable mid-response.",
            questionID: "q2", questionText: "What did you mean by that?"
        )

        // The real prior answer must never be overwritten by the failure
        // text — this is the exact bug: failure silently replacing a
        // good answer.
        #expect(state.currentAnswer?.id == firstAnswer.id)
        #expect(state.currentAnswer?.answerText == "I'm a backend engineer...")
        #expect(state.lastAnswerFailureMessage != nil)
        #expect(state.lastFailedQuestionID == "q2")
        #expect(state.isGeneratingAnswer == false)

        state.dismissAnswerFailure()
        #expect(state.lastAnswerFailureMessage == nil)
        #expect(state.lastFailedQuestionID == nil)
    }

    @Test("a successful completion clears any stale failure state")
    func successClearsStaleFailure() async {
        let sessionID = UUID()
        let state = makeState(sessionID: sessionID)
        state.ingestAnswerFailure("failed", questionID: "q1", questionText: "Why?")
        #expect(state.lastAnswerFailureMessage != nil)

        let answer = makeAnswer(sessionID: sessionID, question: "Why?", answerText: "Because...")
        await state.ingestAnswer(answer)

        #expect(state.lastAnswerFailureMessage == nil)
        #expect(state.lastFailedQuestionID == nil)
        #expect(state.currentAnswer?.id == answer.id)
    }

    @Test("queue depth updates are reflected as an honest pending count")
    func queueDepthIsTracked() {
        let state = makeState()
        #expect(state.queuedQuestionsCount == 0)
        state.setQueuedQuestionsCount(2)
        #expect(state.queuedQuestionsCount == 2)
    }

    @Test("a queue overflow is surfaced explicitly, not silently dropped")
    func queueOverflowIsSurfaced() {
        let state = makeState()
        state.noteQueueOverflow("What's your greatest weakness?")
        #expect(state.queueOverflowMessage?.contains("What's your greatest weakness?") == true)
        state.dismissQueueOverflow()
        #expect(state.queueOverflowMessage == nil)
    }
}
