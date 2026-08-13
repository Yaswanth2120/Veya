import SwiftUI

/// Written local workspace for coding and system-design sessions.
struct CopilotWorkbenchView: View {
    @EnvironmentObject private var coordinator: AppCoordinator
    let session: Session

    var body: some View {
        Group {
            if session.sessionType == .codingPractice {
                CodingWorkbenchView(session: session)
            } else {
                DesignWorkbenchView(session: session)
            }
        }
        .padding(12)
        .background(.quaternary.opacity(0.25), in: RoundedRectangle(cornerRadius: 10))
    }
}

// MARK: - Coding workbench (Section 11)

private struct CodingWorkbenchView: View {
    @EnvironmentObject private var coordinator: AppCoordinator
    let session: Session

    @State private var source = ""
    @State private var fileName = "main.py"
    @State private var version: Int?
    @State private var status = ""
    @State private var request = ""
    @State private var operation = "followup"
    @State private var proposal: CodingProposalResult?
    @State private var isBusy = false

    private let operations = [("followup", "Follow-up"), ("debug", "Debug"), ("generate_tests", "Generate Tests"), ("explain", "Explain")]

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("CODING WORKBENCH").font(.caption.bold())
            TextField("File", text: $fileName)
            TextEditor(text: $source).font(.system(.body, design: .monospaced)).frame(minHeight: 160)
            HStack {
                Button("Save") { Task { await save() } }
                Button("Analyze") { Task { await analyze() } }
                Button("Run") { Task { await run() } }
            }

            Divider()
            Text("ASK THE COPILOT").font(.caption.bold())
            Picker("Operation", selection: $operation) {
                ForEach(operations, id: \.0) { Text($0.1).tag($0.0) }
            }.labelsHidden().pickerStyle(.segmented)
            TextField("e.g. \"Solve Longest Substring Without Repeating Characters\"", text: $request, axis: .vertical)
            Button(isBusy ? "Generating…" : "Generate") { Task { await generate() } }.disabled(request.isEmpty || isBusy)

            if let proposal {
                DiffPreviewView(proposal: proposal)
                HStack {
                    Button("Apply") { Task { await apply(proposal) } }.disabled(version == nil || proposal.edits.isEmpty)
                    Button("Reject") { self.proposal = nil; status = "Proposal rejected — workspace unchanged." }
                    Button("Regenerate") { Task { await generate() } }.disabled(isBusy)
                    if !proposal.tests.isEmpty {
                        Button("Save & Run Tests") { Task { await saveAndRunTests(proposal.tests) } }
                    }
                }
            }

            Text(status).font(.caption).foregroundStyle(.secondary).textSelection(.enabled)
        }
        .task { await load() }
    }

    private func load() async {
        do {
            if let file = try await coordinator.pythonIntelligenceCoordinator.loadCodeFiles(sessionID: session.id).first {
                fileName = file.name; source = file.content; version = file.version
            }
        } catch { status = "Local workbench unavailable." }
    }

    private func save() async {
        do {
            let file = try await coordinator.pythonIntelligenceCoordinator.saveCodeFile(sessionID: session.id, name: fileName, language: "python", content: source, baseVersion: version)
            version = file.version
            status = "Saved version \(file.version)."
        } catch { status = "Save conflict or local worker unavailable." }
    }

    private func analyze() async {
        await save(); guard version != nil else { return }
        do {
            let result = try await coordinator.pythonIntelligenceCoordinator.analyzeCodeFile(sessionID: session.id, name: fileName)
            status = result.syntaxOk ? "Syntax OK · complexity \(result.complexity) · functions \(result.functionCount ?? 0)" : "Syntax error."
        } catch { status = "Analysis unavailable." }
    }

    private func run() async {
        await save(); guard version != nil else { return }
        do {
            let result = try await coordinator.pythonIntelligenceCoordinator.runCodeFile(sessionID: session.id, name: fileName)
            status = result.timedOut ? result.stderr : "exit \(result.exitCode): \(result.stdout)\(result.stderr)"
        } catch { status = "Execution disabled or unavailable." }
    }

    private func generate() async {
        await save()
        isBusy = true
        defer { isBusy = false }
        do {
            proposal = try await coordinator.pythonIntelligenceCoordinator.codingAssist(sessionID: session.id, name: fileName, operation: operation, request: request)
            status = "Proposal ready — review the diff below before applying."
        } catch {
            proposal = nil
            status = "Local coding intelligence is unavailable."
        }
    }

    private func apply(_ proposal: CodingProposalResult) async {
        guard let currentVersion = version else { return }
        do {
            let file = try await coordinator.pythonIntelligenceCoordinator.applyCodeEdits(sessionID: session.id, name: fileName, baseVersion: currentVersion, edits: proposal.edits)
            source = file.content
            version = file.version
            self.proposal = nil
            status = "Applied — now version \(file.version)."
        } catch {
            status = "Apply failed: the file changed since this proposal was generated (stale version). Nothing was overwritten."
        }
    }

    private func saveAndRunTests(_ tests: String) async {
        let testFileName = "test_" + fileName
        do {
            _ = try await coordinator.pythonIntelligenceCoordinator.saveCodeFile(sessionID: session.id, name: testFileName, language: "python", content: tests, baseVersion: nil)
            let result = try await coordinator.pythonIntelligenceCoordinator.runCodeFile(sessionID: session.id, name: testFileName)
            status = result.timedOut ? "Tests timed out." : (result.exitCode == 0 ? "Tests passed.\n\(result.stdout)" : "Tests failed (exit \(result.exitCode)).\n\(result.stdout)\(result.stderr)")
        } catch { status = "Saving/running tests is unavailable (execution may be disabled)." }
    }
}

