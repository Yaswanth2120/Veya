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
        case unavailable, generating, understanding, waitingForSilence, transcribing, listening

        var label: String {
            switch self {
            case .listening: return "Listening"
            case .transcribing: return "Transcribing"
            case .waitingForSilence: return "Waiting for speaker to finish"
            case .understanding: return "Understanding question"
            case .generating: return "Generating answer"
            case .unavailable: return "Local AI unavailable"
            }
        }
    }

    private var activeStep: PipelineStep {
        if isAnswerIntelligenceUnavailable { return .unavailable }
        if conversationState.isGeneratingAnswer { return .generating }
        if conversationState.isClassifyingQuestion || conversationState.isAnalyzingQuestion { return .understanding }
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

            // A completed answer is always shown once one exists, even if
            // the pipeline has since moved on to listening for the next
            // turn — it's only replaced once a *newer* answer completes.
            if let answer = conversationState.currentAnswer, !conversationState.isGeneratingAnswer, !conversationState.isClassifyingQuestion, !conversationState.isAnalyzingQuestion {
                VStack(alignment: .leading, spacing: 6) {
                    Text(answer.question).font(.headline)
                    ForEach(answer.talkingPoints, id: \.self) { point in
                        Text("• \(point)").font(.body)
                    }
                    if !answer.sources.isEmpty {
                        Text("Sources").font(.caption.bold()).foregroundStyle(.secondary)
                        ForEach(answer.sources, id: \.self) { Text("• \($0)").font(.caption) }
                    }
                }
            } else {
                switch activeStep {
                case .generating:
                    VStack(alignment: .leading, spacing: 6) {
                        HStack { ProgressView().controlSize(.small); Text("Generating answer…").font(.callout) }
                        if let partial = conversationState.partialAnswerText, !partial.isEmpty {
                            Text(partial).font(.callout).foregroundStyle(.secondary)
                        }
                    }
                case .understanding:
                    HStack { ProgressView().controlSize(.small); Text("Understanding question…").font(.callout) }
                case .waitingForSilence:
                    Text("Waiting for the speaker to finish…").font(.callout).foregroundStyle(.secondary)
                case .unavailable:
                    Text("Local AI isn't configured — questions will be detected in the transcript, but no answer will be generated.")
                        .font(.callout).foregroundStyle(.secondary)
                case .transcribing:
                    Text("Listening for a question…").font(.callout).foregroundStyle(.secondary)
                case .listening:
                    Text("No question detected yet.").font(.callout).foregroundStyle(.secondary)
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .topLeading)
        .padding(16)
        .background(Color.accentColor.opacity(0.08), in: RoundedRectangle(cornerRadius: 12))
        .overlay(RoundedRectangle(cornerRadius: 12).stroke(Color.accentColor.opacity(0.25), lineWidth: 1))
    }

    private var transcriptPanel: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("TRANSCRIPT")
                .font(.caption.bold())
                .foregroundStyle(.secondary)

            ScrollView {
                VStack(alignment: .leading, spacing: 6) {
                    ForEach(TranscriptDisplayFiltering.displayable(conversationState.segments)) { segment in
                        Text(segment.text)
                            .font(.callout)
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

    private var sidePanel: some View {
        VStack(alignment: .leading, spacing: 16) {
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
