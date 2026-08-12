import SwiftUI

struct RootView: View {
    @EnvironmentObject private var coordinator: AppCoordinator

    var body: some View {
        Group {
            switch coordinator.route {
            case .dashboard:
                DashboardView()
            case .createSession:
                CreateSessionView()
            case .liveSession:
                if let state = coordinator.conversationState {
                    LiveSessionView(conversationState: state)
                } else {
                    DashboardView()
                }
            case .previousSessions:
                PreviousSessionsView()
            case .knowledgeBase:
                KnowledgeBaseView()
            case .personalProfile:
                PersonalProfileView()
            case .settings:
                SettingsView()
            }
        }
    }
}
