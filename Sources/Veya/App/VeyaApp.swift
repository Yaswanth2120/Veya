import SwiftUI

@main
struct VeyaApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate

    var body: some Scene {
        WindowGroup {
            RootView()
                .environmentObject(appDelegate.appCoordinator)
                .frame(minWidth: 820, minHeight: 560)
        }
        .windowResizability(.contentSize)
    }
}
