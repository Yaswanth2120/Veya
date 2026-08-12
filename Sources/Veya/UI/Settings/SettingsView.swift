import SwiftUI

struct SettingsView: View {
    @StateObject private var viewModel = SettingsViewModel()

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            BackToDashboardButton()

            Text("Settings")
                .font(.largeTitle.bold())

            Form {
                Section("Overlay") {
                    Slider(value: $viewModel.opacity, in: 0.2...1.0) {
                        Text("Opacity")
                    }
                    Toggle("Always on top", isOn: $viewModel.alwaysOnTop)
                    Toggle("Compact mode", isOn: $viewModel.compactMode)
                }
            }
            .formStyle(.grouped)

            Spacer()
        }
        .padding(28)
        .task { viewModel.load() }
        .onChange(of: viewModel.opacity) { _, _ in viewModel.persist() }
        .onChange(of: viewModel.alwaysOnTop) { _, _ in viewModel.persist() }
        .onChange(of: viewModel.compactMode) { _, _ in viewModel.persist() }
    }
}

@MainActor
final class SettingsViewModel: ObservableObject {
    @Published var opacity: Double = OverlayPreferences.default.opacity
    @Published var alwaysOnTop: Bool = OverlayPreferences.default.alwaysOnTop
    @Published var compactMode: Bool = OverlayPreferences.default.compactMode

    private let store = OverlayPreferencesStore()

    func load() {
        let preferences = store.load()
        opacity = preferences.opacity
        alwaysOnTop = preferences.alwaysOnTop
        compactMode = preferences.compactMode
    }

    func persist() {
        store.save(OverlayPreferences(opacity: opacity, alwaysOnTop: alwaysOnTop, compactMode: compactMode))
    }
}
