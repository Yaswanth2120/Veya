#if DEBUG
import SwiftUI

/// DEBUG-only developer panel showing real local-VAD measurements —
/// raw RMS amplitude vs. the speech threshold, per audio chunk — so
/// turn-boundary behavior can be verified against the actual microphone
/// instead of inferred from whether an answer eventually appeared.
///
/// Populated only when the Python worker was launched with
/// `VEYA_VAD_DIAGNOSTICS=1` (see `ipc/dispatcher.py`); empty otherwise,
/// with a note explaining how to enable it, rather than silently showing
/// nothing with no explanation.
struct VADDiagnosticsView: View {
    @ObservedObject var conversationState: ConversationState

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text("VAD DIAGNOSTICS")
                    .font(.caption.bold())
                    .foregroundStyle(.secondary)
                Spacer()
                Text(conversationState.turnState.rawValue)
                    .font(.caption2.monospaced())
                    .foregroundStyle(.secondary)
            }

            if conversationState.vadDiagnostics.isEmpty {
                Text("No samples yet. Set VEYA_VAD_DIAGNOSTICS=1 in the worker's environment to populate this panel from real microphone input.")
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
        }
        .padding(10)
        .background(.black.opacity(0.05), in: RoundedRectangle(cornerRadius: 8))
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
