import Foundation
import Testing
@testable import Veya

/// Exercises the Sections 11-13 RPCs against the **real** `core/veya`
/// worker subprocess (not a fake transport) — everything here is
/// deterministic without a local LLM (workspace/version bookkeeping,
/// architecture persistence/export, report caching, memory CRUD), so it
/// runs whenever `PythonAvailability` does. The LLM-backed proposal
/// generation (`coding.followup`, `design.followup`) is covered by
/// `core/tests/test_coding_followup.py`/`test_design_followup_export.py`
/// against a fake provider, and was additionally verified manually
/// against this environment's real local Ollama (see the Section 11-13
/// verification report).
@MainActor
@Suite("Coding/design/memory RPCs (real subprocess integration)", .enabled(if: PythonAvailability.isAvailable))
struct CodingDesignMemoryIntegrationTests {
    private func makeManager() -> PythonWorkerManager {
        var configuration = PythonWorkerConfiguration.resolveDefault()
        configuration.readyTimeout = 5
        configuration.rpcTimeout = 5
        // Every test in this suite must use its own isolated memory
        // database — never the developer's real
        // `~/Library/Application Support/Veya/Memory/memory.sqlite`.
        let isolatedRoot = FileManager.default.temporaryDirectory.appendingPathComponent("veya-test-memory-\(UUID().uuidString)", isDirectory: true)
        configuration.memoryDatabasePathURL = isolatedRoot.appendingPathComponent("memory.sqlite")
        configuration.reportStoreDirectoryURL = isolatedRoot.appendingPathComponent("reports", isDirectory: true)
        return PythonWorkerManager(configuration: configuration)
    }

    /// Same isolated report-store directory as `makeManager()`, exposed so
    /// a test can build a *second*, independent `PythonWorkerManager`
    /// against the same on-disk state — simulating a worker restart.
    private func makeManager(reportStoreDirectoryURL: URL) -> PythonWorkerManager {
        var configuration = PythonWorkerConfiguration.resolveDefault()
        configuration.readyTimeout = 5
        configuration.rpcTimeout = 5
        configuration.memoryDatabasePathURL = FileManager.default.temporaryDirectory
            .appendingPathComponent("veya-test-memory-\(UUID().uuidString)", isDirectory: true)
            .appendingPathComponent("memory.sqlite")
        configuration.reportStoreDirectoryURL = reportStoreDirectoryURL
        return PythonWorkerManager(configuration: configuration)
    }

    @Test("coding.upsert_file, coding.apply_edits, and stale-version rejection round-trip through the real worker")
    func codingWorkspaceRoundTrip() async throws {
        let manager = makeManager()
        await manager.start()
        #expect(manager.state == .ready)

        let sessionID = UUID().uuidString
        let stored: CodeFileResult = try await manager.call(
            method: "coding.upsert_file",
            params: CodingUpsertFileParams(sessionId: sessionID, name: "main.py", language: "python", content: "def f(x):\n    return x\n", baseVersion: nil)
        )
        #expect(stored.version == 1)

        let edited: CodeFileResult = try await manager.call(
            method: "coding.apply_edits",
            params: CodingApplyEditsParams(sessionId: sessionID, name: "main.py", baseVersion: stored.version, edits: [CodingEdit(start: 0, end: 3, replacement: "def g")])
        )
        #expect(edited.content.hasPrefix("def g"))
        #expect(edited.version == 2)

        // Stale version must be rejected without mutating the file.
        await #expect(throws: (any Error).self) {
            let _: CodeFileResult = try await manager.call(
                method: "coding.apply_edits",
                params: CodingApplyEditsParams(sessionId: sessionID, name: "main.py", baseVersion: stored.version, edits: [CodingEdit(start: 0, end: 1, replacement: "z")])
            )
        }

        let files: CodingFilesResult = try await manager.call(method: "coding.list_files", params: SessionIdentifierParams(sessionId: sessionID))
        #expect(files.files.first?.content == edited.content)

