import Foundation
import Carbon.HIToolbox

/// Top-level navigation + lifecycle owner: which screen is showing, the
/// live session's `ConversationState`, the overlay window, and global
/// hotkeys. One instance, created by `AppDelegate`, shared via
/// `@EnvironmentObject`.
@MainActor
final class AppCoordinator: ObservableObject {
    enum Route: Hashable {
        case dashboard
        case createSession
        case liveSession
        case previousSessions
        case knowledgeBase
        case personalProfile
        case settings
        case presenterPrivacy
        case memory
    }

    @Published var route: Route = .dashboard
    @Published private(set) var currentSession: Session?
    @Published private(set) var conversationState: ConversationState?
    @Published var pendingPrivacyPrompt: LiveSessionPrivacyPrompt?
    @Published var lastActionError: String?

    let hotkeyManager = GlobalHotkeyManager()
    private(set) var overlayWindowController: OverlayWindowController?

    /// Persists for the app's lifetime (unlike the overlay, which only
    /// exists during a Live Session) so Settings → Presenter Privacy and
    /// Safe Share work whether or not a session is active. Injectable so
    /// tests can supply a `PresenterPrivacyManager` built from mocked
    /// dependencies.
    let presenterPrivacyManager: PresenterPrivacyManager

    /// Persists for the app's lifetime, same reasoning as
    /// `presenterPrivacyManager`. Injectable so tests can supply a
    /// coordinator built from a fake worker/transport.
    let pythonIntelligenceCoordinator: PythonIntelligenceCoordinator

    private let sessionRepository: SessionRepository
    private let conversationRepository: ConversationRepository

    init(
        sessionRepository: SessionRepository = SessionRepository(),
        conversationRepository: ConversationRepository = ConversationRepository(),
        presenterPrivacyManager: PresenterPrivacyManager = PresenterPrivacyManager(),
        pythonIntelligenceCoordinator: PythonIntelligenceCoordinator = PythonIntelligenceCoordinator()
    ) {
        self.sessionRepository = sessionRepository
        self.conversationRepository = conversationRepository
        self.presenterPrivacyManager = presenterPrivacyManager
        self.pythonIntelligenceCoordinator = pythonIntelligenceCoordinator
        presenterPrivacyManager.setOverlayWindowProvider { [weak self] in
            self?.overlayWindowController?.managedWindow
        }
        pythonIntelligenceCoordinator.launchWorkerInBackground()
    }

    func showDashboard() {
        route = .dashboard
    }

    /// Entry point Create Session should call. If Presenter Privacy is
    /// enabled and the active mode isn't ready (Safe Share not running, or
    /// Direct Private Overlay unverified), surfaces `pendingPrivacyPrompt`
    /// instead of starting immediately — the user is never blocked
    /// outright, just asked. See build prompt §5.22.
    ///
    /// `runTestBeforeSession == false` means the user has explicitly opted
    /// out of these pre-session checks — skip straight to starting,
    /// regardless of mode or current status.
    func requestStartLiveSession(for session: Session) {
        guard presenterPrivacyManager.preferences.enabled,
              presenterPrivacyManager.preferences.runTestBeforeSession
        else {
            startLiveSession(for: session)
            return
        }

        switch presenterPrivacyManager.preferences.selectedMode {
        case .normal:
            startLiveSession(for: session)
        case .safeShare:
            if presenterPrivacyManager.isSafeShareRunning {
                startLiveSession(for: session)
            } else {
                pendingPrivacyPrompt = .confirmStartSafeShare(session)
            }
        case .directPrivateOverlay:
            if presenterPrivacyManager.status == .verified {
                startLiveSession(for: session)
            } else {
                pendingPrivacyPrompt = .confirmUnverifiedDirectOverlay(session)
            }
        }
    }

    func dismissPrivacyPrompt() {
        pendingPrivacyPrompt = nil
    }

    func continueLiveSessionWithoutPrivacyAction(_ session: Session) {
        pendingPrivacyPrompt = nil
        startLiveSession(for: session)
    }

    func startSafeShareThenBeginSession(_ session: Session) {
        pendingPrivacyPrompt = nil
        Task {
            do {
                try await presenterPrivacyManager.startSafeShare()
            } catch {
                // The user explicitly asked for Safe Share here — silently
                // starting the session without it would leave them
                // believing they're protected when they're not. Surface
                // the failure, but still honor "never hard block": the
                // session starts either way.
                lastActionError = "Safe Share couldn't start. Please check Screen Recording permission and try again."
            }
            startLiveSession(for: session)
        }
    }