private struct DiffPreviewView: View {
    let proposal: CodingProposalResult

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("DIFF PREVIEW").font(.caption.bold())
            if proposal.edits.isEmpty {
                Text("No edits proposed.").font(.caption).foregroundStyle(.secondary)
            } else {
                ForEach(Array(proposal.edits.enumerated()), id: \.offset) { _, edit in
                    Text("[\(edit.start)–\(edit.end)] → \"\(edit.replacement)\"")
                        .font(.system(.caption, design: .monospaced))
                        .padding(4)
                        .background(Color.yellow.opacity(0.15), in: RoundedRectangle(cornerRadius: 4))
                }
            }
            if !proposal.explanation.isEmpty { Text(proposal.explanation).font(.caption) }
            if !proposal.complexity.isEmpty { Text("Complexity: \(proposal.complexity)").font(.caption2).foregroundStyle(.secondary) }
        }
    }
}

// MARK: - System design workbench (Section 12)

private struct DesignWorkbenchView: View {
    @EnvironmentObject private var coordinator: AppCoordinator
    let session: Session

    @State private var architecture: ArchitectureStateResult?
    @State private var request = ""
    @State private var status = ""
    @State private var isBusy = false
    @State private var selectedExportFormat = "markdown"
    @State private var exportedContent: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("SYSTEM DESIGN WORKBENCH").font(.caption.bold())
            Text("Architecture state is authoritative; Mermaid/JSON/Markdown/PDF are derived exports.").font(.caption2).foregroundStyle(.secondary)

