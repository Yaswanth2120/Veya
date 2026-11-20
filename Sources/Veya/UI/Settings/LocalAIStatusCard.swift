import SwiftUI

/// Compact, reusable "is local AI actually ready" summary — embedded in
/// both Settings (the full status/setup page) and Create Session (so a
/// user picking Coding Practice/System Design/an answer-dependent session
/// type sees readiness *before* starting, not after). Shows Ollama
/// reachability + configured/installed model, the Whisper model
/// download/ready state, and microphone permission — the full set of
/// "is answer-dependent intelligence actually going to work" signals.
struct LocalAIStatusCard: View {
    @EnvironmentObject private var coordinator: AppCoordinator
    @StateObject private var viewModel = LocalAIStatusViewModel()
    /// Shows the "pick an installed model" controls inline. Off for the
    /// compact Create Session placement (which links to Settings instead)
    /// so it doesn't compete for space with session fields.
    var showsModelPicker: Bool = false
    var onOpenSettings: (() -> Void)?

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text("AI STATUS").font(.caption.bold()).foregroundStyle(.secondary)
                Spacer()
                if viewModel.isLoading { ProgressView().controlSize(.small) }
                Button {
                    Task { await viewModel.refresh(coordinator: coordinator) }
                } label: {
                    Image(systemName: "arrow.clockwise")
                }
                .buttonStyle(.plain)
                .help("Recheck local AI status")
            }

            statusRow(
                label: isGPU ? "Vast GPU" : "Ollama",
                ready: (viewModel.status?.reachable ?? false) && (viewModel.status?.modelInstalled ?? false),
                detail: ollamaDetail
            )
            statusRow(
                label: "Whisper model",
                ready: isWhisperReady,
                detail: whisperDetail
            )
            statusRow(
                label: "Microphone",
                ready: coordinator.pythonIntelligenceCoordinator.microphoneAuthorizationState == .authorized,
                detail: microphoneDetail
            )

            if let warning = readinessWarning {
                Text(warning)
                    .font(.caption)
                    .padding(8)
                    .background(.yellow.opacity(0.15), in: RoundedRectangle(cornerRadius: 6))
                    .textSelection(.enabled)
            }

            if showsModelPicker {
                Divider()
                Picker("Answer backend", selection: $viewModel.selectedBackend) {
                    Text("Local Ollama").tag("ollama")
                    Text("Vast GPU via SSH").tag("vast")
                }
                Button("Use This Backend") {
                    Task { await viewModel.applySelectedBackend(coordinator: coordinator) }
                }
                .disabled(viewModel.isApplying || coordinator.pythonIntelligenceCoordinator.hasActiveSession)
                Text("GPU mode sends questions and selected context to your rented server through the existing SSH tunnel. Raw audio stays local. End the session before changing backends.")
                    .font(.caption).foregroundStyle(.secondary)
            }

            if showsModelPicker, let status = viewModel.status, !status.availableModels.isEmpty {
                Divider()
                Text("INSTALLED MODELS").font(.caption.bold()).foregroundStyle(.secondary)
                Picker("Model", selection: $viewModel.selectedModel) {
                    Text(isGPU ? "Automatic (served GPU model)" : "Worker default").tag("")
                    ForEach(status.availableModels, id: \.self) { Text($0).tag($0) }
                }
                .labelsHidden()
                HStack {
                    Button("Use This Model") { Task { await viewModel.applySelectedModel(coordinator: coordinator) } }
                        .disabled(viewModel.isApplying || coordinator.pythonIntelligenceCoordinator.hasActiveSession)
                    if viewModel.isApplying { ProgressView().controlSize(.small) }
                }
            } else if let onOpenSettings {
                Button("Configure in Settings…", action: onOpenSettings)
                    .font(.caption)
            }
        }
        .padding(12)
        .background(.quaternary.opacity(0.2), in: RoundedRectangle(cornerRadius: 10))
        .task { await viewModel.refresh(coordinator: coordinator) }
    }

    private func statusRow(label: String, ready: Bool, detail: String) -> some View {
        HStack(spacing: 8) {
            Image(systemName: ready ? "checkmark.circle.fill" : "exclamationmark.circle.fill")
                .foregroundStyle(ready ? .green : .orange)
            Text(label).font(.caption.bold())
            Text(detail).font(.caption).foregroundStyle(.secondary)
            Spacer()
        }
    }

    private var ollamaDetail: String {
        guard let status = viewModel.status else { return "checking…" }
        if !status.reachable { return "not reachable" }
        return status.modelInstalled ? "\(status.configuredModel) ready" : "\(status.configuredModel) unavailable"
    }

    private var isGPU: Bool {
        viewModel.status?.provider == "vast" || coordinator.pythonIntelligenceCoordinator.selectedLLMBackend == "vast"
    }

    private var isWhisperReady: Bool {
        if case .ready = coordinator.pythonIntelligenceCoordinator.whisperModelDownloadState { return true }
        return false
    }

    private var whisperDetail: String {
        switch coordinator.pythonIntelligenceCoordinator.whisperModelDownloadState {
        case .idle: return "not started"
        case .downloading(let progress): return "downloading \(Int(progress * 100))%"
        case .verifying: return "verifying…"
        case .ready: return "ready"
        case .failed(let reason): return reason
        }
    }

    private var microphoneDetail: String {
        switch coordinator.pythonIntelligenceCoordinator.microphoneAuthorizationState {
        case .authorized: return "authorized"
        case .denied: return "denied — enable in System Settings > Privacy"
        case .restricted: return "restricted"
        case .undetermined: return "not yet requested"
        }
    }

    private var readinessWarning: String? {
        guard let status = viewModel.status else { return nil }
        if isGPU {
            if !status.reachable { return "GPU tunnel unavailable at \(status.baseUrl). Reconnect your existing SSH tunnel forwarding local port 18000. Veya will not switch providers automatically." }
            if !status.modelInstalled { return "The configured GPU model is not served by the tunneled endpoint." }
            return nil
        }
        if !status.reachable {
            return "Ollama isn't reachable at \(status.baseUrl.isEmpty ? "the configured address" : status.baseUrl). Start it with `ollama serve`. Question answering, coding/design assistance, and reports will be unavailable until it's running."
        }
        if !status.modelInstalled {
            return "The configured model \"\(status.configuredModel)\" isn't installed. Pull it with `ollama pull \(status.configuredModel)`, or pick an installed model below."
        }
        return nil
    }
}

