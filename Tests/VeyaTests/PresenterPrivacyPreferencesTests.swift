import Foundation
import Testing
@testable import Veya

@Suite("PresenterPrivacyPreferences")
struct PresenterPrivacyPreferencesTests {
    @Test("defaults match the build prompt's recommended defaults")
    func defaults() {
        let defaults = PresenterPrivacyPreferences.default
        #expect(defaults.enabled == false)
        #expect(defaults.selectedMode == .safeShare)
        #expect(defaults.runTestBeforeSession == true)
        #expect(defaults.warnWhenUnverified == true)
        #expect(defaults.preferredDisplayID == nil)
        #expect(defaults.safeShareFPS == 30)
    }

    private func makeStore() -> PresenterPrivacyPreferencesStore {
        let suiteName = "com.veya.tests.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suiteName)!
        return PresenterPrivacyPreferencesStore(defaults: defaults)
    }

    @Test("load returns defaults when nothing has been saved")
    func loadReturnsDefaultsWhenEmpty() {
        let store = makeStore()
        #expect(store.load() == .default)
    }

    @Test("save then load round-trips preferences")
    func saveThenLoadRoundTrips() {
        let store = makeStore()
        let preferences = PresenterPrivacyPreferences(
            enabled: true,
            selectedMode: .directPrivateOverlay,
            runTestBeforeSession: false,
            warnWhenUnverified: false,
            preferredDisplayID: 42,
            safeShareFPS: 15
        )
        store.save(preferences)
        #expect(store.load() == preferences)
    }

    @Test("malformed persisted data falls back to defaults rather than crashing")
    func malformedDataFallsBackToDefaults() {
        let suiteName = "com.veya.tests.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suiteName)!
        defaults.set(Data([0xFF, 0x00, 0x01]), forKey: "presenterPrivacyPreferences")
        let store = PresenterPrivacyPreferencesStore(defaults: defaults)
        #expect(store.load() == .default)
    }
}