            if let architecture {
                Text(architecture.title).font(.headline)
                ArchitectureGraphView(state: architecture) { updated in
                    Task { await persistLayout(updated) }
                }
                DisclosureGroup("Details") {
                    VStack(alignment: .leading, spacing: 4) {
                        labeledList("Requirements", architecture.requirements)
                        labeledList("Assumptions", architecture.assumptions)
                        labeledList("Decisions", architecture.decisions)
                        labeledList("Trade-offs", architecture.tradeOffs)
                        labeledList("Risks", architecture.risks)
                        labeledList("Action Items", architecture.actionItems)
                    }
                }

                TextField("e.g. \"100M redirects/day\" or \"How do we handle hot URLs?\"", text: $request, axis: .vertical)
                Button(isBusy ? "Evolving…" : "Send Follow-up") { Task { await followup() } }.disabled(request.isEmpty || isBusy)

                HStack {
                    Picker("Format", selection: $selectedExportFormat) {
                        Text("Mermaid").tag("mermaid"); Text("JSON").tag("json"); Text("Markdown").tag("markdown"); Text("PDF").tag("pdf")
                    }.labelsHidden()
                    Button("Export") { Task { await export() } }
                }
                if let exportedContent {
                    ScrollView { Text(exportedContent).font(.system(.caption2, design: .monospaced)).textSelection(.enabled) }.frame(maxHeight: 160)
                }
            } else {
                ProgressView()
            }
            Text(status).font(.caption).foregroundStyle(.secondary)
        }
        .task { await load() }
    }

    @ViewBuilder
    private func labeledList(_ title: String, _ items: [String]) -> some View {
        if !items.isEmpty {
            Text(title).font(.caption.bold())
            ForEach(items, id: \.self) { Text("• \($0)").font(.caption) }
        }
    }

    private func load() async {
        do { architecture = try await coordinator.pythonIntelligenceCoordinator.loadArchitecture(sessionID: session.id) }
        catch { status = "Local workbench unavailable." }
    }

    private func followup() async {
        isBusy = true
        defer { isBusy = false }
        do {
            architecture = try await coordinator.pythonIntelligenceCoordinator.designFollowup(sessionID: session.id, request: request)
            status = "Design updated."
            request = ""
        } catch { status = "Local design intelligence is unavailable." }
    }

    private func persistLayout(_ updated: ArchitectureStateResult) async {
        do { architecture = try await coordinator.pythonIntelligenceCoordinator.saveArchitecture(sessionID: session.id, state: updated) }
        catch { status = "Could not persist layout." }
    }

    private func export() async {
        do {
            let result = try await coordinator.pythonIntelligenceCoordinator.exportArchitecture(sessionID: session.id, format: selectedExportFormat)
            if let content = result.content { exportedContent = content }
            else if let base64 = result.contentBase64 { exportedContent = try saveBinaryExport(base64: base64, format: result.format) }
        } catch { status = "Export unavailable." }
    }

    private func saveBinaryExport(base64: String, format: String) throws -> String {
        guard let data = Data(base64Encoded: base64) else { return "Export failed to decode." }
        let directory = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
        let url = directory.appendingPathComponent("\(session.title.isEmpty ? "architecture" : session.title)-export.\(format)")
        try data.write(to: url)
        return "Saved to \(url.path)"
    }
}

/// A minimal but real editable node/edge graph, backed entirely by the
/// authoritative `ArchitectureStateResult` — add/rename/delete propagate
/// back through `onChange` (which the caller persists via
/// `design.replace`), never mutated only in local view state.
private struct ArchitectureGraphView: View {
    let state: ArchitectureStateResult
    let onChange: (ArchitectureStateResult) -> Void

    @State private var newNodeLabel = ""
    @State private var newEdgeSource = ""
    @State private var newEdgeTarget = ""

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            ForEach(state.nodes) { node in
                HStack {
                    Text("● \(node.label)").font(.caption)
                    Text("[\(node.kind)]").font(.caption2).foregroundStyle(.secondary)
                    Spacer()
                    Button(role: .destructive) { deleteNode(node.id) } label: { Image(systemName: "trash") }.buttonStyle(.plain)
                }
            }
            ForEach(Array(state.edges.enumerated()), id: \.offset) { index, edge in
                HStack {
                    Text("\(edge.source) → \(edge.target)\(edge.label.isEmpty ? "" : " (\(edge.label))")").font(.caption2)
                    Spacer()
                    Button(role: .destructive) { deleteEdge(at: index) } label: { Image(systemName: "trash") }.buttonStyle(.plain)
                }
            }
            HStack {
                TextField("New node label", text: $newNodeLabel)
                Button("Add Node") { addNode() }.disabled(newNodeLabel.isEmpty)
            }
            HStack {
                TextField("source id", text: $newEdgeSource).frame(width: 90)
                TextField("target id", text: $newEdgeTarget).frame(width: 90)
                Button("Add Edge") { addEdge() }.disabled(newEdgeSource.isEmpty || newEdgeTarget.isEmpty)
            }
        }
    }

    private func addNode() {
        var updated = state
        let id = newNodeLabel.lowercased().replacingOccurrences(of: " ", with: "_")
        updated.nodes.append(ArchitectureNode(id: id, label: newNodeLabel, kind: "service"))
        newNodeLabel = ""
        onChange(updated)
    }

    private func deleteNode(_ id: String) {
        var updated = state
        updated.nodes.removeAll { $0.id == id }
        updated.edges.removeAll { $0.source == id || $0.target == id }
        onChange(updated)
    }

    private func addEdge() {
        var updated = state
        updated.edges.append(ArchitectureEdge(source: newEdgeSource, target: newEdgeTarget, label: ""))
        newEdgeSource = ""; newEdgeTarget = ""
        onChange(updated)
    }

    private func deleteEdge(at index: Int) {
        var updated = state
        updated.edges.remove(at: index)
        onChange(updated)
    }
}
