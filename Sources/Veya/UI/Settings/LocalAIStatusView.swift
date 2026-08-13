import SwiftUI

/// Diagnostics + repair for local answer/coding/design/report
/// intelligence — a review found the app silently degraded to "No local
/// LLM was available" with no way to see why or fix it from the UI, even
/// though Ollama was running with a different model installed than the
/// worker's hard-coded default. This is the actionable status/setup panel
/// that was missing.
struct LocalAIStatusView: View {
    @EnvironmentObject private var coordinator: AppCoordinator
    @StateObject private var viewModel = LocalAIStatusViewModel()

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            BackToDashboardButton()
            Text("Local AI").font(.largeTitle.bold())
            Text("Ollama powers question answering, coding/design assistance, and session reports — all local, never a cloud API.")
                .font(.caption).foregroundStyle(.secondary)

            if viewModel.isLoading {
                ProgressView()
            } else if let status = viewModel.status {
                statusSummary(status)
                if !status.reachable {
                    repairInstructions(text: "Ollama isn't reachable at \(status.baseUrl.isEmpty ? "the configured address" : status.baseUrl). Start it with `ollama serve`, or check that it's already running.")
                } else if !status.modelInstalled {
                    repairInstructions(text: "The configured model \"\(status.configuredModel)\" isn't installed. Pull it with `ollama pull \(status.configuredModel)`, or pick an installed model below.")
                } else {
                    Label("Ready — \(status.configuredModel) is installed and reachable.", systemImage: "checkmark.circle.fill")
                        .foregroundStyle(.green)
                }

                if !status.availableModels.isEmpty {
                    Divider()
                    Text("INSTALLED MODELS").font(.caption.bold())
                    Picker("Model", selection: $viewModel.selectedModel) {
                        Text("Worker default").tag("")
                        ForEach(status.availableModels, id: \.self) { Text($0).tag($0) }
                    }
                    .labelsHidden()
                    HStack {
                        Button("Use This Model") { Task { await viewModel.applySelectedModel(coordinator: coordinator) } }
                            .disabled(viewModel.isApplying)
                        if viewModel.isApplying { ProgressView().controlSize(.small) }
                    }
                }
            } else {
                Text("Status unavailable — the local worker may not be running yet.").font(.caption).foregroundStyle(.secondary)
            }

            Button("Refresh") { Task { await viewModel.refresh(coordinator: coordinator) } }
            Spacer()
        }
        .padding(28)
        .task { await viewModel.refresh(coordinator: coordinator) }
    }

    private func statusSummary(_ status: LLMStatusResult) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("Reachable: \(status.reachable ? "Yes" : "No")").font(.caption)
            Text("Configured model: \(status.configuredModel.isEmpty ? "(unknown)" : status.configuredModel)").font(.caption)
            Text("Configured model installed: \(status.modelInstalled ? "Yes" : "No")").font(.caption)
        }
    }

    private func repairInstructions(text: String) -> some View {
        Text(text)
            .font(.caption)
            .padding(8)
            .background(.yellow.opacity(0.15), in: RoundedRectangle(cornerRadius: 6))
            .textSelection(.enabled)
    }
}

@MainActor
final class LocalAIStatusViewModel: ObservableObject {
    @Published private(set) var status: LLMStatusResult?
    @Published private(set) var isLoading = false
    @Published private(set) var isApplying = false
    @Published var selectedModel = ""

    func refresh(coordinator: AppCoordinator) async {
        isLoading = true
        defer { isLoading = false }
        status = await coordinator.pythonIntelligenceCoordinator.fetchLLMStatus()
        if let status, selectedModel.isEmpty {
            selectedModel = status.modelInstalled ? status.configuredModel : ""
        }
    }

    func applySelectedModel(coordinator: AppCoordinator) async {
        isApplying = true
        defer { isApplying = false }
        await coordinator.pythonIntelligenceCoordinator.setOllamaModelOverride(selectedModel)
        await refresh(coordinator: coordinator)
    }
}
