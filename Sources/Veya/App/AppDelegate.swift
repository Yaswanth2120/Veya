import AppKit

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    // Real microphone capture is opted into here, explicitly, rather than
    // as `PythonIntelligenceCoordinator`'s own default — every other call
    // site (tests, previews) gets `audioCapture: nil` (real transcription
    // disabled) by default, so constructing a coordinator never touches
    // AVFoundation/microphone permission unless this actual app launch
    // path asks for it.
    let appCoordinator = AppCoordinator(
        pythonIntelligenceCoordinator: PythonIntelligenceCoordinator(audioCapture: MicrophoneAudioCapture())
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
