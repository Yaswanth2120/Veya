import AppKit
import SwiftUI

/// Section 16: the meeting/system-audio track's live controls — source
/// selection (preferring a specific app over indiscriminate all-system
/// audio), the one-time consent disclosure, a visible capture-active
/// indicator, and a one-click stop control. Reachable only while a real
/// transcription session is actually running (see `LiveSessionView`),
/// since the meeting-audio track shares that session's orchestrator.
struct MeetingAudioControlView: View {
    @ObservedObject var pythonIntelligenceCoordinator: PythonIntelligenceCoordinator

    @State private var availableApplications: [SelectableAudioApplication] = []
    @State private var selectedApplication: SelectableAudioApplication?
    @State private var isShowingConsent = false
    @State private var isLoadingApplications = false
    @State private var statusMessage: String?
    @State private var screenRecordingPermissionGranted = CGPreflightScreenCaptureAccess()

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text("MEETING AUDIO")
                    .font(.caption.bold())
                    .foregroundStyle(.secondary)
                Spacer()
                if pythonIntelligenceCoordinator.meetingAudioActive {
                    Label("Capturing", systemImage: "circle.fill")
                        .font(.caption2)
                        .foregroundStyle(.red)
                        .labelStyle(.titleAndIcon)
                }
            }

            if pythonIntelligenceCoordinator.meetingAudioActive {
                if let selectedApplication {
                    Text("Source: \(selectedApplication.displayName)")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                } else {
                    Text("Source: all system audio")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
                Button("Stop meeting audio") {
                    Task {
                        await pythonIntelligenceCoordinator.endMeetingAudioCapture()
                        statusMessage = nil
                    }
                }
                .font(.caption)
            } else if !screenRecordingPermissionGranted {
                Text("Meeting audio unavailable — Screen Recording permission is required.")
                    .font(.caption2)
                    .foregroundStyle(.orange)
                Button("Open Screen Recording Settings") {
                    if let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture") {
                        NSWorkspace.shared.open(url)
                    }
                }
                .font(.caption)
                Button("Re-check permission") {
                    screenRecordingPermissionGranted = CGPreflightScreenCaptureAccess()
                }
                .font(.caption)
            } else {
                Button(isLoadingApplications ? "Loading applications…" : "Select meeting app…") {
                    Task { await loadApplications() }
                }
                .font(.caption)
                .disabled(isLoadingApplications)

                if !availableApplications.isEmpty {
                    Picker("Application", selection: $selectedApplication) {
                        Text("All system audio").tag(SelectableAudioApplication?.none)
                        ForEach(availableApplications) { app in
                            Text(app.displayName).tag(SelectableAudioApplication?.some(app))
                        }
                    }
                    .font(.caption)
                    .labelsHidden()

                    Button("Start meeting audio") {
                        isShowingConsent = true
                    }
                    .font(.caption)
                }

                if let statusMessage {
                    Text(statusMessage)
                        .font(.caption2)
                        .foregroundStyle(.orange)
                }
            }
        }
        .padding(10)
        .background(.quaternary.opacity(0.15), in: RoundedRectangle(cornerRadius: 8))
        .alert("Transcribe meeting audio locally?", isPresented: $isShowingConsent) {
            Button("Cancel", role: .cancel) {}
            Button("Start") {
                Task { await startCapture() }
            }
        } message: {
            Text("Veya will transcribe the selected audio locally on this Mac — nothing is sent to the cloud. You're responsible for complying with any consent, workplace, and meeting-platform policies that apply to recording or transcribing this conversation.")
        }
    }

    private func loadApplications() async {
        isLoadingApplications = true
        defer { isLoadingApplications = false }
        do {
            availableApplications = try await SystemAudioCapture.selectableApplications()
        } catch {
            statusMessage = "Couldn't list running applications — check Screen Recording permission."
            screenRecordingPermissionGranted = CGPreflightScreenCaptureAccess()
        }
    }

    private func startCapture() async {
        let source: SystemAudioSource? = selectedApplication.map {
            .application(processID: $0.processID, bundleIdentifier: $0.bundleIdentifier, displayName: $0.displayName)
        }
        let started = await pythonIntelligenceCoordinator.beginMeetingAudioCapture(source: source)
        if !started {
            statusMessage = "Couldn't start meeting audio capture."
        } else {
            statusMessage = nil
        }
    }
}