    func useSafeShareInsteadThenBeginSession(_ session: Session) {
        pendingPrivacyPrompt = nil
        Task {
            await presenterPrivacyManager.selectMode(.safeShare)
            requestStartLiveSession(for: session)
        }
    }

    func startSessionThenRunPrivacyTest(_ session: Session) {
        pendingPrivacyPrompt = nil
        startLiveSession(for: session)
        Task {
            _ = try? await presenterPrivacyManager.runCompatibilityTest()
        }
    }

    func startLiveSession(for session: Session) {
        var startedSession = session
        startedSession.status = .live
        currentSession = startedSession
        Task { try? await sessionRepository.update(startedSession) }

        let state = ConversationState(sessionID: session.id)
        conversationState = state

        let overlay = OverlayWindowController(conversationState: state, privacyManager: presenterPrivacyManager)
        overlayWindowController = overlay
        presenterPrivacyManager.overlayWindowDidBecomeAvailable()

        route = .liveSession
        overlay.show()

        Task {
            await pythonIntelligenceCoordinator.beginLiveSession(state: state, session: startedSession)
        }
    }

    func endLiveSession() {
        conversationState?.end()
        overlayWindowController?.hide()

        if var session = currentSession {
            session.status = .ended
            session.endedAt = Date()
            Task { try? await sessionRepository.update(session) }
            Task { await pythonIntelligenceCoordinator.endLiveSession(sessionID: session.id) }
            Task { await generateSessionReport(sessionID: session.id) }
        }

        currentSession = nil
        conversationState = nil
        overlayWindowController = nil
        route = .dashboard
    }

    /// Runs `session.analyze` against exactly this session's own transcript/
    /// questions/answers (Swift/GRDB remains their sole owner; only this
    /// narrowly scoped copy crosses the IPC boundary) and persists the
    /// resulting `SessionReport` — best-effort: a worker that isn't running
    /// simply leaves no report, it never blocks ending the session.
    private func generateSessionReport(sessionID: UUID) async {
        guard let transcript = try? await conversationRepository.transcript(sessionID: sessionID),
              let questions = try? await conversationRepository.questions(sessionID: sessionID),
              let answers = try? await conversationRepository.answers(sessionID: sessionID)
        else { return }

        let transcriptPayload = transcript.map { TranscriptSegmentPayload(text: $0.text, startedAt: $0.startedAt, endedAt: $0.endedAt, isFinal: $0.isFinal) }
        let questionPayload = questions.map { DetectedQuestionPayload(id: $0.id.uuidString, text: $0.text, detectedAt: $0.detectedAt.timeIntervalSince1970) }
        let answerPayload = answers.map { answer -> AnswerPayload in
            let sources = answer.sources.map { display -> AnswerSourceEventData in
                let parts = display.split(separator: ":", maxSplits: 1).map { $0.trimmingCharacters(in: .whitespaces) }
                let fileName = parts.first ?? display
                let excerpt = parts.count > 1 ? parts[1] : ""
                return AnswerSourceEventData(documentId: display, fileName: fileName, chunkId: display, excerpt: excerpt)
            }
            return AnswerPayload(questionId: answer.questionID.uuidString, question: answer.question, talkingPoints: answer.talkingPoints, sources: sources)
        }

        guard let result = try? await pythonIntelligenceCoordinator.analyzeSession(sessionID: sessionID, transcript: transcriptPayload, questions: questionPayload, answers: answerPayload) else { return }

        let report = SessionReport(
            id: UUID(), sessionID: sessionID, summary: result.summary, topics: result.topics, questions: result.questions,
            decisions: result.decisions, actionItems: result.actionItems, unansweredQuestions: result.unansweredQuestions,
            preparationGaps: result.preparationGaps, generatedAt: Date()
        )
        try? await conversationRepository.save(report)
    }

    /// Registers the two hotkeys from the build prompt: show/hide overlay,
    /// compact/expand overlay. Safe to call once at launch even before a
    /// live session exists — the closures just no-op until an overlay
    /// window controller is created.
    func registerHotkeys() {
        hotkeyManager.register(keyCode: UInt32(kVK_ANSI_O), modifiers: UInt32(cmdKey | shiftKey)) { [weak self] in
            self?.overlayWindowController?.toggleVisibility()
        }
        hotkeyManager.register(keyCode: UInt32(kVK_ANSI_C), modifiers: UInt32(cmdKey | shiftKey)) { [weak self] in
            self?.overlayWindowController?.toggleCompactMode()
        }
    }
}
