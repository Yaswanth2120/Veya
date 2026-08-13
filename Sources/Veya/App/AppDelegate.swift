import AppKit

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    // Real microphone *and* meeting/system-audio capture are opted into
    // here, explicitly, rather than as `PythonIntelligenceCoordinator`'s
    // own defaults — every other call site (tests, previews) gets
    // `audioCapture`/`meetingAudioCapture: nil` (real transcription/
    // meeting audio disabled) by default, so constructing a coordinator
    // never touches AVFoundation/ScreenCaptureKit unless this actual app
    // launch path asks for it. `SystemAudioCapture` only actually
    // requests Screen Recording permission when `start()` is called
    // (Section 16's meeting-audio track, opt-in per session) — merely
    // constructing it here is inert.
    let appCoordinator = AppCoordinator(
        pythonIntelligenceCoordinator: PythonIntelligenceCoordinator(
            audioCapture: MicrophoneAudioCapture(),
            meetingAudioCapture: SystemAudioCapture()
        )
    )

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        NSApp.activate(ignoringOtherApps: true)
        appCoordinator.registerHotkeys()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        true
    }
}
