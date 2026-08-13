import Testing
@testable import Veya

@Suite("DisplayManager selection")
struct DisplayManagerSelectionTests {
    private let builtIn = DisplayInfo(id: 1, name: "Built-in Retina Display", frame: .zero, visibleFrame: .zero, scaleFactor: 2, isBuiltIn: true)
    private let external = DisplayInfo(id: 2, name: "External Display", frame: .zero, visibleFrame: .zero, scaleFactor: 1, isBuiltIn: false)

    @Test("single display: the only display is always selected")
    func singleDisplay() {
        let selected = DisplayManager.selectPreferredDisplay(from: [builtIn], preferredDisplayID: nil)
        #expect(selected == builtIn)
    }

    @Test("multiple displays: the built-in display is preferred when no preference is set")
    func multipleDisplaysDefaultsToBuiltIn() {
        let selected = DisplayManager.selectPreferredDisplay(from: [external, builtIn], preferredDisplayID: nil)
        #expect(selected == builtIn)
    }

    @Test("multiple displays: an explicit preference wins over the built-in default")
    func explicitPreferenceWins() {
        let selected = DisplayManager.selectPreferredDisplay(from: [builtIn, external], preferredDisplayID: external.id)
        #expect(selected == external)
    }

    @Test("preferred display missing (disconnected): falls back to the built-in display")
    func preferredDisplayMissingFallsBackToBuiltIn() {
        let selected = DisplayManager.selectPreferredDisplay(from: [builtIn], preferredDisplayID: external.id)
        #expect(selected == builtIn)
    }

    @Test("display removed: an empty display list resolves to nil, not a crash")
    func noDisplaysResolvesToNil() {
        let selected = DisplayManager.selectPreferredDisplay(from: [], preferredDisplayID: builtIn.id)
        #expect(selected == nil)
    }

    @Test("no built-in and no preference: falls back to the first available display")
    func noBuiltInFallsBackToFirst() {
        let secondExternal = DisplayInfo(id: 3, name: "Second External", frame: .zero, visibleFrame: .zero, scaleFactor: 1, isBuiltIn: false)
        let selected = DisplayManager.selectPreferredDisplay(from: [external, secondExternal], preferredDisplayID: nil)
        #expect(selected == external)
    }
}
