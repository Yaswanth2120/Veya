import Foundation

/// Persists `OverlayPreferences` (opacity, always-on-top, compact mode) to
/// `UserDefaults`. Window frame/position persistence is handled separately
/// by `OverlayWindowController` via `setFrameAutosaveName`, which is
/// AppKit's own persistence mechanism and doesn't need to round-trip
/// through here.
final class OverlayPreferencesStore {
    private let defaults: UserDefaults
    private let key = "overlayPreferences"

    init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
    }

    func load() -> OverlayPreferences {
        guard
            let data = defaults.data(forKey: key),
            let preferences = try? JSONDecoder().decode(OverlayPreferences.self, from: data)
        else {
            return .default
        }
        return preferences
    }

    func save(_ preferences: OverlayPreferences) {
        guard let data = try? JSONEncoder().encode(preferences) else { return }
        defaults.set(data, forKey: key)
    }
}
