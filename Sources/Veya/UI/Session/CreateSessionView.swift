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

            ScrollView {
                Form {
                    Section("Overview") {
                        TextField("Session title", text: $viewModel.title)
                        TextField("Company", text: $viewModel.company)
                        TextField("Role / Topic", text: $viewModel.roleOrTopic)
                        TextField("Description", text: $viewModel.sessionDescription, axis: .vertical)
                            .lineLimit(2...4)
                        TextField("Expected participants", text: $viewModel.expectedParticipants)
                        Picker("Session type", selection: $viewModel.sessionType) {
                            ForEach(SessionType.allCases) { type in
                                Text(type.displayName).tag(type)
                            }
                        }
                    }

                    Section("Copilot preferences") {
                        TextField("Notes", text: $viewModel.notes, axis: .vertical)
                            .lineLimit(2...4)
                        Picker("Preferred answer style", selection: $viewModel.preferredAnswerStyle) {
                            ForEach(AnswerStyle.allCases) { style in
                                Text(style.displayName).tag(style)
                            }
                        }
                        TextField("Preferred programming language", text: $viewModel.preferredProgrammingLanguage)
                        TextField("Custom instructions", text: $viewModel.customInstructions, axis: .vertical)
                            .lineLimit(2...4)
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
}
