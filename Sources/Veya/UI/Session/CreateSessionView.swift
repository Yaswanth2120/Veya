import SwiftUI
import UniformTypeIdentifiers

struct CreateSessionView: View {
    @EnvironmentObject private var coordinator: AppCoordinator
    @StateObject private var viewModel = CreateSessionViewModel()

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            BackToDashboardButton()

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
                                    Text(ByteCountFormatter.string(fromByteCount: document.fileSizeBytes, countStyle: .file))
                                        .foregroundStyle(.secondary)
                                        .font(.caption)
                                }
                            }
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
                Button("Create & Start Session") {
                    Task {
                        if let session = await viewModel.save() {
                            coordinator.pythonIntelligenceCoordinator.ingestDocuments(
                                session: session, documents: viewModel.lastCreatedDocuments
                            )
                            coordinator.requestStartLiveSession(for: session)
                        }
                    }
                }
                .buttonStyle(.borderedProminent)
                .disabled(viewModel.title.trimmingCharacters(in: .whitespaces).isEmpty)
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
