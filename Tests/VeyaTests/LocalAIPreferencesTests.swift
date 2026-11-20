import Foundation
import Testing
@testable import Veya

struct LocalAIPreferencesTests {
    @Test func oldSavedPreferencesKeepTheirModel() throws {
        let old = Data(#"{"ollamaModel":"existing-model"}"#.utf8)
        let decoded = try JSONDecoder().decode(LocalAIPreferences.self, from: old)
        #expect(decoded.ollamaModel == "existing-model")
        #expect(decoded.llmBackend == nil)
    }

    @Test func backendChoiceRoundTrips() throws {
        let original = LocalAIPreferences(ollamaModel: "local-model", llmBackend: "vast")
        let decoded = try JSONDecoder().decode(LocalAIPreferences.self, from: JSONEncoder().encode(original))
        #expect(decoded == original)
    }
}
