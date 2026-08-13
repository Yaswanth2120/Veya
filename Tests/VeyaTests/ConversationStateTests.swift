import Foundation
import Testing
@testable import Veya

@MainActor
@Suite("ConversationState")
struct ConversationStateTests {
    private func makeState(sessionID: UUID = UUID()) -> ConversationState {
        ConversationState(
            sessionID: sessionID,
            repository: ConversationRepository(db: DatabaseManager.makeInMemory())
        )
    }

    private func makeSegment(sessionID: UUID, text: String, isFinal: Bool = true) -> TranscriptSegment {
        TranscriptSegment(
            id: UUID(),
            sessionID: sessionID,
            text: text,
            startedAt: 0,
            endedAt: 1,
            isFinal: isFinal,
            speakerRole: "unknown"
        )
    }

    @Test("non-question segments are appended but produce no question or answer")
    func nonQuestionSegment() async {
        let sessionID = UUID()
        let state = makeState(sessionID: sessionID)

        await state.ingest(makeSegment(sessionID: sessionID, text: "We moved the auth service first."))

        #expect(state.segments.count == 1)
        #expect(state.detectedQuestions.isEmpty)
        #expect(state.currentAnswer == nil)
    }

    @Test("a question-shaped segment produces a detected question and a canned answer")
    func questionSegmentDrivesFullPipeline() async {
        let sessionID = UUID()
        let state = makeState(sessionID: sessionID)

        await state.ingest(makeSegment(sessionID: sessionID, text: "So why did the migration take six weeks?"))

        #expect(state.segments.count == 1)
        #expect(state.detectedQuestions.count == 1)
        #expect(state.detectedQuestions.first?.text == "So why did the migration take six weeks?")
        #expect(state.currentAnswer != nil)
        #expect(state.currentAnswer?.talkingPoints.isEmpty == false)
        #expect(state.currentAnswer?.sessionID == sessionID)
    }

    @Test("non-final segments never trigger question detection")
    func nonFinalSegmentIsIgnoredForDetection() async {
        let sessionID = UUID()
        let state = makeState(sessionID: sessionID)

        await state.ingest(makeSegment(sessionID: sessionID, text: "Why did this take so long?", isFinal: false))

        #expect(state.segments.count == 1)
        #expect(state.detectedQuestions.isEmpty)
    }

    @Test("segments accumulate across multiple ingests in order")
    func segmentsAccumulateInOrder() async {
        let sessionID = UUID()
        let state = makeState(sessionID: sessionID)

        await state.ingest(makeSegment(sessionID: sessionID, text: "First line."))
        await state.ingest(makeSegment(sessionID: sessionID, text: "Second line."))

        #expect(state.segments.map(\.text) == ["First line.", "Second line."])
    }

    @Test("start transitions phase to live, end transitions to ended")
    func phaseTransitions() async {
        let state = makeState()
        #expect(state.phase == .idle)

        state.start(source: MockTranscriptSource(script: [], intervalSeconds: 0))
        #expect(state.phase == .live)

        state.end()
        #expect(state.phase == .ended)
    }

    @Test("ingestDetectedQuestion sets isAnalyzingQuestion, cleared once answer generation starts")
    func isAnalyzingQuestionLifecycle() async {
        let sessionID = UUID()
        let state = makeState(sessionID: sessionID)
        #expect(state.isAnalyzingQuestion == false)

        let question = DetectedQuestion(id: UUID(), sessionID: sessionID, text: "Why?", detectedAt: Date())
        await state.ingestDetectedQuestion(question)
        #expect(state.isAnalyzingQuestion == true)
        #expect(state.isGeneratingAnswer == false)

        state.setAnswerGenerating(true)
        #expect(state.isAnalyzingQuestion == false)
        #expect(state.isGeneratingAnswer == true)
    }

    @Test("ingestTranscriptSegment (the Python-driven path) never runs the Swift canned question detector")
    func ingestTranscriptSegmentNeverRunsCannedDetection() async {
        // This is the entry point both the Python mock feed AND real
        // transcription route final transcript text through
        // (`IPCEventRouter`) — unlike `ingest(_:)` (the Swift-fallback-only
        // entry point), it must never independently decide a segment is a
        // question, even for text that obviously looks like one.
        let sessionID = UUID()
        let state = makeState(sessionID: sessionID)

        await state.ingestTranscriptSegment(
            makeSegment(sessionID: sessionID, text: "So why did the migration take six weeks?")
        )

        #expect(state.segments.count == 1)
        #expect(state.detectedQuestions.isEmpty)
        #expect(state.currentAnswer == nil)
    }

    @Test("cancelPendingAnswerActivity clears analyzing/generating/partial state without touching persisted data")
    func cancelPendingAnswerActivityClearsTransientState() async {
        let sessionID = UUID()
        let state = makeState(sessionID: sessionID)

        let question = DetectedQuestion(id: UUID(), sessionID: sessionID, text: "Why?", detectedAt: Date())
        await state.ingestDetectedQuestion(question)
        state.setAnswerGenerating(true)
        state.setPartialAnswer("partial text")

        state.cancelPendingAnswerActivity()

        #expect(state.isAnalyzingQuestion == false)
        #expect(state.isGeneratingAnswer == false)
        #expect(state.partialAnswerText == nil)
        // The already-persisted question itself is untouched.
        #expect(state.detectedQuestions.count == 1)
    }
}
