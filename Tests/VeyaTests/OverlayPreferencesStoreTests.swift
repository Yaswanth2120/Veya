import Foundation
import Testing
@testable import Veya

@Suite("OverlayPreferencesStore")
struct OverlayPreferencesStoreTests {
    private func makeStore() -> OverlayPreferencesStore {
        let suiteName = "com.veya.tests.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suiteName)!
        return OverlayPreferencesStore(defaults: defaults)
    }

    @Test("load returns defaults when nothing has been saved")
    func loadDefaultsWhenEmpty() {
        let store = makeStore()
        #expect(store.load() == .default)
    }

    @Test("save then load round-trips preferences")
    func saveThenLoad() {
        let store = makeStore()
        let preferences = OverlayPreferences(opacity: 0.55, alwaysOnTop: false, compactMode: true)

        store.save(preferences)

        #expect(store.load() == preferences)
    }

    @Test("saved preferences persist across store instances sharing the same defaults suite")
    func persistsAcrossInstances() {
        let suiteName = "com.veya.tests.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suiteName)!
        let preferences = OverlayPreferences(opacity: 0.7, alwaysOnTop: true, compactMode: false)

        OverlayPreferencesStore(defaults: defaults).save(preferences)
        let reloaded = OverlayPreferencesStore(defaults: defaults).load()

        #expect(reloaded == preferences)
    }
}
