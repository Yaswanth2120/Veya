import Foundation

@MainActor
final class CreateSessionViewModel: ObservableObject {
    struct PendingDocument {
        let fileName: String
        let fileExtension: String
        let fileSizeBytes: Int64
        let sourceURL: URL
    }

    @Published var title = ""
    @Published var company = ""
    @Published var roleOrTopic = ""
    @Published var sessionDescription = ""
    @Published var expectedParticipants = ""
    @Published var sessionType: SessionType = .meeting
    @Published var notes = ""
    @Published var preferredAnswerStyle: AnswerStyle = .concise
    @Published var preferredProgrammingLanguage = ""
    @Published var customInstructions = ""

    @Published var attachedDocuments: [PendingDocument] = []
    @Published var isFileImporterPresented = false
    @Published var errorMessage: String?

    /// The `SessionDocument` rows actually persisted by the most recent
    /// `save()` call — the caller (`CreateSessionView`) uses these to
    /// request ingestion via `PythonIntelligenceCoordinator.ingestDocuments`
    /// once the session itself has been created successfully.
    private(set) var lastCreatedDocuments: [SessionDocument] = []

    private let sessionRepository = SessionRepository()
    private let documentRepository = SessionDocumentRepository()

    func handleFileImport(_ result: Result<[URL], Error>) {
        switch result {
        case .success(let urls):
            for url in urls {
                let didAccess = url.startAccessingSecurityScopedResource()
                defer { if didAccess { url.stopAccessingSecurityScopedResource() } }

                let attributes = try? FileManager.default.attributesOfItem(atPath: url.path)
                let size = (attributes?[.size] as? NSNumber)?.int64Value ?? 0
                attachedDocuments.append(
                    PendingDocument(
                        fileName: url.lastPathComponent,
                        fileExtension: url.pathExtension,
                        fileSizeBytes: size,
                        sourceURL: url
                    )
                )
            }
        case .failure(let error):
            errorMessage = error.localizedDescription
        }
    }

    /// Creates the session, copies attached documents into Application
    /// Support (metadata + file copy only — no parsing), and returns the
    /// created session so the caller can start the live session.
    func save() async -> Session? {
        let trimmedTitle = title.trimmingCharacters(in: .whitespaces)
        guard !trimmedTitle.isEmpty else {
            errorMessage = "Session title is required."
            return nil
        }

        let session = Session(
            id: UUID(),
            title: trimmedTitle,
            company: company,
            roleOrTopic: roleOrTopic,
            sessionDescription: sessionDescription,
            expectedParticipants: expectedParticipants,
            sessionType: sessionType,
            notes: notes,
            preferredAnswerStyle: preferredAnswerStyle,
            preferredProgrammingLanguage: preferredProgrammingLanguage,
            customInstructions: customInstructions,
            status: .notStarted,
            createdAt: Date(),
            endedAt: nil
        )

        do {
            try await sessionRepository.create(session)
            try await persistDocuments(for: session)
            return session
        } catch {
            errorMessage = "Failed to save session: \(error.localizedDescription)"
            return nil
        }
    }

    private func persistDocuments(for session: Session) async throws {
        lastCreatedDocuments = []
        guard !attachedDocuments.isEmpty else { return }

        let appSupport = try FileManager.default.url(
            for: .applicationSupportDirectory,
            in: .userDomainMask,
            appropriateFor: nil,
            create: true
        )
        let sessionDirectory = appSupport
            .appendingPathComponent("Veya", isDirectory: true)
            .appendingPathComponent("SessionDocuments", isDirectory: true)
            .appendingPathComponent(session.id.uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: sessionDirectory, withIntermediateDirectories: true)

        for pending in attachedDocuments {
            let destination = sessionDirectory.appendingPathComponent(pending.fileName)

            let didAccess = pending.sourceURL.startAccessingSecurityScopedResource()
            defer { if didAccess { pending.sourceURL.stopAccessingSecurityScopedResource() } }

            if FileManager.default.fileExists(atPath: destination.path) {
                try FileManager.default.removeItem(at: destination)
            }
            try FileManager.default.copyItem(at: pending.sourceURL, to: destination)

            let document = SessionDocument(
                id: UUID(),
                sessionID: session.id,
                fileName: pending.fileName,
                fileExtension: pending.fileExtension,
                storedPath: destination.path,
                fileSizeBytes: pending.fileSizeBytes,
                addedAt: Date()
            )
            try await documentRepository.create(document)
            lastCreatedDocuments.append(document)
        }
    }
}
