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
            Text("Create Session")
                .font(.largeTitle.bold())

            LocalAIStatusCard(onOpenSettings: { coordinator.route = .localAIStatus })

            ScrollView {
                Form {
                    Section("Overview") {
                        TextField("Session title", text: $viewModel.title)
                        Picker("Session type", selection: $viewModel.sessionType) {
                            ForEach(SessionType.allCases) { type in
                                Text(type.displayName).tag(type)
                            }
                        }
                    }

                    if showsMeetingFields {
                        Section("Context") {
                            TextField("Company", text: $viewModel.company)
                            TextField("Role / Topic", text: $viewModel.roleOrTopic)
                            TextField("Description", text: $viewModel.sessionDescription, axis: .vertical)
                                .lineLimit(2...4)
                            TextField("Expected participants", text: $viewModel.expectedParticipants)
                        }
                    }

                    if viewModel.sessionType == .codingPractice {
                        Section("Coding Practice") {
                            ProgrammingLanguagePicker(selection: $viewModel.preferredProgrammingLanguage)
                            Toggle("Allow local code execution for this session", isOn: $viewModel.codeExecutionConsent)
                            Text("Generated code can be run locally in a bounded, non-sandboxed subprocess — never a shell, never network access. This only takes effect if the app build also has local execution enabled.")
                                .font(.caption2)
                                .foregroundStyle(.secondary)
                        }
                    }

                    if viewModel.sessionType == .systemDesign {
                        Section("System Design") {
                            TextField("Requirements / context", text: $viewModel.sessionDescription, axis: .vertical)
                                .lineLimit(2...4)
                            TextField("Expected scale / traffic (e.g. \"100M redirects/day\")", text: $viewModel.expectedScale, axis: .vertical)
                                .lineLimit(1...3)
                        }
                    }

                    if showsMeetingFields {
                        Section("Copilot preferences") {
                            Picker("Preferred answer style", selection: $viewModel.preferredAnswerStyle) {
                                ForEach(AnswerStyle.allCases) { style in
                                    Text(style.displayName).tag(style)
                                }
                            }
                            TextField("Notes", text: $viewModel.notes, axis: .vertical)
                                .lineLimit(2...4)
                            HStack {
                                Text("Personal context comes from your")
                                Button("Personal Profile") { coordinator.route = .personalProfile }
                                    .buttonStyle(.link)
                            }
                            .font(.caption)
                            TextField("Custom instructions", text: $viewModel.customInstructions, axis: .vertical)
                                .lineLimit(2...4)
                        }
                    }

                    Section("Documents") {
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
                                    if isInterviewSession {
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
                                    }
                                    Text(ByteCountFormatter.string(fromByteCount: document.fileSizeBytes, countStyle: .file))
                                        .foregroundStyle(.secondary)
                                        .font(.caption)
                                }
                            }
                        }
                        if isInterviewSession {
                            Text("A resume is required before starting an Interview Copilot session, unless you explicitly start without one.")
                                .font(.caption2)
                                .foregroundStyle(.secondary)
                            Toggle("Start without resume", isOn: $viewModel.startWithoutResume)
                                .font(.caption)
                        }
                    }
                }
                .formStyle(.grouped)
            }

            HStack {
                if let error = viewModel.errorMessage {
                    Text(error)
                        .foregroundStyle(.red)
                        .font(.caption)
                }
                Spacer()
                Button(isInterviewSession ? "Create Session" : "Create & Start Session") {
                    Task { await createAndProceed() }
                }
                .buttonStyle(.borderedProminent)
                .disabled(!canCreate)
            }
        }
    }

    private var canCreate: Bool {
        guard !viewModel.title.trimmingCharacters(in: .whitespaces).isEmpty else { return false }
        if isInterviewSession, !viewModel.hasResumeDocument, !viewModel.startWithoutResume { return false }
        return true
    }

    private func createAndProceed() async {
        guard let session = await viewModel.save() else { return }
        pythonIntelligenceCoordinator.ingestDocuments(session: session, documents: viewModel.lastCreatedDocuments)

        if isInterviewSession, !viewModel.lastCreatedDocuments.isEmpty {
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

    private var isInterviewSession: Bool {
        viewModel.sessionType == .interviewPractice
    }

    /// Company/role/participants/answer-style/notes are meaningful for
    /// conversational session types — showing them for Coding Practice or
    /// System Design would just be irrelevant generic fields nobody fills.
    private var showsMeetingFields: Bool {
        switch viewModel.sessionType {
        case .presentation, .meeting, .clientCall, .technicalMeeting, .interviewPractice:
            return true
        case .codingPractice, .systemDesign:
            return false
        }
    }
}
