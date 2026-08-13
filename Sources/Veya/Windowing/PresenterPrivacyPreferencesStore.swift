import Foundation

/// Persists `PresenterPrivacyPreferences` to `UserDefaults`, same pattern
/// as `OverlayPreferencesStore`.
final class PresenterPrivacyPreferencesStore {
    private let defaults: UserDefaults
    private let key = "presenterPrivacyPreferences"

    init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
    }

    func load() -> PresenterPrivacyPreferences {
        guard
            let data = defaults.data(forKey: key),
            let preferences = try? JSONDecoder().decode(PresenterPrivacyPreferences.self, from: data)
        else {
            return .default
        }
        return preferences
    }

    func save(_ preferences: PresenterPrivacyPreferences) {
        guard let data = try? JSONEncoder().encode(preferences) else { return }
        defaults.set(data, forKey: key)
    }
}
