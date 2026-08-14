import SwiftUI

struct LiveSessionView: View {
    @EnvironmentObject private var coordinator: AppCoordinator
    @ObservedObject var conversationState: ConversationState
    @ObservedObject private var pythonIntelligenceCoordinator: PythonIntelligenceCoordinator
    @ObservedObject private var knowledgeIngestionTracker: KnowledgeIngestionTracker
    @State private var sessionDocuments: [SessionDocument] = []
    @State private var lastLLMStatus: LLMStatusResult?
    @State private var isCheckingLocalAI = false
    private let documentRepository = SessionDocumentRepository()

    init(conversationState: ConversationState, pythonIntelligenceCoordinator: PythonIntelligenceCoordinator) {
        self.conversationState = conversationState
        self.pythonIntelligenceCoordinator = pythonIntelligenceCoordinator
        self.knowledgeIngestionTracker = pythonIntelligenceCoordinator.knowledgeIngestionTracker
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            header
            pipelineStatusStrip

            HStack(alignment: .top, spacing: 20) {
                VStack(alignment: .leading, spacing: 16) {
                    answerPanel
                    transcriptPanel
                }
                sidePanel
            }
            if let session = coordinator.currentSession,
               session.sessionType == .codingPractice || session.sessionType == .systemDesign {
                CopilotWorkbenchView(session: session)
            }
            #if DEBUG
            VADDiagnosticsView(conversationState: conversationState)
            #endif
        }
        .padding(28)
        .task {
            sessionDocuments = (try? await documentRepository.fetchAll(sessionID: conversationState.sessionID)) ?? []
        }
    }

    private var header: some View {
        HStack {
            VStack(alignment: .leading, spacing: 2) {
                Text("Live Session")
                    .font(.largeTitle.bold())
                Text(statusText)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Spacer()
            Button("End Session") {
                coordinator.endLiveSession()
            }
            .buttonStyle(.borderedProminent)
            .tint(.red)
        }
    }

    private var statusText: String {
        switch conversationState.phase {
        case .idle: return "Not started"
        case .live: return "Live"
        case .ended: return "Session ended"
        }
    }

    // MARK: - Pipeline status

    /// The six honest states the build prompt requires — computed in
    /// strict priority order so the UI never shows two conflicting things
    /// at once, and never shows "Listening"/"No question detected yet"
    /// while a spoken turn is actually still in flight.
    private enum PipelineStep: CaseIterable {
        case unavailable, refining, drafting, generating, understanding, hearingQuestion, waitingForSilence, transcribing, listening

        var label: String {
            switch self {
            case .listening: return "Listening"
            case .transcribing: return "Transcribing"
            case .hearingQuestion: return "Hearing a question…"
            case .waitingForSilence: return "Waiting for speaker to finish"
            case .understanding: return "Understanding question"
            case .drafting: return "Drafting answer…"
            case .refining: return "Refining answer…"
            case .generating: return "Generating answer"
            case .unavailable: return "Local AI unavailable"
            }
        }
    }

    /// Section 15: `true` whenever *any* candidate/draft/finalize/
    /// generation activity is in flight for the turn currently being
    /// spoken — the one condition that must always suppress the
    /// completed-answer view and "No question detected yet", so a newer
    /// turn's work in progress is never hidden behind a stale answer.
    private var isAnswerRoundInFlight: Bool {
        conversationState.isGeneratingAnswer
            || conversationState.isDraftingAnswer
            || conversationState.isClassifyingQuestion
            || conversationState.isAnalyzingQuestion
            || conversationState.candidateState == .candidate
    }

    private var activeStep: PipelineStep {
        if isAnswerIntelligenceUnavailable { return .unavailable }
        if conversationState.isRefiningAnswer { return .refining }
        if conversationState.isDraftingAnswer { return .drafting }
        if conversationState.isGeneratingAnswer { return .generating }
        if conversationState.isClassifyingQuestion || conversationState.isAnalyzingQuestion { return .understanding }
        if conversationState.candidateState == .candidate { return .hearingQuestion }
        if conversationState.turnState == .waitingForSilence { return .waitingForSilence }
        if conversationState.turnState == .speech || conversationState.partialTranscriptText != nil { return .transcribing }
        return .listening
    }

    /// Never claims AI is working when it isn't: real transcription can
    /// keep flowing with no answer intelligence at all, and this is the
    /// one place that distinction is made unmissable rather than folded
    /// into a single ambiguous status line.
    private var isAnswerIntelligenceUnavailable: Bool {
        switch pythonIntelligenceCoordinator.drivingSource {
        case .realTranscription:
            return !pythonIntelligenceCoordinator.answerIntelligenceAvailable
        case .pythonWorker, .swiftFallback:
            return true
        case .none:
            return false
        }
    }

    private var pipelineStatusStrip: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                Circle()
                    .fill(activeStep == .unavailable ? Color.orange : Color.accentColor)
                    .frame(width: 8, height: 8)
                Text(activeStep.label)
                    .font(.caption.bold())
                    .foregroundStyle(activeStep == .unavailable ? Color.orange : Color.accentColor)
                Spacer()
            }

            if isAnswerIntelligenceUnavailable {
                HStack {
                    Label(pythonIntelligenceCoordinator.liveSessionIndicatorText, systemImage: "exclamationmark.triangle.fill")
                        .font(.caption)
                        .foregroundStyle(.orange)
                    Button(isCheckingLocalAI ? "Checking…" : "Check Local AI") {
                        Task {
                            isCheckingLocalAI = true
                            lastLLMStatus = await pythonIntelligenceCoordinator.fetchLLMStatus()
                            isCheckingLocalAI = false
                        }
                    }
                    .font(.caption)
                    .disabled(isCheckingLocalAI)
                    Button("Open Local AI Settings") { coordinator.route = .localAIStatus }
                        .font(.caption)
                        .buttonStyle(.link)
                    Spacer()
                }
                if let status = lastLLMStatus, !status.reachable {
                    Text("Ollama isn't reachable. Answers will not be generated until it's running.")
                        .font(.caption2).foregroundStyle(.secondary)
                } else if let status = lastLLMStatus, !status.modelInstalled {
                    Text("Configured model \"\(status.configuredModel)\" isn't installed. Pull it or pick a different one in Local AI Settings.")
                        .font(.caption2).foregroundStyle(.secondary)
                }
            }
        }
        .padding(10)
        .background(.quaternary.opacity(0.15), in: RoundedRectangle(cornerRadius: 8))
    }

    // MARK: - Answer panel

    @ViewBuilder
    private var answerPanel: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("ANSWER").font(.caption.bold()).foregroundStyle(.secondary)

            if let failureMessage = conversationState.lastAnswerFailureMessage {
                answerFailureBanner(failureMessage)
            }

            // A completed answer is always shown once one exists — it is
            // never hidden just because a newer round (candidate, draft,
            // refinement, or generation) is in flight; the in-flight
            // content is shown *above* it instead, and this is only
            // actually replaced once a newer answer completes.
            if isAnswerRoundInFlight {
                inFlightContent
                if let answer = conversationState.currentAnswer {
                    previousAnswerView(answer)
                }
            } else if let answer = conversationState.currentAnswer {
                answerContent(answer)
            } else {
                idleContent
            }

            if conversationState.queuedQuestionsCount > 0 {
                Text("\(conversationState.queuedQuestionsCount) question\(conversationState.queuedQuestionsCount == 1 ? "" : "s") pending")
                    .font(.caption).foregroundStyle(.secondary)
            }
            if let overflowMessage = conversationState.queueOverflowMessage {
                HStack(alignment: .top) {
                    Text(overflowMessage).font(.caption2).foregroundStyle(.orange)
                    Spacer()
                    Button("Dismiss") { conversationState.dismissQueueOverflow() }.font(.caption2)
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .topLeading)
        .padding(16)
        .background(Color.accentColor.opacity(0.08), in: RoundedRectangle(cornerRadius: 12))
        .overlay(RoundedRectangle(cornerRadius: 12).stroke(Color.accentColor.opacity(0.25), lineWidth: 1))
    }

    @ViewBuilder
    private func answerFailureBanner(_ message: String) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Label(message, systemImage: "exclamationmark.triangle.fill")
                .font(.callout)
                .foregroundStyle(.orange)
            HStack {
                if let questionID = conversationState.lastFailedQuestionID,
                   let questionText = conversationState.lastFailedQuestionText {
                    Button("Retry") {
                        Task { await pythonIntelligenceCoordinator.retryFailedAnswer(questionID: questionID, questionText: questionText) }
                    }
                    .font(.caption)
                }
                Button("Dismiss") { conversationState.dismissAnswerFailure() }
                    .font(.caption)
            }
        }
        .padding(10)
        .background(Color.orange.opacity(0.1), in: RoundedRectangle(cornerRadius: 8))
    }

    @ViewBuilder
    private func answerContent(_ answer: CopilotAnswer) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(answer.question).font(.headline)
            // The natural, speakable answer leads — talking points are
            // optional supporting detail, shown expanded here (Live
            // Session has room, unlike the compact overlay) but never
            // presented as the primary content on their own (Section 16).
            if !answer.answerText.isEmpty {
                Text(answer.answerText).font(.body)
            }
            if !answer.talkingPoints.isEmpty {
                DisclosureGroup("Details") {
                    VStack(alignment: .leading, spacing: 4) {
                        ForEach(answer.talkingPoints, id: \.self) { point in
                            Text("• \(point)").font(.callout).foregroundStyle(.secondary)
                        }
                    }
                }
                .font(.caption.bold())
            }
            if !answer.sources.isEmpty {
                Text("Sources").font(.caption.bold()).foregroundStyle(.secondary)
                ForEach(answer.sources, id: \.self) { Text("• \($0)").font(.caption) }
            }
        }
    }

    /// A compact, visually de-emphasized rendering of the last completed
    /// answer, shown below in-flight content while a newer answer is
    /// being drafted/generated — never removed until the newer one
    /// actually completes.
    @ViewBuilder
    private func previousAnswerView(_ answer: CopilotAnswer) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("PREVIOUS ANSWER").font(.caption2.bold()).foregroundStyle(.secondary)
            Text(answer.question).font(.subheadline).foregroundStyle(.secondary)
            if !answer.answerText.isEmpty {
                Text(answer.answerText).font(.callout).foregroundStyle(.secondary)
            }
        }
        .padding(.top, 8)
        .opacity(0.7)
    }

    @ViewBuilder
    private var inFlightContent: some View {
        switch activeStep {
        case .refining, .drafting:
            VStack(alignment: .leading, spacing: 6) {
                if let questionText = conversationState.finalizedQuestionText ?? conversationState.candidateQuestionText {
                    Text(questionText).font(.headline)
                }
                HStack { ProgressView().controlSize(.small); Text(activeStep.label).font(.callout) }
                // The draft's own streamed text is the prominent content
                // here — it's real generated content, not a placeholder,
                // even before the turn finalizes.
                if !conversationState.draftAnswerText.isEmpty {
                    Text(conversationState.draftAnswerText).font(.body)
                }
            }
        case .generating:
            VStack(alignment: .leading, spacing: 6) {
                HStack { ProgressView().controlSize(.small); Text("Generating answer…").font(.callout) }
                if let partial = conversationState.partialAnswerText, !partial.isEmpty {
                    Text(partial).font(.callout)
                }
            }
        case .understanding:
            HStack { ProgressView().controlSize(.small); Text("Understanding question…").font(.callout) }
        case .hearingQuestion:
            VStack(alignment: .leading, spacing: 6) {
                Text("Hearing a question…").font(.callout).foregroundStyle(.secondary)
                if let candidateText = conversationState.candidateQuestionText, !candidateText.isEmpty {
                    Text(candidateText).font(.callout.italic())
                }
            }
        case .waitingForSilence, .unavailable, .transcribing, .listening:
            // These states never set `isAnswerRoundInFlight`, so
            // `inFlightContent` is never reached for them — see
            // `activeStep`/`isAnswerRoundInFlight`.
            EmptyView()
        }
    }

    @ViewBuilder
    private var idleContent: some View {
        switch activeStep {
        case .waitingForSilence:
            Text("Waiting for the speaker to finish…").font(.callout).foregroundStyle(.secondary)
        case .unavailable:
            Text("Local AI isn't configured — questions will be detected in the transcript, but no answer will be generated.")
                .font(.callout).foregroundStyle(.secondary)
        case .transcribing:
            Text("Listening for a question…").font(.callout).foregroundStyle(.secondary)
        case .listening:
            Text("No question detected yet.").font(.callout).foregroundStyle(.secondary)
        case .refining, .drafting, .generating, .understanding, .hearingQuestion:
            // Unreachable: any of these implies `isAnswerRoundInFlight`,
            // which routes to `inFlightContent` instead.
            EmptyView()
        }
    }

    private var transcriptPanel: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("TRANSCRIPT")
                .font(.caption.bold())
                .foregroundStyle(.secondary)

            ScrollView {
                VStack(alignment: .leading, spacing: 6) {
                    ForEach(TranscriptDisplayFiltering.displayable(conversationState.segments)) { segment in
                        VStack(alignment: .leading, spacing: 1) {
                            // Section 16: a plain, human lane label — never
                            // the raw "meeting_audio"/"microphone" source
                            // string. Omitted entirely for "unknown" (single-
                            // track/mixed mode), where the app has no
                            // reliable way to tell who was speaking.
                            if let laneLabel = transcriptLaneLabel(for: segment) {
                                Text(laneLabel)
                                    .font(.caption2.bold())
                                    .foregroundStyle(.secondary)
                            }
                            Text(segment.text)
                                .font(.callout)
                        }
                        .padding(.vertical, 2)
                    }
                    if let partial = conversationState.partialTranscriptText, !partial.isEmpty {
                        Text(partial)
                            .font(.callout)
                            .foregroundStyle(.secondary)
                            .italic()
                    }
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .topLeading)
        .padding(12)
        .background(.quaternary.opacity(0.25), in: RoundedRectangle(cornerRadius: 10))
    }

    /// Section 16: the mixed/microphone-only mode "I'm answering"
    /// fallback control — a visible toggle alongside the ⌘⇧A hotkey,
    /// with an explicit disclosure that automatic speaker identity isn't
    /// reliable outside separated-track mode.
    private var imAnsweringControl: some View {
        VStack(alignment: .leading, spacing: 6) {
            Toggle(isOn: Binding(
                get: { pythonIntelligenceCoordinator.isUserSpeaking },
                set: { newValue in Task { await pythonIntelligenceCoordinator.setUserSpeaking(newValue) } }
            )) {
                Text("I'm answering").font(.caption.bold())
            }
            .toggleStyle(.switch)
            Text("Hotkey: ⌘⇧A. Hold this on while you speak so your own voice isn't mistaken for an interviewer question — automatic speaker identity isn't reliable without separated meeting audio.")
                .font(.caption2)
                .foregroundStyle(.secondary)
        }
        .padding(10)
        .background(.quaternary.opacity(0.15), in: RoundedRectangle(cornerRadius: 8))
    }

    private func transcriptLaneLabel(for segment: TranscriptSegment) -> String? {
        switch segment.speakerRole {
        case SpeakerRole.interviewer.rawValue: return "INTERVIEWER"
        case SpeakerRole.user.rawValue: return "YOU"
        default: return nil
        }
    }

    private var sidePanel: some View {
        VStack(alignment: .leading, spacing: 16) {
            if pythonIntelligenceCoordinator.drivingSource == .realTranscription {
                MeetingAudioControlView(pythonIntelligenceCoordinator: pythonIntelligenceCoordinator)
                imAnsweringControl
            }

            VStack(alignment: .leading, spacing: 6) {
                Text("DETECTED QUESTIONS")
                    .font(.caption.bold())
                    .foregroundStyle(.secondary)
                if conversationState.detectedQuestions.isEmpty {
                    Text("None yet.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                } else {
                    ForEach(conversationState.detectedQuestions) { question in
                        Text(question.text)
                            .font(.caption)
                    }
                }
            }

            if !sessionDocuments.isEmpty {
                documentsSection
            }

            VStack(alignment: .leading, spacing: 8) {
                Text("OVERLAY")
                    .font(.caption.bold())
                    .foregroundStyle(.secondary)
                Button("Toggle overlay visibility") {
                    coordinator.overlayWindowController?.toggleVisibility()
                }
                Button("Toggle compact / expanded") {
                    coordinator.overlayWindowController?.toggleCompactMode()
                }
                Text("Hotkeys: ⌘⇧O show/hide · ⌘⇧C compact/expand")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
        }
        .frame(width: 260, alignment: .topLeading)
        .padding(12)
        .background(.quaternary.opacity(0.25), in: RoundedRectangle(cornerRadius: 10))
    }

    /// Section 9's exact status list (Not indexed/Indexing…/Ready/Failed
    /// to index/Unsupported document) — never claims a document is
    /// searchable ("Ready") until Python's `knowledge.ingestion_completed`
    /// actually says so.
    private var documentsSection: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("DOCUMENTS")
                .font(.caption.bold())
                .foregroundStyle(.secondary)
            ForEach(sessionDocuments) { document in
                HStack {
                    Text(document.fileName)
                        .font(.caption)
                        .lineLimit(1)
                    Spacer()
                    Text(knowledgeIngestionTracker.status(forDocumentID: document.id).displayText)
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
            }
        }
    }
}
