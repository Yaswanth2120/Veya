import Foundation
import SwiftUI

/// Entry-point UI only, backed by the real `UserProfile` storage model and
/// `UserProfileRepository`, but with no smarts beyond a simple form.
struct PersonalProfileView: View {
    @StateObject private var viewModel = PersonalProfileViewModel()

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            BackToDashboardButton()

            Text("Personal Profile")
                .font(.largeTitle.bold())

            Form {
                TextField("Name", text: $viewModel.name)
                TextField("Headline", text: $viewModel.headline)
                TextField("Background", text: $viewModel.background, axis: .vertical)
                    .lineLimit(3...6)
                Picker("Default answer style", selection: $viewModel.defaultAnswerStyle) {
                    ForEach(AnswerStyle.allCases) { style in
                        Text(style.displayName).tag(style)
                    }
                }
                TextField("Default programming language", text: $viewModel.defaultProgrammingLanguage)
            }
            .formStyle(.grouped)

            Button("Save") {
                Task { await viewModel.save() }
            }
            .buttonStyle(.borderedProminent)

            Spacer()
        }
        .padding(28)
        .task { await viewModel.load() }
    }
}

@MainActor
final class PersonalProfileViewModel: ObservableObject {
    @Published var name = ""
    @Published var headline = ""
    @Published var background = ""
    @Published var defaultAnswerStyle: AnswerStyle = .concise
    @Published var defaultProgrammingLanguage = ""

    private var profileID = UUID()
    private let repository = UserProfileRepository()

    func load() async {
        guard let profile = try? await repository.fetch() else { return }
        profileID = profile.id
        name = profile.name
        headline = profile.headline
        background = profile.background
        defaultAnswerStyle = profile.defaultAnswerStyle
        defaultProgrammingLanguage = profile.defaultProgrammingLanguage
    }

    func save() async {
        let profile = UserProfile(
            id: profileID,
            name: name,
            headline: headline,
            background: background,
            defaultAnswerStyle: defaultAnswerStyle,
            defaultProgrammingLanguage: defaultProgrammingLanguage,
            updatedAt: Date()
        )
        try? await repository.save(profile)
    }
}
