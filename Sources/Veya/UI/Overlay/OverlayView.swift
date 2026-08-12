import SwiftUI

enum OverlayAnswerStyle: String, CaseIterable, Identifiable {
    case short = "Short"
    case deep = "Deep"
    case source = "Source"
    case followUp = "Follow-up"

    var id: String { rawValue }
}

/// The overlay's content, matching the compact panel mock in the build
/// prompt: live question, suggested talking points, a source label, and a
/// row of display-style controls. Purely a renderer of
/// `ConversationState.currentAnswer` — no intelligence lives here.
struct OverlayView: View {
    @ObservedObject var conversationState: ConversationState
    let controller: OverlayWindowController

    @State private var style: OverlayAnswerStyle = .short

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            header

            if let answer = conversationState.currentAnswer {
                answerContent(for: answer)
            } else {
                emptyState
            }

            Spacer(minLength: 0)

            styleRow
        }
        .padding(14)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 14, style: .continuous)
                .strokeBorder(Color.white.opacity(0.15), lineWidth: 1)
        )
    }

    private var header: some View {
        HStack {
            Text("LIVE QUESTION")
                .font(.caption.bold())
                .foregroundStyle(.secondary)
            Spacer()
            Button {
                controller.toggleCompactMode()
            } label: {
                Image(systemName: controller.preferences.compactMode ? "arrow.up.left.and.arrow.down.right" : "arrow.down.right.and.arrow.up.left")
            }
            .buttonStyle(.plain)
        }
    }

    @ViewBuilder
    private func answerContent(for answer: CopilotAnswer) -> some View {
        Text(answer.question)
            .font(.headline)
            .fixedSize(horizontal: false, vertical: true)

        Text("SUGGESTED TALKING POINTS")
            .font(.caption.bold())
            .foregroundStyle(.secondary)
            .padding(.top, 4)

        VStack(alignment: .leading, spacing: 4) {
            ForEach(pointsToShow(for: answer), id: \.self) { point in
                Label(point, systemImage: "circle.fill")
                    .labelStyle(BulletLabelStyle())
                    .font(.callout)
            }
        }

        if let source = answer.sources.first {
            Text("Source: \(source)")
                .font(.caption)
                .foregroundStyle(.secondary)
                .padding(.top, 4)
        }
    }

    private func pointsToShow(for answer: CopilotAnswer) -> [String] {
        switch style {
        case .short:
            return Array(answer.talkingPoints.prefix(2))
        case .deep, .source, .followUp:
            return answer.talkingPoints
        }
    }

    private var emptyState: some View {
        Text("Listening for questions…")
            .font(.callout)
            .foregroundStyle(.secondary)
    }

    private var styleRow: some View {
        HStack(spacing: 0) {
            ForEach(OverlayAnswerStyle.allCases) { option in
                Button(option.rawValue) {
                    style = option
                }
                .buttonStyle(.plain)
                .font(.caption.weight(style == option ? .bold : .regular))
                .foregroundStyle(style == option ? .primary : .secondary)
                .frame(maxWidth: .infinity)
            }
        }
        .padding(.top, 6)
    }
}

private struct BulletLabelStyle: LabelStyle {
    func makeBody(configuration: Configuration) -> some View {
        HStack(alignment: .top, spacing: 6) {
            configuration.icon
                .font(.system(size: 5))
                .padding(.top, 6)
            configuration.title
        }
    }
}