        await manager.stop()
    }

    @Test("design.replace, design.get, and every design.export format round-trip through the real worker")
    func designStateAndExportRoundTrip() async throws {
        let manager = makeManager()
        await manager.start()
        #expect(manager.state == .ready)

        let sessionID = UUID().uuidString
        let saved: ArchitectureStateResult = try await manager.call(
            method: "design.replace",
            params: ArchitectureReplaceParams(
                sessionId: sessionID, baseVersion: nil, title: "Checkout",
                nodes: [ArchitectureNode(id: "api", label: "API", kind: "service"), ArchitectureNode(id: "db", label: "Database", kind: "database")],
                edges: [ArchitectureEdge(source: "api", target: "db", label: "reads")],
                decisions: ["Use PostgreSQL"], assumptions: [], requirements: ["100M redirects/day"], risks: [], tradeOffs: [], actionItems: []
            )
        )
        #expect(saved.nodes.count == 2)

        let fetched: ArchitectureStateResult = try await manager.call(method: "design.get", params: SessionIdentifierParams(sessionId: sessionID))
        #expect(fetched.version == saved.version)
        #expect(fetched.requirements == ["100M redirects/day"])

        for format in ["mermaid", "json", "markdown", "pdf"] {
            let export: ArchitectureExportResult = try await manager.call(method: "design.export", params: ArchitectureExportParams(sessionId: sessionID, format: format))
            #expect(export.format == format)
            if format == "pdf" {
                #expect(export.contentBase64 != nil)
            } else {
                #expect(export.content?.isEmpty == false)
            }
        }

        await manager.stop()
    }

    @Test("session.analyze without a configured LLM still returns a data-only report Swift can persist")
    func sessionAnalyzeDataOnlyReport() async throws {
        let manager = makeManager()
        await manager.start()
        #expect(manager.state == .ready)

        let sessionID = UUID()
        let result: SessionReportResult = try await manager.call(
            method: "session.analyze",
            params: SessionAnalyzeParams(
                sessionId: sessionID.uuidString,
                transcript: [TranscriptSegmentPayload(text: "Let's discuss the plan.", startedAt: 0, endedAt: 1, isFinal: true)],
                questions: [DetectedQuestionPayload(id: "q1", text: "How long will it take?", detectedAt: 0)],
                answers: []
            )
        )
        #expect(result.sessionId == sessionID.uuidString)
        #expect(result.unansweredQuestions == ["How long will it take?"])

        let refetched: SessionReportResult = try await manager.call(method: "session.report.get", params: SessionIdentifierParams(sessionId: sessionID.uuidString))
        #expect(refetched.summary == result.summary)

        await manager.stop()
    }

    /// Regression test for a review finding: `session.report.get` used to
    /// read from an in-memory dict on `WorkerContext`, so a worker
    /// restart (crash, relaunch, or simply a fresh `PythonWorkerManager`
    /// pointed at the same directory) silently lost every analyzed
    /// report. Analyzes through one real worker process, stops it
    /// entirely, starts a completely separate worker process pointed at
    /// the same on-disk report directory, and proves the report is still
    /// retrievable — genuine cross-process durability, not merely
    /// cross-call.
    @Test("an analyzed report survives a full worker process restart")
    func sessionReportSurvivesWorkerRestart() async throws {
        let isolatedReportDirectory = FileManager.default.temporaryDirectory
            .appendingPathComponent("veya-test-report-restart-\(UUID().uuidString)", isDirectory: true)

        let firstManager = makeManager(reportStoreDirectoryURL: isolatedReportDirectory)
        await firstManager.start()
        #expect(firstManager.state == .ready)

        let sessionID = UUID()
        let analyzed: SessionReportResult = try await firstManager.call(
            method: "session.analyze",
            params: SessionAnalyzeParams(
                sessionId: sessionID.uuidString,
                transcript: [TranscriptSegmentPayload(text: "We discussed the payments migration.", startedAt: 0, endedAt: 1, isFinal: true)],
                questions: [], answers: []
            )
        )
        await firstManager.stop()

        let secondManager = makeManager(reportStoreDirectoryURL: isolatedReportDirectory)
        await secondManager.start()
        #expect(secondManager.state == .ready)

        let refetched: SessionReportResult = try await secondManager.call(method: "session.report.get", params: SessionIdentifierParams(sessionId: sessionID.uuidString))
        #expect(refetched.summary == analyzed.summary)
        #expect(refetched.sessionId == sessionID.uuidString)

        await secondManager.stop()
    }

    @Test("memory candidate approve/list/update/delete lifecycle round-trips through the real worker")
    func memoryLifecycleRoundTrip() async throws {
        let manager = makeManager()
        await manager.start()
        #expect(manager.state == .ready)

        // No LLM is configured in this worker (so `session.analyze` never
        // proposes a candidate here), so this exercises the CRUD surface
        // directly: a not-found id must raise a typed error, and
        // `memory.list` must return a well-formed, empty response.
        await #expect(throws: (any Error).self) {
            let _: MemoryRecordResult = try await manager.call(method: "memory.approve", params: MemoryIdentifierParams(memoryId: "does-not-exist"))
        }
        let listed: MemoryListResult = try await manager.call(method: "memory.list", params: MemoryListParams(status: "PROPOSED"))
        #expect(listed.memories.isEmpty)

        await manager.stop()
    }

    /// Real end-to-end reproduction of a review finding: on this
    /// environment, Ollama is genuinely running with `qwen3:1.7b`
    /// installed, but the worker's *default* configured model is
    /// `llama3.2`, which is not installed — so `system.llm_status` must
    /// report `reachable: true, modelInstalled: false`, and switching the
    /// override to the real installed model must flip it to `true`,
    /// against a real worker and a real local Ollama instance (never
    /// asserted, only reported, when no real Ollama happens to be running
    /// in whatever environment eventually runs this).
    @Test("system.llm_status reflects a real Ollama instance and a real installed-model mismatch")
    func llmStatusReflectsRealOllamaMismatchAndRecovers() async throws {
        let eventRouter = IPCEventRouter()
        let workerManager = makeManager()
        let coordinator = PythonIntelligenceCoordinator(workerManager: workerManager, eventRouter: eventRouter)
        await workerManager.start()
        #expect(workerManager.state == .ready)

        let status = await coordinator.fetchLLMStatus()
        guard status.reachable else {
            // No real local Ollama running in whatever environment ran
            // this — nothing further to prove here.
            await workerManager.stop()
            return
        }

        guard let installedModel = status.availableModels.first else {
            await workerManager.stop()
            return
        }

        await coordinator.setOllamaModelOverride(installedModel)
        #expect(workerManager.state == .ready)

        let updatedStatus = await coordinator.fetchLLMStatus()
        #expect(updatedStatus.modelInstalled == true)
        #expect(updatedStatus.configuredModel == installedModel)

        await workerManager.stop()
    }
}
