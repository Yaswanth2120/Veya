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
    @ObservedObject var privacyManager: PresenterPrivacyManager
    let controller: OverlayWindowController

    @State private var style: OverlayAnswerStyle = .short

    /// Mirrors `LiveSessionView.isAnswerRoundInFlight` exactly — the
    /// overlay must show the same primary current/draft answer state as
    /// the main app, never a different one (Section 17).
    private var isAnswerRoundInFlight: Bool {
        conversationState.isGeneratingAnswer
            || conversationState.isDraftingAnswer
            || conversationState.isClassifyingQuestion
            || conversationState.isAnalyzingQuestion
            || conversationState.candidateState == .candidate
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            header

            if let failureMessage = conversationState.lastAnswerFailureMessage {
                Label(failureMessage, systemImage: "exclamationmark.triangle.fill")
                    .font(.caption)
                    .foregroundStyle(.orange)
                    .fixedSize(horizontal: false, vertical: true)
            }

            if conversationState.isAnswerSlow {
                // Retry/Skip controls live on the main Answer panel
                // (which is always primary/available — see
                // `LiveSessionView`); this compact overlay only mirrors
                // the same status so it's never left showing a silent,
                // unexplained spinner.
                Label("Local model is taking longer than expected…", systemImage: "clock.badge.exclamationmark")
                    .font(.caption)
                    .foregroundStyle(.orange)
            }

            // A completed answer is never hidden just because a newer
            // round is in flight — the in-flight content is shown above
            // it instead, same as the main app's Answer panel.
            if isAnswerRoundInFlight {
                draftContent
                if let answer = conversationState.currentAnswer {
                    previousAnswerContent(for: answer)
                }
            } else if let answer = conversationState.currentAnswer {
                answerContent(for: answer)
            } else {
                emptyState
            }

            Spacer(minLength: 0)

            styleRow

            if let privacyIndicatorText {
                Text(privacyIndicatorText)
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
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

        // The natural, speakable answer is the primary content — talking
        // points below are optional supporting detail, never the answer
        // itself (Section 16).
        if !answer.answerText.isEmpty {
            Text(answer.answerText)
                .font(.callout)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.top, 2)
        }

        // Falls back to showing points even in `.short` style when there's
        // no natural answer text at all (e.g. an answer persisted before
        // this field existed) — never leaves the panel effectively blank.
        if (style != .short || answer.answerText.isEmpty), !pointsToShow(for: answer).isEmpty {
            Text("DETAILS")
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

    /// A compact in-flight view — the same speculative/generating content
    /// `LiveSessionView`'s answer panel shows, kept short since the
    /// overlay must stay small and non-obstructive.
    @ViewBuilder
    private var draftContent: some View {
        if let questionText = conversationState.finalizedQuestionText ?? conversationState.candidateQuestionText {
            Text(questionText)
                .font(.headline)
                .fixedSize(horizontal: false, vertical: true)
        }
        HStack(spacing: 6) {
            ProgressView().controlSize(.small)
            Text(inFlightStatusLabel)
                .font(.caption)
                .foregroundStyle(.secondary)
        }
        // The draft's own streamed text is real generated content, shown
        // as soon as the first delta arrives — never a placeholder.
        if conversationState.isDraftingAnswer, !conversationState.draftAnswerText.isEmpty {
            Text(conversationState.draftAnswerText)
                .font(.callout)
                .lineLimit(4)
        } else if let partial = conversationState.partialAnswerText, !partial.isEmpty {
            Text(partial)
                .font(.callout)
                .lineLimit(4)
        }
    }

    private var inFlightStatusLabel: String {
        if conversationState.isRefiningAnswer { return "Refining…" }
        if conversationState.isDraftingAnswer { return "Drafting…" }
        if conversationState.isGeneratingAnswer { return "Generating…" }
        if conversationState.isClassifyingQuestion || conversationState.isAnalyzingQuestion { return "Understanding…" }
        return "Hearing a question…"
    }

    /// A compact, de-emphasized rendering of the last completed answer,
    /// shown below in-flight content — never removed until a newer
    /// answer actually completes.
    @ViewBuilder
    private func previousAnswerContent(for answer: CopilotAnswer) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text("PREVIOUS ANSWER")
                .font(.caption2.bold())
                .foregroundStyle(.secondary)
            if !answer.answerText.isEmpty {
                Text(answer.answerText)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(3)
            }
        }
        .padding(.top, 4)
        .opacity(0.7)
    }

    private var emptyState: some View {
        Text("Listening for questions…")
            .font(.callout)
            .foregroundStyle(.secondary)
    }

    /// A minimal, non-intrusive status line — never more than this one
    /// short string — per the build prompt's "don't clutter the overlay."
    /// When things are fine it always shows the ✓ confirmation; the ⚠
    /// warning variants are gated behind `warnWhenUnverified` — a user who
    /// has turned that off doesn't want to be alarmed by it mid-session.
    private var privacyIndicatorText: String? {
        guard privacyManager.preferences.enabled else { return nil }
        let warnWhenUnverified = privacyManager.preferences.warnWhenUnverified
        switch privacyManager.preferences.selectedMode {
        case .normal:
            return nil
        case .safeShare:
            if privacyManager.isSafeShareRunning { return "Privacy: Safe Share ✓" }
            return warnWhenUnverified ? "Privacy: Safe Share not running ⚠" : nil
        case .directPrivateOverlay:
            if privacyManager.status == .verified { return "Privacy: Direct / Verified ✓" }
            return warnWhenUnverified ? "Privacy: Unverified ⚠" : nil
        }
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
