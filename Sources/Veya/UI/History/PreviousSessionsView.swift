import Foundation
import SwiftUI

struct PreviousSessionsView: View {
    @EnvironmentObject private var coordinator: AppCoordinator
    @StateObject private var viewModel = PreviousSessionsViewModel()
    @State private var showingDeleteAllFirstConfirmation = false
    @State private var showingDeleteAllFinalConfirmation = false

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
                if !viewModel.sessions.isEmpty {
                    Button("Delete All…", role: .destructive) { showingDeleteAllFirstConfirmation = true }
                        .font(.caption)
                }
            }

            Text("Previous Sessions")
                .font(.largeTitle.bold())

            if viewModel.isLoading {
                ProgressView()
            } else if viewModel.sessions.isEmpty {
                emptyState
            } else {
                ScrollView {
                    VStack(spacing: 8) {
                        ForEach(viewModel.sessions) { session in
                            SessionDetailDisclosure(
                                session: session,
                                onDelete: { await viewModel.delete(session, coordinator: coordinator) },
                                onDuplicate: { await viewModel.duplicate(session, coordinator: coordinator) },
                                onExport: { await viewModel.export(session, coordinator: coordinator) }
                            )
                        }
                    }
                }
            }
            if let exportedPath = viewModel.lastExportedPath {
                Text("Exported to \(exportedPath)").font(.caption2).foregroundStyle(.secondary).textSelection(.enabled)
            }
            Spacer()
        }
        .padding(28)
        .task { await viewModel.load() }
        // Two-step confirmation, deliberately stronger than the per-session
        // delete's single dialog — this is destructive across every session.
        .confirmationDialog(
            "Delete all \(viewModel.sessions.count) sessions?",
            isPresented: $showingDeleteAllFirstConfirmation,
            titleVisibility: .visible
        ) {
            Button("Continue", role: .destructive) { showingDeleteAllFinalConfirmation = true }
            Button("Cancel", role: .cancel) {}
        }
        .confirmationDialog(
            "This permanently deletes every session, transcript, report, document, and workspace. This cannot be undone.",
            isPresented: $showingDeleteAllFinalConfirmation,
            titleVisibility: .visible
        ) {
            Button("Delete Everything", role: .destructive) { Task { await viewModel.deleteAll(coordinator: coordinator) } }
            Button("Cancel", role: .cancel) {}
        }
    }

    private var emptyState: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("No sessions yet.").foregroundStyle(.secondary)
            Button("Create your first session") { coordinator.route = .createSession }
        }
        .padding(.vertical, 24)
    }
}

private struct SessionDetailDisclosure: View {
    let session: Session
    let onDelete: () async -> Void
    let onDuplicate: () async -> Void
    let onExport: () async -> Void

    @State private var expanded = false
    @State private var showingDeleteConfirmation = false
    @StateObject private var detail = SessionDetailViewModel()

    var body: some View {
        DisclosureGroup(isExpanded: $expanded) {
            VStack(alignment: .leading, spacing: 8) {
                if detail.isLoading {
                    ProgressView()
                } else {
                    if let report = detail.report {
                        SessionReportCardView(report: report)
                    } else {
                        Text("No report yet — reports are generated when a session ends.")
                            .font(.caption).foregroundStyle(.secondary)
                    }

                    Divider()
                    DisclosureGroup("Raw transcript (\(detail.displayableTranscript.count) segments)") {
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
                    .font(.caption.bold())
                }
            }
            .padding(.top, 6)
            .padding(.leading, 8)
        } label: {
            HStack {
                SessionRow(session: session)
                Menu {
                    Button("Duplicate / Reuse", systemImage: "plus.square.on.square") { Task { await onDuplicate() } }
                    Button("Export…", systemImage: "square.and.arrow.up") { Task { await onExport() } }
                    if session.status != .live {
                        Button("Delete…", systemImage: "trash", role: .destructive) { showingDeleteConfirmation = true }
                    }
                } label: {
                    Image(systemName: "ellipsis.circle")
                }
                .buttonStyle(.plain)
                .menuIndicator(.hidden)
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

/// Report content as readable expandable sections/cards, rather than a
/// raw dump — the raw transcript lives in its own separate, collapsed-
/// by-default disclosure group above.
private struct SessionReportCardView: View {
    let report: SessionReport

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            if !report.summary.isEmpty {
                VStack(alignment: .leading, spacing: 4) {
                    Text("SUMMARY").font(.caption2.bold()).foregroundStyle(.secondary)
                    Text(report.summary).font(.callout)
                }
            }
            if !report.topics.isEmpty {
                labeledChips("Topics", report.topics)
            }

            if !report.generatedAnswers.isEmpty {
                cardSection("Questions & Answers (\(report.generatedAnswers.count))") {
                    ForEach(Array(report.generatedAnswers.enumerated()), id: \.offset) { _, answer in
                        VStack(alignment: .leading, spacing: 2) {
                            Text(answer.question).font(.caption.bold())
                            ForEach(answer.talkingPoints, id: \.self) { Text("• \($0)").font(.caption) }
                        }
                        .padding(.bottom, 4)
                    }
                }
            }

            if !report.sources.isEmpty {
                cardSection("Sources (\(report.sources.count))") {
                    ForEach(report.sources, id: \.chunkId) { Text("• \($0.fileName): \($0.excerpt)").font(.caption) }
                }
            }

            if !report.decisions.isEmpty {
                cardSection("Decisions") { listItems(report.decisions) }
            }
            if !report.actionItems.isEmpty {
                cardSection("Action Items") { listItems(report.actionItems) }
            }
            if !report.unansweredQuestions.isEmpty {
                cardSection("Unanswered Questions") { listItems(report.unansweredQuestions) }
            }
            if !report.preparationGaps.isEmpty {
                cardSection("Preparation Gaps") { listItems(report.preparationGaps) }
            }
        }
    }

    private func cardSection<Content: View>(_ title: String, @ViewBuilder content: @escaping () -> Content) -> some View {
        DisclosureGroup(title) {
            VStack(alignment: .leading, spacing: 4) { content() }
                .padding(.top, 4)
        }
        .font(.caption.bold())
    }

    private func listItems(_ items: [String]) -> some View {
        ForEach(items, id: \.self) { Text("• \($0)").font(.caption) }
    }

    private func labeledChips(_ title: String, _ items: [String]) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title.uppercased()).font(.caption2.bold()).foregroundStyle(.secondary)
            HStack {
                ForEach(items, id: \.self) { item in
                    Text(item)
                        .font(.caption2)
                        .padding(.horizontal, 8).padding(.vertical, 3)
                        .background(.quaternary.opacity(0.4), in: Capsule())
                }
            }
        }
    }
}

