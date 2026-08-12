import SwiftUI

/// Entry-point UI only. Document ingestion, parsing, chunking, and
/// retrieval are the separate `Knowledge` subsystem — not built here.
struct KnowledgeBaseView: View {
    @EnvironmentObject private var coordinator: AppCoordinator

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            BackToDashboardButton()

            Text("Knowledge Base")
                .font(.largeTitle.bold())

            Text("Document ingestion and retrieval aren't built yet — this is a placeholder entry point for that subsystem.")
                .foregroundStyle(.secondary)

            Spacer()
        }
        .padding(28)
    }
}

struct BackToDashboardButton: View {
    @EnvironmentObject private var coordinator: AppCoordinator

    var body: some View {
        Button {
            coordinator.showDashboard()
        } label: {
            Label("Dashboard", systemImage: "chevron.left")
        }
        .buttonStyle(.plain)
    }
}
