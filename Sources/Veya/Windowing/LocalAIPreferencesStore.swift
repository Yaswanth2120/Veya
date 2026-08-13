import Foundation

/// The user's chosen local Ollama model, if they've overridden the
/// worker's built-in default (`llama3.2`) — a review found this default
/// isn't installed on a real dev machine with no way to change it from
/// the app, leaving answer/report/coding/design intelligence silently
/// unavailable with no actionable guidance. Empty means "no override";
/// the worker falls back to its own default (or `VEYA_OLLAMA_MODEL` from
/// the shell environment, if a developer set one).
struct LocalAIPreferences: Codable, Equatable, Sendable {
    var ollamaModel: String = ""

    static let `default` = LocalAIPreferences()
}

final class LocalAIPreferencesStore {
    private let defaults: UserDefaults
    private let key = "localAIPreferences"

    init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
    }

    func load() -> LocalAIPreferences {
        guard
            let data = defaults.data(forKey: key),
            let preferences = try? JSONDecoder().decode(LocalAIPreferences.self, from: data)
        else {
            return .default
        }
        return preferences
    }

    func save(_ preferences: LocalAIPreferences) {
        guard let data = try? JSONEncoder().encode(preferences) else { return }
        defaults.set(data, forKey: key)
    }
}