@MainActor
private final class SessionDetailViewModel: ObservableObject {
    @Published private(set) var displayableTranscript: [TranscriptSegment] = []
    @Published private(set) var report: SessionReport?
    @Published private(set) var isLoading = false
    private let repository = ConversationRepository()

    func load(sessionID: UUID) async {
        isLoading = true
        defer { isLoading = false }
        let transcript = (try? await repository.transcript(sessionID: sessionID)) ?? []
        displayableTranscript = TranscriptDisplayFiltering.displayable(transcript)
        report = try? await repository.report(sessionID: sessionID)
    }
}

@MainActor
final class PreviousSessionsViewModel: ObservableObject {
    @Published private(set) var sessions: [Session] = []
    @Published private(set) var isLoading = false
    @Published private(set) var lastExportedPath: String?
    private let repository = SessionRepository()
    private let conversationRepository = ConversationRepository()

    func load() async {
        isLoading = true
        defer { isLoading = false }
        sessions = (try? await repository.fetchAll()) ?? []
    }

    func delete(_ session: Session, coordinator: AppCoordinator) async {
        try? await coordinator.deleteSession(session)
        await load()
    }

    func deleteAll(coordinator: AppCoordinator) async {
        await coordinator.deleteAllSessions()
        await load()
    }

    func duplicate(_ session: Session, coordinator: AppCoordinator) async {
        _ = try? await coordinator.duplicateSession(session)
        await load()
    }

    /// Writes a readable Markdown export (session metadata, report, and
    /// transcript) to the user's Documents folder. No third-party
    /// dependency — the same plain-text-composition approach the design
    /// workbench already uses for its exports.
    func export(_ session: Session, coordinator: AppCoordinator) async {
        let transcript = (try? await conversationRepository.transcript(sessionID: session.id)) ?? []
        let report = try? await conversationRepository.report(sessionID: session.id)

        var lines = ["# \(session.title.isEmpty ? "Untitled Session" : session.title)", "", "_\(session.sessionType.displayName) · \(session.createdAt.formatted())_", ""]
        if let report {
            if !report.summary.isEmpty { lines += ["## Summary", report.summary, ""] }
            if !report.decisions.isEmpty { lines += ["## Decisions"] + report.decisions.map { "- \($0)" } + [""] }
            if !report.actionItems.isEmpty { lines += ["## Action Items"] + report.actionItems.map { "- \($0)" } + [""] }
            if !report.unansweredQuestions.isEmpty { lines += ["## Unanswered Questions"] + report.unansweredQuestions.map { "- \($0)" } + [""] }
            if !report.preparationGaps.isEmpty { lines += ["## Preparation Gaps"] + report.preparationGaps.map { "- \($0)" } + [""] }
        }
        lines.append("## Transcript")
        let displayable = TranscriptDisplayFiltering.displayable(transcript)
        lines += displayable.isEmpty ? ["_No transcript recorded._"] : displayable.map { $0.text }

        let content = lines.joined(separator: "\n")
        guard let documentsDirectory = try? FileManager.default.url(for: .documentDirectory, in: .userDomainMask, appropriateFor: nil, create: true) else { return }
        let safeTitle = session.title.isEmpty ? "session" : session.title.replacingOccurrences(of: "/", with: "-")
        let url = documentsDirectory.appendingPathComponent("\(safeTitle)-\(session.id.uuidString.prefix(8)).md")
        try? content.write(to: url, atomically: true, encoding: .utf8)
        lastExportedPath = url.path
    }
}
