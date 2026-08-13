import SwiftUI

struct RootView: View {
    @EnvironmentObject private var coordinator: AppCoordinator

    /// The sidebar is hidden during a Live Session — that view is meant
    /// to be immersive/full-width, and navigating away mid-session via the
    /// sidebar would be a way to silently abandon it without going
    /// through `endLiveSession()`.
    private var showsSidebar: Bool {
        coordinator.route != .liveSession
    }

    var body: some View {
        Group {
            if showsSidebar {
                HStack(spacing: 0) {
                    SidebarView()
                    Divider()
                    content
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                }
            } else {
                content
            }
        }
        .confirmationDialog(
            privacyPromptTitle,
            isPresented: privacyPromptBinding,
            titleVisibility: .visible,
            presenting: coordinator.pendingPrivacyPrompt
        ) { prompt in
            privacyPromptActions(for: prompt)
        } message: { prompt in
            Text(privacyPromptMessage(for: prompt))
        }
        .alert(
            "Presenter Privacy",
            isPresented: lastActionErrorBinding,
            presenting: coordinator.lastActionError
        ) { _ in
            Button("OK") { coordinator.lastActionError = nil }
        } message: { message in
            Text(message)
        }
    }

    @ViewBuilder
    private var content: some View {
        switch coordinator.route {
        case .dashboard:
            DashboardView()
        case .createSession:
            CreateSessionView(pythonIntelligenceCoordinator: coordinator.pythonIntelligenceCoordinator)
        case .liveSession:
            if let state = coordinator.conversationState {
                LiveSessionView(
                    conversationState: state,
                    pythonIntelligenceCoordinator: coordinator.pythonIntelligenceCoordinator
                )
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
        case .presenterPrivacy:
            PresenterPrivacySettingsView(privacyManager: coordinator.presenterPrivacyManager)
        case .memory:
            MemoryReviewView()
        case .localAIStatus:
            LocalAIStatusView()
        }
    }

    private var lastActionErrorBinding: Binding<Bool> {
        Binding(
            get: { coordinator.lastActionError != nil },
            set: { isPresented in
                if !isPresented { coordinator.lastActionError = nil }
            }
        )
    }

    private var privacyPromptBinding: Binding<Bool> {
        Binding(
            get: { coordinator.pendingPrivacyPrompt != nil },
            set: { isPresented in
                if !isPresented { coordinator.dismissPrivacyPrompt() }
            }
        )
    }

    private var privacyPromptTitle: String {
        switch coordinator.pendingPrivacyPrompt {
        case .confirmStartSafeShare: return "Start Safe Share before session?"
        case .confirmUnverifiedDirectOverlay: return "Private Overlay isn't verified"
        case nil: return ""
        }
    }

    private func privacyPromptMessage(for prompt: LiveSessionPrivacyPrompt) -> String {
        switch prompt {
        case .confirmStartSafeShare:
            return "Presenter Privacy is set to Safe Share, but Safe Share isn't running yet."
        case .confirmUnverifiedDirectOverlay:
            return "Private Overlay has not been verified for this configuration."
        }
    }

    @ViewBuilder
    private func privacyPromptActions(for prompt: LiveSessionPrivacyPrompt) -> some View {
        switch prompt {
        case .confirmStartSafeShare(let session):
            Button("Start Safe Share") { coordinator.startSafeShareThenBeginSession(session) }
            Button("Continue Without", role: .cancel) { coordinator.continueLiveSessionWithoutPrivacyAction(session) }
        case .confirmUnverifiedDirectOverlay(let session):
            Button("Run Test") { coordinator.startSessionThenRunPrivacyTest(session) }
            Button("Use Safe Share Instead") { coordinator.useSafeShareInsteadThenBeginSession(session) }
            Button("Continue", role: .cancel) { coordinator.continueLiveSessionWithoutPrivacyAction(session) }
        }
    }
}
