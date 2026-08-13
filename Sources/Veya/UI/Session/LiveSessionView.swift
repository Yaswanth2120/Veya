import SwiftUI

struct LiveSessionView: View {
    @EnvironmentObject private var coordinator: AppCoordinator
    @ObservedObject var conversationState: ConversationState
    @ObservedObject private var pythonIntelligenceCoordinator: PythonIntelligenceCoordinator
    @ObservedObject private var knowledgeIngestionTracker: KnowledgeIngestionTracker
    @State private var sessionDocuments: [SessionDocument] = []
    private let documentRepository = SessionDocumentRepository()

    init(conversationState: ConversationState, pythonIntelligenceCoordinator: PythonIntelligenceCoordinator) {
        self.conversationState = conversationState
        self.pythonIntelligenceCoordinator = pythonIntelligenceCoordinator
        self.knowledgeIngestionTracker = pythonIntelligenceCoordinator.knowledgeIngestionTracker
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            header

            HStack(alignment: .top, spacing: 20) {
                transcriptPanel
                sidePanel
            }
            if let session = coordinator.currentSession,
               session.sessionType == .codingPractice || session.sessionType == .systemDesign {
                CopilotWorkbenchView(session: session)
            }
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
                Text(intelligenceSourceText)
                    .font(.caption2)
                    .foregroundStyle(intelligenceSourceIsFallback ? .orange : .secondary)
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
        case .live: return "Mocked transcript is streaming…"
        case .ended: return "Session ended"
        }
    }

    /// Never claims Python-backed mock intelligence or real transcription
    /// is active when it isn't — see build prompt "Fallback Behavior."
    /// The text itself is decided by the coordinator (not here) so
    /// AVFoundation/worker-state details stay out of this view.
    private var intelligenceSourceText: String {
        pythonIntelligenceCoordinator.liveSessionIndicatorText
    }

    private var intelligenceSourceIsFallback: Bool {
        switch pythonIntelligenceCoordinator.drivingSource {
        case .swiftFallback, .pythonWorker:
            return true
        case .realTranscription:
            // Real transcripts are still flowing — only flag it visually
            // when answer intelligence specifically isn't available.
            return !pythonIntelligenceCoordinator.answerIntelligenceAvailable
        case .none:
            return false
        }
    }

    private var transcriptPanel: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("TRANSCRIPT (mocked)")
                .font(.caption.bold())
                .foregroundStyle(.secondary)

            ScrollView {
                VStack(alignment: .leading, spacing: 6) {
                    ForEach(conversationState.segments) { segment in
                        Text(segment.text)
                            .font(.callout)
                            .padding(.vertical, 2)
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
