#if DEBUG
import SwiftUI

/// DEBUG-only diagnostics panel. Shows subsystem/window/stream state for
/// troubleshooting — never screen contents, transcript contents, AI
/// answers, or uploaded documents.
struct PresenterPrivacyDebugView: View {
    @ObservedObject var privacyManager: PresenterPrivacyManager

    @State private var diagnostics: SafeShareDiagnostics?
    @State private var screenRecordingPermission = false
    @State private var memoryBytes: UInt64?

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("PRESENTER PRIVACY DEBUG")
                .font(.caption.bold())
                .foregroundStyle(.secondary)

            row("macOS version", ProcessInfo.processInfo.operatingSystemVersionString)
            row("Veya version", AppVersion.current)
            row("Selected display", privacyManager.displayManager.preferredDisplay(preferredDisplayID: privacyManager.preferences.preferredDisplayID).map { "\($0.name) (id \($0.id))" } ?? "none")
            row("Display scale factor", privacyManager.displayManager.preferredDisplay(preferredDisplayID: privacyManager.preferences.preferredDisplayID).map { String(format: "%.1f", $0.scaleFactor) } ?? "—")
            row("Screen Recording permission", screenRecordingPermission ? "granted" : "not granted")
            row("Current privacy status", privacyManager.status.displayName)
            row("Safe Share running", diagnostics?.isRunning == true ? "yes" : "no")
            row("Configured FPS", diagnostics.map { String($0.configuredFPS) } ?? "—")
            row("Stream resolution", diagnostics.map { "\($0.streamWidth)x\($0.streamHeight)" } ?? "—")
            row("Frames received", diagnostics.map { String($0.framesReceived) } ?? "—")
            row("Frames dropped", diagnostics.map { String($0.framesDropped) } ?? "—")
            row("Verification confidence", privacyManager.lastTestResult.map { String(format: "%.2f", $0.confidence) } ?? "—")
            row("Memory usage", memoryBytes.map { ByteCountFormatter.string(fromByteCount: Int64($0), countStyle: .memory) } ?? "—")
        }
        .padding(10)
        .background(.black.opacity(0.05), in: RoundedRectangle(cornerRadius: 8))
        .task {
            screenRecordingPermission = CGPreflightScreenCaptureAccess()
            memoryBytes = MemoryDiagnostics.residentMemoryBytes()
        }
        .task(id: privacyManager.isSafeShareRunning) {
            diagnostics = await privacyManager.safeShareManager.diagnostics()
        }
    }

    private func row(_ label: String, _ value: String) -> some View {
        HStack {
            Text(label).font(.caption2).foregroundStyle(.secondary)
            Spacer()
            Text(value).font(.caption2.monospaced())
        }
    }
}
#endif
