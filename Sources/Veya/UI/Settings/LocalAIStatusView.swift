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
            Text("AI Backend").font(.largeTitle.bold())
            Text("Choose local Ollama or your Vast GPU through the SSH tunnel. Audio transcription, document embeddings, and storage stay on this Mac. The GPU backend receives questions and selected document, memory, and conversation context to generate answers.")
                .font(.caption).foregroundStyle(.secondary)

            LocalAIStatusCard(showsModelPicker: true)
            Spacer()
        }
        .padding(28)
    }
}
