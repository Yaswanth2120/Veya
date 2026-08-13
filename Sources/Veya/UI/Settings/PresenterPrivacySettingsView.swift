import SwiftUI

struct PresenterPrivacySettingsView: View {
    @EnvironmentObject private var coordinator: AppCoordinator
    @ObservedObject private var privacyManager: PresenterPrivacyManager
    @ObservedObject private var displayManager: DisplayManager
    @ObservedObject private var safeShareManager: SafeShareManager

    @State private var isRunningTest = false
    @State private var testErrorMessage: String?

    init(privacyManager: PresenterPrivacyManager) {
        self.privacyManager = privacyManager
        self.displayManager = privacyManager.displayManager
        self.safeShareManager = privacyManager.safeShareManager
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            BackToDashboardButton()

            Text("Presenter Privacy")
                .font(.largeTitle.bold())

            ScrollView {
                VStack(alignment: .leading, spacing: 20) {
                    enableToggle

                    if privacyManager.preferences.enabled {
                        modePicker
                        sessionCheckToggles

                        switch privacyManager.preferences.selectedMode {
                        case .normal:
                            EmptyView()
                        case .directPrivateOverlay:
                            directOverlaySection
                        case .safeShare:
                            SafeShareControlsView(
                                privacyManager: privacyManager,
                                displayManager: displayManager,
                                safeShareManager: safeShareManager
                            )
                        }

                        if !privacyManager.history.isEmpty {
                            historySection
                        }

                        #if DEBUG
                        PresenterPrivacyDebugView(privacyManager: privacyManager)
                        #endif
                    }
                }
                .padding(.bottom, 24)
            }

            Spacer(minLength: 0)
        }
        .padding(28)
        .task {
            await privacyManager.evaluateSupport()
            await privacyManager.loadHistory()
        }
    }

    private var enableToggle: some View {
        Toggle(
            "Enable Presenter Privacy",
            isOn: Binding(
                get: { privacyManager.preferences.enabled },
                set: { newValue in
                    Task {
                        if newValue {
                            await privacyManager.enable()
                        } else {
                            privacyManager.disable()
                        }
                    }
                }
            )
        )
        .toggleStyle(.switch)
    }

    private var modePicker: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Mode").font(.headline)
            Picker("Mode", selection: modeBinding) {
                ForEach(PresenterPrivacyMode.allCases) { mode in
                    Text(mode.displayName).tag(mode)
                }
            }
            .pickerStyle(.radioGroup)

            if privacyManager.shouldRecommendSafeShare {
                Text("Direct privacy could not be verified. Recommended: use Veya Safe Share.")
                    .font(.caption)
                    .foregroundStyle(.orange)
            }
        }
    }

    private var sessionCheckToggles: some View {
        VStack(alignment: .leading, spacing: 4) {
            Toggle(
                "Check before starting a session",
                isOn: Binding(
                    get: { privacyManager.preferences.runTestBeforeSession },
                    set: { privacyManager.setRunTestBeforeSession($0) }
                )
            )
            Text("When off, Live Sessions start immediately — no privacy prompts.")
                .font(.caption2)
                .foregroundStyle(.secondary)

            Toggle(
                "Warn when unverified",
                isOn: Binding(
                    get: { privacyManager.preferences.warnWhenUnverified },
                    set: { privacyManager.setWarnWhenUnverified($0) }
                )
            )
            Text("Shows the ⚠ status in the overlay when privacy isn't confirmed.")
                .font(.caption2)
                .foregroundStyle(.secondary)
        }
        .toggleStyle(.switch)
    }

    private var modeBinding: Binding<PresenterPrivacyMode> {
        Binding(
            get: { privacyManager.preferences.selectedMode },
            set: { newValue in Task { await privacyManager.selectMode(newValue) } }
        )
    }

    private var directOverlaySection: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("PRIVATE OVERLAY — EXPERIMENTAL")
                .font(.caption.bold())
                .foregroundStyle(.secondary)

            HStack {
                statusIcon
                Text("Status: \(privacyManager.status.displayName)")
            }

            if let result = privacyManager.lastTestResult {
                Text(result.diagnosticMessage)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }

            if let testErrorMessage {
                Text(testErrorMessage)
                    .font(.caption)
                    .foregroundStyle(.red)
            }

            Text(
                "This test only tells you what Veya's own local capture diagnostic detects — " +
                "it is not a guarantee against any specific meeting app."
            )
            .font(.caption2)
            .foregroundStyle(.secondary)

            Button(isRunningTest ? "Testing…" : "Run Capture Test") {
                runTest()
            }
            .buttonStyle(.borderedProminent)
            .disabled(isRunningTest)
        }
    }

    private var statusIcon: some View {
        Group {
            switch privacyManager.status {
            case .verified:
                Image(systemName: "checkmark.circle.fill").foregroundStyle(.green)
            case .overlayDetected, .error:
                Image(systemName: "xmark.circle.fill").foregroundStyle(.red)
            case .uncertain, .unsupported:
                Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(.orange)
            case .testing:
                ProgressView().controlSize(.small)
            case .notTested, .disabled:
                Image(systemName: "questionmark.circle").foregroundStyle(.secondary)
            }
        }
    }

    private func runTest() {
        testErrorMessage = nil
        isRunningTest = true
        Task {
            defer { isRunningTest = false }
            do {
                _ = try await privacyManager.runCompatibilityTest()
            } catch {
                testErrorMessage = error.localizedDescription
            }
        }
    }

    private var historySection: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("COMPATIBILITY HISTORY")
                .font(.caption.bold())
                .foregroundStyle(.secondary)

            ForEach(privacyManager.history.prefix(10)) { record in
                HStack {
                    Text(record.result.status.displayName)
                        .font(.caption)
                    Spacer()
                    Text(record.mode.displayName)
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                    Text(record.testedAt.formatted(date: .abbreviated, time: .shortened))
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
            }
        }
    }
}
