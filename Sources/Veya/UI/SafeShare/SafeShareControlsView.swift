import SwiftUI

/// Safe Share's controls, kept separate from `SafeShareView`/the actual
/// "Veya Safe Share" window content per the build prompt — controls live
/// in Veya's own UI, never drawn over the shareable content itself.
struct SafeShareControlsView: View {
    @ObservedObject var privacyManager: PresenterPrivacyManager
    @ObservedObject var displayManager: DisplayManager
    @ObservedObject var safeShareManager: SafeShareManager

    @State private var selectedQuality: SafeShareQuality = .balanced
    @State private var errorMessage: String?
    @State private var isStarting = false

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text(
                "Safe Share creates a clean live view of your selected display with Veya removed. " +
                "Share the \u{201C}Veya Safe Share\u{201D} window in your meeting app."
            )
            .font(.callout)
            .foregroundStyle(.secondary)
            .fixedSize(horizontal: false, vertical: true)

            if !safeShareManager.isRunning {
                Picker("Display", selection: displaySelectionBinding) {
                    ForEach(displayManager.displays) { display in
                        Text(display.isBuiltIn ? "\(display.name) (Built-in)" : display.name)
                            .tag(Optional(display.id))
                    }
                }

                Picker("Quality", selection: $selectedQuality) {
                    ForEach(SafeShareQuality.allCases) { quality in
                        Text(quality.displayName).tag(quality)
                    }
                }

                Picker("Frame Rate", selection: fpsBinding) {
                    Text("15 FPS").tag(15)
                    Text("30 FPS").tag(30)
                }

                if let errorMessage {
                    Text(errorMessage)
                        .font(.caption)
                        .foregroundStyle(.red)
                }

                Button(isStarting ? "Starting…" : "Start Safe Share") {
                    startSafeShare()
                }
                .buttonStyle(.borderedProminent)
                .disabled(isStarting || displayManager.displays.isEmpty)
            } else {
                Label("Safe Share Running", systemImage: "checkmark.circle.fill")
                    .foregroundStyle(.green)
                    .font(.headline)

                Text("Share this window in your meeting application:")
                    .font(.callout)
                Text("Veya Safe Share")
                    .font(.callout.bold())

                Button("Stop Safe Share") {
                    Task { await privacyManager.stopSafeShare() }
                }
                .buttonStyle(.bordered)
                .tint(.red)
            }
        }
        .onAppear { displayManager.refresh() }
    }

    private var displaySelectionBinding: Binding<UInt32?> {
        Binding(
            get: { privacyManager.preferences.preferredDisplayID ?? displayManager.builtInDisplay?.id },
            set: { privacyManager.setPreferredDisplay($0) }
        )
    }

    private var fpsBinding: Binding<Int> {
        Binding(
            get: { privacyManager.preferences.safeShareFPS },
            set: { privacyManager.setSafeShareFPS($0) }
        )
    }

    private func startSafeShare() {
        errorMessage = nil
        isStarting = true
        Task {
            defer { isStarting = false }
            do {
                try await privacyManager.startSafeShare()
            } catch {
                errorMessage = error.localizedDescription
            }
        }
    }
}
