import Foundation
import SwiftUI

struct PreviousSessionsView: View {
    @EnvironmentObject private var coordinator: AppCoordinator
    @StateObject private var viewModel = PreviousSessionsViewModel()

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack {
                Button {
                    coordinator.showDashboard()
                } label: {
                    Label("Dashboard", systemImage: "chevron.left")
                }
                .buttonStyle(.plain)
                Spacer()
            }

            Text("Previous Sessions")
                .font(.largeTitle.bold())

            if viewModel.sessions.isEmpty {
                Text("No sessions yet.")
                    .foregroundStyle(.secondary)
            } else {
                ScrollView {
                    VStack(spacing: 8) {
                        ForEach(viewModel.sessions) { session in
                            SessionDetailDisclosure(session: session)
                        }
                    }
                }
            }
            Spacer()
        }
        .padding(28)
        .task { await viewModel.load() }
    }
}

private struct SessionDetailDisclosure: View {
    let session: Session
    @State private var expanded = false
    @StateObject private var detail = SessionDetailViewModel()

    var body: some View {
        DisclosureGroup(isExpanded: $expanded) {
            VStack(alignment: .leading, spacing: 8) {
                if detail.transcript.isEmpty {
                    Text("No transcript recorded.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                } else {
                    ForEach(detail.transcript) { segment in
                        Text(segment.text)
                            .font(.caption)
                    }
                }
            }
            .padding(.top, 6)
            .padding(.leading, 8)
        } label: {
            SessionRow(session: session)
        }
        .onChange(of: expanded) { _, isExpanded in
            if isExpanded {
                Task { await detail.load(sessionID: session.id) }
            }
        }
    }
}

@MainActor
private final class SessionDetailViewModel: ObservableObject {
    @Published private(set) var transcript: [TranscriptSegment] = []
    private let repository = ConversationRepository()

    func load(sessionID: UUID) async {
        transcript = (try? await repository.transcript(sessionID: sessionID)) ?? []
    }
}

@MainActor
final class PreviousSessionsViewModel: ObservableObject {
    @Published private(set) var sessions: [Session] = []
    private let repository = SessionRepository()

    func load() async {
        sessions = (try? await repository.fetchAll()) ?? []
    }
}
