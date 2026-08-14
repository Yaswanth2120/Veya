#if DEBUG
import SwiftUI

/// DEBUG-only developer panel showing real live-session pipeline state —
/// safe metadata only (counts, timestamps, state names): never raw
/// audio, transcript text, prompts, answers, document text, or model
/// output. VAD samples (raw RMS vs. threshold) are only ever populated
/// when the Python worker was launched with `VEYA_VAD_DIAGNOSTICS=1`
/// (see `ipc/dispatcher.py`); the rest of this panel is always live.
struct VADDiagnosticsView: View {
    @ObservedObject var conversationState: ConversationState

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("LIVE SESSION DIAGNOSTICS")
                .font(.caption.bold())
                .foregroundStyle(.secondary)

            pipelineSummary

            HStack {
                Text("VAD samples").font(.caption2.bold()).foregroundStyle(.secondary)
                Spacer()
                Text(conversationState.turnState.rawValue)
                    .font(.caption2.monospaced())
                    .foregroundStyle(.secondary)
            }
            if conversationState.vadDiagnostics.isEmpty {
                Text("No samples yet. Set VEYA_VAD_DIAGNOSTICS=1 in the worker's environment to populate this from real microphone input.")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            } else {
                ScrollView {
                    VStack(alignment: .leading, spacing: 2) {
                        ForEach(conversationState.vadDiagnostics.suffix(30)) { sample in
                            row(for: sample)
                        }
                    }
                }
                .frame(maxHeight: 160)
            }

            Divider()
            Text("ANSWER LATENCY").font(.caption2.bold()).foregroundStyle(.secondary)
            if conversationState.answerTimingSamples.isEmpty {
                Text("No samples yet. Set VEYA_ANSWER_TIMING_DIAGNOSTICS=1 in the worker's environment to populate this.")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            } else {
                VStack(alignment: .leading, spacing: 2) {
                    ForEach(conversationState.answerTimingSamples.suffix(10)) { sample in
                        timingRow(for: sample)
                    }
                }
            }
        }
        .padding(10)
        .background(.black.opacity(0.05), in: RoundedRectangle(cornerRadius: 8))
    }

    private func timingRow(for sample: ConversationState.AnswerTimingSample) -> some View {
        HStack(spacing: 8) {
            Text("usable")
                .font(.caption2)
                .foregroundStyle(.secondary)
            Text(sample.firstSpeakableLatencySeconds.map { String(format: "%.2fs", $0) } ?? "—")
                .font(.caption2.monospaced())
            Text("rendered")
                .font(.caption2)
                .foregroundStyle(.secondary)
            Text(sample.firstRenderedLatencySeconds.map { String(format: "%.2fs", $0) } ?? "—")
                .font(.caption2.monospaced())
            Text("total")
                .font(.caption2)
                .foregroundStyle(.secondary)
            Text(sample.totalLatencySeconds.map { String(format: "%.2fs", $0) } ?? "—")
                .font(.caption2.monospaced())
        }
    }

    private var pipelineSummary: some View {
        VStack(alignment: .leading, spacing: 2) {
            metadataRow("ASR provider", conversationState.asrProvider ?? "—")
            metadataRow("Latest partial received", relativeTimestamp(conversationState.latestPartialReceivedAt))
            metadataRow("Latest final received", relativeTimestamp(conversationState.latestFinalReceivedAt))
            metadataRow("Candidate tracker state", conversationState.candidateState.rawValue)
            metadataRow("Candidate revision count", String(conversationState.candidateRevisionCount))
            metadataRow("Active draft sequence", conversationState.draftSequence.map(String.init) ?? "—")
            metadataRow("Last draft transition", conversationState.lastDraftTransitionReason?.rawValue ?? "—")
            metadataRow("Audio chunks sent / dropped", "\(conversationState.audioChunksSent) / \(conversationState.audioChunksDropped)")
        }
    }

    private func metadataRow(_ label: String, _ value: String) -> some View {
        HStack {
            Text(label).font(.caption2).foregroundStyle(.secondary)
            Spacer()
            Text(value).font(.caption2.monospaced())
        }
    }

    private func relativeTimestamp(_ date: Date?) -> String {
        guard let date else { return "—" }
        let seconds = Date().timeIntervalSince(date)
        return String(format: "%.1fs ago", max(0, seconds))
    }

    private func row(for sample: ConversationState.VADDiagnosticSample) -> some View {
        HStack(spacing: 8) {
            Text(sample.isInSpeech ? "speech" : "silence")
                .font(.caption2.monospaced())
                .foregroundStyle(sample.isInSpeech ? .green : .secondary)
                .frame(width: 52, alignment: .leading)
            Text(String(format: "rms %5.0f / thr %5.0f", sample.rms, sample.threshold))
                .font(.caption2.monospaced())
            Text(String(format: "speech %.1fs  silence %.1fs", sample.speechSeconds, sample.silenceSeconds))
                .font(.caption2.monospaced())
                .foregroundStyle(.secondary)
        }
    }
}
#endif
