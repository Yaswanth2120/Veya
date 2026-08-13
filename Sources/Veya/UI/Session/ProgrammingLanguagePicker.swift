import SwiftUI

/// The fixed catalog a searchable language picker offers, plus "No
/// preference" and "Other" (which reveals a free-text field) — replaces
/// the old raw `TextField` for preferred programming language.
enum ProgrammingLanguageCatalog {
    static let noPreference = ""
    static let other = "Other"

    static let languages: [String] = [
        "Swift", "Python", "JavaScript", "TypeScript", "Java", "Kotlin",
        "C", "C++", "C#", "Go", "Rust", "Ruby", "PHP", "SQL", "Bash / Shell", "HTML / CSS"
    ]
}

/// A searchable dropdown for a preferred programming language. `selection`
/// holds the persisted value directly — `""` means "No preference", and
/// any value not in `ProgrammingLanguageCatalog.languages` is treated as a
/// custom "Other" entry (so a previously free-typed value still displays
/// sensibly rather than silently resetting).
struct ProgrammingLanguagePicker: View {
    @Binding var selection: String
    @State private var searchText = ""
    @State private var customText = ""
    @State private var isOtherSelected = false

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Picker("Preferred language", selection: pickerBinding) {
                Text("No preference").tag(ProgrammingLanguageCatalog.noPreference)
                ForEach(filteredLanguages, id: \.self) { language in
                    Text(language).tag(language)
                }
                Text("Other…").tag(ProgrammingLanguageCatalog.other)
            }
            if !isOtherSelected {
                TextField("Search languages", text: $searchText)
                    .textFieldStyle(.roundedBorder)
                    .font(.caption)
            } else {
                TextField("Custom language", text: $customText)
                    .textFieldStyle(.roundedBorder)
                    .onChange(of: customText) { _, newValue in
                        selection = newValue
                    }
            }
        }
        .onAppear {
            if !selection.isEmpty && !ProgrammingLanguageCatalog.languages.contains(selection) {
                isOtherSelected = true
                customText = selection
            }
        }
    }

    private var filteredLanguages: [String] {
        guard !searchText.isEmpty else { return ProgrammingLanguageCatalog.languages }
        return ProgrammingLanguageCatalog.languages.filter { $0.localizedCaseInsensitiveContains(searchText) }
    }

    private var pickerBinding: Binding<String> {
        Binding(
            get: {
                if isOtherSelected { return ProgrammingLanguageCatalog.other }
                return selection
            },
            set: { newValue in
                if newValue == ProgrammingLanguageCatalog.other {
                    isOtherSelected = true
                    selection = customText
                } else {
                    isOtherSelected = false
                    selection = newValue
                }
            }
        )
    }
}
