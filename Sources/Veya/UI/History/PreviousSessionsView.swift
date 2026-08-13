import Foundation
import SwiftUI

struct PreviousSessionsView: View {
    @EnvironmentObject private var coordinator: AppCoordinator
    @StateObject private var viewModel = PreviousSessionsViewModel()

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack {
                Button {
                    coordinator.showDashboard()
                } label: {
                    Label("Dashboard", systemImage: "chevron.left")
                }
                .buttonStyle(.plain)
                Spacer()
            }

            Text("Previous Sessions")
                .font(.largeTitle.bold())

            if viewModel.sessions.isEmpty {
                Text("No sessions yet.")
                    .foregroundStyle(.secondary)
            } else {
                ScrollView {
                    VStack(spacing: 8) {
                        ForEach(viewModel.sessions) { session in
                            SessionDetailDisclosure(session: session) {
                                await viewModel.delete(session, coordinator: coordinator)
                            }
                        }
                    }
                }
            }
            Spacer()
        }
        .padding(28)
        .task { await viewModel.load() }
    }
}

private struct SessionDetailDisclosure: View {
    let session: Session
    let onDelete: () async -> Void

    @State private var expanded = false
    @State private var showingDeleteConfirmation = false
    @StateObject private var detail = SessionDetailViewModel()

    var body: some View {
        DisclosureGroup(isExpanded: $expanded) {
            VStack(alignment: .leading, spacing: 8) {
                if let report = detail.report {
                    SessionReportSummaryView(report: report)
                    Divider()
                }
                if detail.displayableTranscript.isEmpty {
                    Text("No transcript recorded.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                } else {
                    ForEach(detail.displayableTranscript) { segment in
                        Text(segment.text)
                            .font(.caption)
                    }
                }
            }
            .padding(.top, 6)
            .padding(.leading, 8)
        } label: {
            HStack {
                SessionRow(session: session)
                if session.status != .live {
                    Button(role: .destructive) {
                        showingDeleteConfirmation = true
                    } label: {
                        Image(systemName: "trash")
                    }
                    .buttonStyle(.plain)
                    .help("Delete session")
                }
            }
        }
        .onChange(of: expanded) { _, isExpanded in
            if isExpanded {
                Task { await detail.load(sessionID: session.id) }
            }
        }
        .confirmationDialog(
            "Delete \"\(session.title.isEmpty ? "Untitled Session" : session.title)\"?",
            isPresented: $showingDeleteConfirmation,
            titleVisibility: .visible
        ) {
            Button("Delete Permanently", role: .destructive) { Task { await onDelete() } }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("This permanently deletes the transcript, report, questions/answers, documents, and any coding/design workspace for this session. This cannot be undone.")
        }
    }
}

private struct SessionReportSummaryView: View {
    let report: SessionReport

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("SESSION REPORT").font(.caption.bold())
            if !report.summary.isEmpty { Text(report.summary).font(.caption) }
            labeledList("Topics", report.topics)
            if !report.generatedAnswers.isEmpty {
                Text("Answers").font(.caption2.bold()).foregroundStyle(.secondary)
                ForEach(Array(report.generatedAnswers.enumerated()), id: \.offset) { _, answer in
                    Text("Q: \(answer.question)").font(.caption2)
                    ForEach(answer.talkingPoints, id: \.self) { Text("  • \($0)").font(.caption2) }
                }
            }
            if !report.sources.isEmpty {
                Text("Sources").font(.caption2.bold()).foregroundStyle(.secondary)
                ForEach(report.sources, id: \.chunkId) { Text("• \($0.fileName): \($0.excerpt)").font(.caption2) }
            }
            labeledList("Decisions", report.decisions)
            labeledList("Action Items", report.actionItems)
            labeledList("Unanswered Questions", report.unansweredQuestions)
            labeledList("Preparation Gaps", report.preparationGaps)
        }
    }

    @ViewBuilder
    private func labeledList(_ title: String, _ items: [String]) -> some View {
        if !items.isEmpty {
            Text(title).font(.caption2.bold()).foregroundStyle(.secondary)
            ForEach(items, id: \.self) { Text("• \($0)").font(.caption2) }
        }
    }
}

@MainActor
private final class SessionDetailViewModel: ObservableObject {
    @Published private(set) var displayableTranscript: [TranscriptSegment] = []
    @Published private(set) var report: SessionReport?
    private let repository = ConversationRepository()

    func load(sessionID: UUID) async {
        let transcript = (try? await repository.transcript(sessionID: sessionID)) ?? []
        // A review found raw whisper.cpp non-speech tags (e.g.
        // "[BLANK_AUDIO]") visible in this history view — sessions
        // transcribed before the source-level fix (which stops these
        // from ever being persisted going forward) still have them
        // stored, so this filters at display time too rather than only
        // fixing new data.
        displayableTranscript = transcript.filter { !Self.isNonSpeechMarker($0.text) }
        report = try? await repository.report(sessionID: sessionID)
    }

    private static func isNonSpeechMarker(_ text: String) -> Bool {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let first = trimmed.first, let last = trimmed.last else { return true }
        let isBracketed = (first == "[" && last == "]") || (first == "(" && last == ")")
        guard isBracketed else { return false }
        let inner = trimmed.dropFirst().dropLast()
        return !inner.contains("[") && !inner.contains("]") && !inner.contains("(") && !inner.contains(")")
    }
}

@MainActor
final class PreviousSessionsViewModel: ObservableObject {
    @Published private(set) var sessions: [Session] = []
    private let repository = SessionRepository()

    func load() async {
        sessions = (try? await repository.fetchAll()) ?? []
    }

    func delete(_ session: Session, coordinator: AppCoordinator) async {
        try? await coordinator.deleteSession(session)
        await load()
    }
}
