import SwiftUI

struct LiveSessionView: View {
    @EnvironmentObject private var coordinator: AppCoordinator
    @ObservedObject var conversationState: ConversationState

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            header

            HStack(alignment: .top, spacing: 20) {
                transcriptPanel
                sidePanel
            }
        }
        .padding(28)
    }

    private var header: some View {
        HStack {
            VStack(alignment: .leading, spacing: 2) {
                Text("Live Session")
                    .font(.largeTitle.bold())
                Text(statusText)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Spacer()
            Button("End Session") {
                coordinator.endLiveSession()
            }
            .buttonStyle(.borderedProminent)
            .tint(.red)
        }
    }

    private var statusText: String {
        switch conversationState.phase {
        case .idle: return "Not started"
        case .live: return "Mocked transcript is streaming…"
        case .ended: return "Session ended"
        }
    }

    private var transcriptPanel: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("TRANSCRIPT (mocked)")
                .font(.caption.bold())
                .foregroundStyle(.secondary)

            ScrollView {
                VStack(alignment: .leading, spacing: 6) {
                    ForEach(conversationState.segments) { segment in
                        Text(segment.text)
                            .font(.callout)
                            .padding(.vertical, 2)
                    }
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .topLeading)
        .padding(12)
        .background(.quaternary.opacity(0.25), in: RoundedRectangle(cornerRadius: 10))
    }

    private var sidePanel: some View {
        VStack(alignment: .leading, spacing: 16) {
            VStack(alignment: .leading, spacing: 6) {
                Text("DETECTED QUESTIONS")
                    .font(.caption.bold())
                    .foregroundStyle(.secondary)
                if conversationState.detectedQuestions.isEmpty {
                    Text("None yet.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                } else {
                    ForEach(conversationState.detectedQuestions) { question in
                        Text(question.text)
                            .font(.caption)
                    }
                }
            }

            VStack(alignment: .leading, spacing: 8) {
                Text("OVERLAY")
                    .font(.caption.bold())
                    .foregroundStyle(.secondary)
                Button("Toggle overlay visibility") {
                    coordinator.overlayWindowController?.toggleVisibility()
                }
                Button("Toggle compact / expanded") {
                    coordinator.overlayWindowController?.toggleCompactMode()
                }
                Text("Hotkeys: ⌘⇧O show/hide · ⌘⇧C compact/expand")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
        }
        .frame(width: 260, alignment: .topLeading)
        .padding(12)
        .background(.quaternary.opacity(0.25), in: RoundedRectangle(cornerRadius: 10))
    }
}
