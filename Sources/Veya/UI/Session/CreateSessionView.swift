import SwiftUI
import UniformTypeIdentifiers

struct CreateSessionView: View {
    @EnvironmentObject private var coordinator: AppCoordinator
    @StateObject private var viewModel = CreateSessionViewModel()
    @ObservedObject private var pythonIntelligenceCoordinator: PythonIntelligenceCoordinator
    @ObservedObject private var knowledgeIngestionTracker: KnowledgeIngestionTracker

    /// Non-nil once `save()` has succeeded for an Interview Copilot
    /// session with attached documents — the view switches from the
    /// create form to the "waiting for documents to index" gate.
    @State private var pendingInterviewSession: Session?

    init(pythonIntelligenceCoordinator: PythonIntelligenceCoordinator) {
        self.pythonIntelligenceCoordinator = pythonIntelligenceCoordinator
        self.knowledgeIngestionTracker = pythonIntelligenceCoordinator.knowledgeIngestionTracker
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            BackToDashboardButton()

            if let pendingInterviewSession {
                interviewReadinessGate(for: pendingInterviewSession)
            } else {
                createForm
            }
        }
        .padding(28)
        .fileImporter(
            isPresented: $viewModel.isFileImporterPresented,
            allowedContentTypes: [.pdf, .plainText, .sourceCode, UTType(filenameExtension: "docx") ?? .data, UTType(filenameExtension: "md") ?? .plainText],
            allowsMultipleSelection: true
        ) { result in
            viewModel.handleFileImport(result)
        }
    }

    private var createForm: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Create Interview")
                .font(.largeTitle.bold())

            LocalAIStatusCard(onOpenSettings: { coordinator.route = .localAIStatus })

            ScrollView {
                VStack(alignment: .leading, spacing: 20) {
                    VStack(alignment: .leading, spacing: 10) {
                        Text("Interview").font(.headline)
                        EditableSessionField(label: "Interview title", text: $viewModel.title, identifier: "session.title", focusesOnMount: true)
                            .frame(height: 30)
                        EditableSessionField(label: "Company (optional)", text: $viewModel.company, identifier: "session.company")
                            .frame(height: 30)
                        EditableSessionField(label: "Role / job title (optional)", text: $viewModel.roleOrTopic, identifier: "session.role")
                            .frame(height: 30)
                        EditableSessionField(label: "Job description / interview notes (optional)", text: $viewModel.sessionDescription, identifier: "session.notes", multiline: true)
                            .frame(height: 80)
                    }

                    VStack(alignment: .leading, spacing: 8) {
                        Text("Personal context").font(.headline)
                        HStack {
                            Text("Approved memory and background come from your")
                            Button("Personal Profile") { coordinator.route = .personalProfile }
                                .buttonStyle(.link)
                        }
                        .font(.caption)
                    }

                    VStack(alignment: .leading, spacing: 10) {
                        Text("Documents").font(.headline)
                        Button("Attach files…") {
                            viewModel.isFileImporterPresented = true
                        }
                        if viewModel.attachedDocuments.isEmpty {
                            Text("No documents attached.")
                                .foregroundStyle(.secondary)
                        } else {
                            ForEach(viewModel.attachedDocuments, id: \.fileName) { document in
                                HStack {
                                    Image(systemName: "doc.fill")
                                    Text(document.fileName)
                                    Spacer()
                                    Picker("Kind", selection: Binding(
                                        get: { document.kind },
                                        set: { viewModel.setDocumentKind($0, forFileNamed: document.fileName) }
                                    )) {
                                        ForEach(DocumentKind.allCases) { kind in
                                            Text(kind.displayName).tag(kind)
                                        }
                                    }
                                    .labelsHidden()
                                    .frame(width: 140)
                                    Text(ByteCountFormatter.string(fromByteCount: document.fileSizeBytes, countStyle: .file))
                                        .foregroundStyle(.secondary)
                                        .font(.caption)
                                    Button("Remove") { viewModel.attachedDocuments.removeAll { $0.fileName == document.fileName } }
                                        .font(.caption)
                                }
                            }
                        }
                        Text("A resume is required before starting, unless you explicitly start without one. A job description is optional but strongly recommended.")
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                        Toggle("Start without resume", isOn: $viewModel.startWithoutResume)
                            .font(.caption)
                    }
                }
                .padding(2)
            }

            HStack {
                if let message = viewModel.errorMessage ?? viewModel.validationMessage {
                    Text(message)
                        .foregroundStyle(viewModel.errorMessage == nil ? Color.secondary : Color.red)
                        .font(.caption)
                }
                Spacer()
                Button(viewModel.isSaving ? "Creating…" : "Create Session") {
                    Task { await createAndProceed() }
                }
                .buttonStyle(.borderedProminent)
                .disabled(!canCreate || coordinator.isEndingSession)
            }
        }
    }

    private var canCreate: Bool {
        viewModel.validationMessage == nil && !viewModel.isSaving
    }

    private func createAndProceed() async {
        guard let session = await viewModel.save() else { return }
        pythonIntelligenceCoordinator.ingestDocuments(session: session, documents: viewModel.lastCreatedDocuments)

        if !viewModel.lastCreatedDocuments.isEmpty {
            // Gate on real indexing readiness before the interview
            // actually starts — see `interviewReadinessGate`.
            pendingInterviewSession = session
        } else {
            coordinator.requestStartLiveSession(for: session)
        }
    }

    /// Section 16: "Resume ready for interview / Job description ready /
    /// Interview context ready" — never lets the interview start while
    /// required documents are still indexing.
    private func interviewReadinessGate(for session: Session) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Preparing Interview Context")
                .font(.title2.bold())

            VStack(alignment: .leading, spacing: 6) {
                ForEach(viewModel.lastCreatedDocuments) { document in
                    HStack {
                        Text(document.fileName)
                        Spacer()
                        Text(knowledgeIngestionTracker.status(forDocumentID: document.id).displayText)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
            }
            .padding(12)
            .background(.quaternary.opacity(0.25), in: RoundedRectangle(cornerRadius: 8))

            switch readinessState {
            case .ready(let hasReadyResume):
                Text(hasReadyResume ? "Resume ready for interview. Interview context ready." : "Interview context ready.")
                    .font(.callout)
                    .foregroundStyle(.green)
            case .blocked:
                Text("One or more documents failed to index or aren't supported. They will not become ready — start anyway, or remove and re-attach them.")
                    .font(.callout)
                    .foregroundStyle(.orange)
            case .indexing:
                HStack {
                    ProgressView().controlSize(.small)
                    Text("Indexing documents…").font(.callout)
                }
            }

            HStack {
                Button("Start Interview") {
                    coordinator.requestStartLiveSession(for: session)
                }
                .buttonStyle(.borderedProminent)
                .disabled(!isInterviewContextReady)

                if readinessState == .blocked {
                    Button("Start anyway") {
                        coordinator.requestStartLiveSession(for: session)
                    }
                    .font(.caption)
                }
            }
        }
    }

    /// Only documents that have actually finished indexing successfully
    /// count as "ready" — `.failed`/`.unsupported` must never silently
    /// unlock the normal Start button. The only way past those states is
    /// the explicit "Start anyway" escape hatch above.
    private var isInterviewContextReady: Bool {
        if case .ready = readinessState { return true }
        return false
    }

    private var readinessState: InterviewReadinessState {
        InterviewReadinessEvaluator.evaluate(
            documents: viewModel.lastCreatedDocuments,
            status: { knowledgeIngestionTracker.status(forDocumentID: $0) }
        )
    }

}