@MainActor
final class LocalAIStatusViewModel: ObservableObject {
    @Published private(set) var status: LLMStatusResult?
    @Published private(set) var isLoading = false
    @Published private(set) var isApplying = false
    @Published var selectedModel = ""
    @Published var selectedBackend = "ollama"

    func refresh(coordinator: AppCoordinator) async {
        isLoading = true
        defer { isLoading = false }
        status = await coordinator.pythonIntelligenceCoordinator.fetchLLMStatus()
        selectedBackend = coordinator.pythonIntelligenceCoordinator.selectedLLMBackend
        if let status, selectedModel.isEmpty {
            selectedModel = status.modelInstalled ? status.configuredModel : ""
        }
    }

    func applySelectedModel(coordinator: AppCoordinator) async {
        isApplying = true
        defer { isApplying = false }
        if coordinator.pythonIntelligenceCoordinator.selectedLLMBackend == "vast" {
            await coordinator.pythonIntelligenceCoordinator.setGPUModelOverride(selectedModel)
        } else {
            await coordinator.pythonIntelligenceCoordinator.setOllamaModelOverride(selectedModel)
        }
        await refresh(coordinator: coordinator)
    }

    func applySelectedBackend(coordinator: AppCoordinator) async {
        isApplying = true
        defer { isApplying = false }
        await coordinator.pythonIntelligenceCoordinator.setLLMBackend(selectedBackend)
        await refresh(coordinator: coordinator)
    }
}
