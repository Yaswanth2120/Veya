import SwiftUI

struct DashboardView: View {
    @EnvironmentObject private var coordinator: AppCoordinator
    @StateObject private var viewModel = DashboardViewModel()

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 24) {
                header
                actionGrid
                recentSessionsSection
            }
            .padding(28)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .task { await viewModel.loadRecentSessions() }
        .onChange(of: coordinator.route) { _, route in
            if route == .dashboard {
                Task { await viewModel.loadRecentSessions() }
            }
        }
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("Veya")
                .font(.largeTitle.bold())
            Text("Your real-time conversation copilot")
                .font(.subheadline)
                .foregroundStyle(.secondary)
        }
    }

    private var actionGrid: some View {
        LazyVGrid(columns: [GridItem(.adaptive(minimum: 180), spacing: 16)], spacing: 16) {
            DashboardTile(title: "New Session", systemImage: "plus.circle.fill") {
                coordinator.route = .createSession
            }
            DashboardTile(title: "Previous Sessions", systemImage: "clock.arrow.circlepath") {
                coordinator.route = .previousSessions
            }
            DashboardTile(title: "Knowledge Base", systemImage: "books.vertical.fill") {
                coordinator.route = .knowledgeBase
            }
            DashboardTile(title: "Personal Profile", systemImage: "person.crop.circle.fill") {
                coordinator.route = .personalProfile
            }
            DashboardTile(title: "Settings", systemImage: "gearshape.fill") {
                coordinator.route = .settings
            }
        }
    }

    @ViewBuilder
    private var recentSessionsSection: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Text("Recent Sessions")
                    .font(.title3.bold())
                Spacer()
                Button("See all") { coordinator.route = .previousSessions }
                    .buttonStyle(.link)
            }

            if viewModel.recentSessions.isEmpty {
                Text("No sessions yet. Create one to get started.")
                    .foregroundStyle(.secondary)
                    .padding(.vertical, 8)
            } else {
                ForEach(viewModel.recentSessions) { session in
                    SessionRow(session: session)
                }
            }
        }
    }
}

private struct DashboardTile: View {
    let title: String
    let systemImage: String
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            VStack(alignment: .leading, spacing: 10) {
                Image(systemName: systemImage)
                    .font(.title2)
                    .foregroundStyle(.tint)
                Text(title)
                    .font(.headline)
                    .foregroundStyle(.primary)
                Spacer(minLength: 0)
            }
            .frame(height: 96, alignment: .topLeading)
            .padding(16)
            .frame(maxWidth: .infinity, alignment: .topLeading)
            .background(.quaternary.opacity(0.4), in: RoundedRectangle(cornerRadius: 12))
        }
        .buttonStyle(.plain)
    }
}

@MainActor
final class DashboardViewModel: ObservableObject {
    @Published private(set) var recentSessions: [Session] = []

    private let repository = SessionRepository()

    func loadRecentSessions() async {
        let sessions = (try? await repository.fetchAll()) ?? []
        recentSessions = Array(sessions.prefix(5))
    }
}
