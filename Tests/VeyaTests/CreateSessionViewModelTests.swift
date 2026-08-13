import Foundation
import Testing
@testable import Veya

@MainActor
@Suite("CreateSessionViewModel document kind gating")
struct CreateSessionViewModelTests {
    private func makePendingDocument(fileName: String, kind: DocumentKind = .other) -> CreateSessionViewModel.PendingDocument {
        CreateSessionViewModel.PendingDocument(
            fileName: fileName, fileExtension: "pdf", fileSizeBytes: 1024,
            sourceURL: URL(fileURLWithPath: "/tmp/\(fileName)"), kind: kind
        )
    }

    @Test("hasResumeDocument is false with no attached documents")
    func noDocumentsMeansNoResume() {
        let viewModel = CreateSessionViewModel()
        #expect(viewModel.hasResumeDocument == false)
    }

    @Test("hasResumeDocument is false when only non-resume documents are attached")
    func onlyOtherDocumentsMeansNoResume() {
        let viewModel = CreateSessionViewModel()
        viewModel.attachedDocuments = [
            makePendingDocument(fileName: "notes.txt", kind: .other),
            makePendingDocument(fileName: "jd.pdf", kind: .jobDescription),
        ]
        #expect(viewModel.hasResumeDocument == false)
    }

    @Test("hasResumeDocument is true once a document is marked as the resume")
    func resumeDocumentIsDetected() {
        let viewModel = CreateSessionViewModel()
        viewModel.attachedDocuments = [
            makePendingDocument(fileName: "resume.pdf", kind: .resume),
            makePendingDocument(fileName: "jd.pdf", kind: .jobDescription),
        ]
        #expect(viewModel.hasResumeDocument == true)
    }

    @Test("setDocumentKind updates the matching document by file name")
    func setDocumentKindUpdatesTheRightDocument() {
        let viewModel = CreateSessionViewModel()
        viewModel.attachedDocuments = [
            makePendingDocument(fileName: "a.pdf"),
            makePendingDocument(fileName: "b.pdf"),
        ]

        viewModel.setDocumentKind(.resume, forFileNamed: "b.pdf")

        #expect(viewModel.attachedDocuments[0].kind == .other)
        #expect(viewModel.attachedDocuments[1].kind == .resume)
        #expect(viewModel.hasResumeDocument == true)
    }

    @Test("setDocumentKind for an unknown file name is a harmless no-op")
    func setDocumentKindForUnknownFileIsANoOp() {
        let viewModel = CreateSessionViewModel()
        viewModel.attachedDocuments = [makePendingDocument(fileName: "a.pdf")]

        viewModel.setDocumentKind(.resume, forFileNamed: "does-not-exist.pdf")

        #expect(viewModel.attachedDocuments[0].kind == .other)
    }

    @Test("startWithoutResume defaults to false")
    func startWithoutResumeDefaultsFalse() {
        let viewModel = CreateSessionViewModel()
        #expect(viewModel.startWithoutResume == false)
    }
}
