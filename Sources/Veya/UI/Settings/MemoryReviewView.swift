import SwiftUI

/// Section 13's durable-memory review UI: proposed candidates are shown for
/// explicit approve/reject, approved memory can be edited/deleted, and
/// nothing here is retrievable in a future session until approved. All
/// state is owned by the Python-side `MemoryStore` (local SQLite under the
/// managed application-support root) — this view only ever reflects it,
/// never a local cache treated as truth.
struct MemoryReviewView: View {
    @EnvironmentObject private var coordinator: AppCoordinator
    @StateObject private var viewModel = MemoryReviewViewModel()

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            BackToDashboardButton()
            Text("Memory").font(.largeTitle.bold())
            Text("Facts Veya has proposed remembering about you. Nothing here is used in future sessions until you approve it.")
                .font(.caption).foregroundStyle(.secondary)

            if !viewModel.proposed.isEmpty {
                Text("PROPOSED").font(.caption.bold())
                ForEach(viewModel.proposed) { memory in
                    HStack {
                        Text(memory.text).font(.callout)
                        Spacer()
                        Button("Approve") { Task { await viewModel.approve(memory.id) } }
                        Button("Reject", role: .destructive) { Task { await viewModel.reject(memory.id) } }
                    }
                    .padding(8)
                    .background(.yellow.opacity(0.1), in: RoundedRectangle(cornerRadius: 6))
                }
            }

            Text("APPROVED").font(.caption.bold())
            if viewModel.approved.isEmpty {
                Text("None yet.").font(.caption).foregroundStyle(.secondary)
            } else {
                ForEach(viewModel.approved) { memory in
                    HStack {
                        Text(memory.text).font(.callout)
                        Spacer()
                        Button("Delete", role: .destructive) { Task { await viewModel.delete(memory.id) } }
                    }
                    .padding(8)
                    .background(.quaternary.opacity(0.2), in: RoundedRectangle(cornerRadius: 6))
                }
            }
            Spacer()
        }
        .padding(28)
        .task { await viewModel.load(coordinator: coordinator) }
    }
}

@MainActor
final class MemoryReviewViewModel: ObservableObject {
    @Published private(set) var proposed: [MemoryRecordResult] = []
    @Published private(set) var approved: [MemoryRecordResult] = []
    private weak var coordinator: AppCoordinator?

    func load(coordinator: AppCoordinator) async {
        self.coordinator = coordinator
        await refresh()
    }

    private func refresh() async {
        guard let coordinator else { return }
        proposed = (try? await coordinator.pythonIntelligenceCoordinator.listMemories(status: "PROPOSED")) ?? []
        approved = (try? await coordinator.pythonIntelligenceCoordinator.listMemories(status: "APPROVED")) ?? []
    }

    func approve(_ id: String) async {
        guard let coordinator else { return }
        _ = try? await coordinator.pythonIntelligenceCoordinator.approveMemory(id: id)
        await refresh()
    }

    func reject(_ id: String) async {
        guard let coordinator else { return }
        try? await coordinator.pythonIntelligenceCoordinator.rejectMemory(id: id)
        await refresh()
    }

    func delete(_ id: String) async {
        guard let coordinator else { return }
        try? await coordinator.pythonIntelligenceCoordinator.deleteMemory(id: id)
        await refresh()
    }
}
