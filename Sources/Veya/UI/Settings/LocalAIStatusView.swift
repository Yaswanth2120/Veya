import SwiftUI

/// Diagnostics + repair for local answer/coding/design/report
/// intelligence — a review found the app silently degraded to "No local
/// LLM was available" with no way to see why or fix it from the UI, even
/// though Ollama was running with a different model installed than the
/// worker's hard-coded default. This is the actionable status/setup panel
/// that was missing.
struct LocalAIStatusView: View {
    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            BackToDashboardButton()
            Text("Local AI").font(.largeTitle.bold())
            Text("Ollama powers question answering, coding/design assistance, and session reports; Whisper powers real transcription — all local, never a cloud API.")
                .font(.caption).foregroundStyle(.secondary)

            LocalAIStatusCard(showsModelPicker: true)
            Spacer()
        }
        .padding(28)
    }
}
