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

    @State private var files: [CodeFileResult] = []
    @State private var selectedFileName: String?
    @State private var source = ""
    @State private var version: Int?
    @State private var language = "Python"
    @State private var status = ""
    @State private var request = ""
    @State private var proposal: CodingProposalResult?
    @State private var isBusy = false
    @State private var consoleOutput: String?
    @State private var isNewFileSheetPresented = false
    @State private var newFileName = ""
    @State private var renamingFile: String?
    @State private var renameText = ""

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("CODING WORKBENCH").font(.caption.bold())

            HStack(alignment: .top, spacing: 12) {
                fileSidebar
                editorAndActions
            }

            Text(status).font(.caption).foregroundStyle(.secondary).textSelection(.enabled)
        }
        .task { await load() }
        .sheet(isPresented: $isNewFileSheetPresented) {
            newFileSheet
        }
    }

    // MARK: File sidebar

    private var fileSidebar: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text("FILES").font(.caption.bold()).foregroundStyle(.secondary)
                Spacer()
                Button { newFileName = ""; isNewFileSheetPresented = true } label: { Image(systemName: "plus") }
                    .buttonStyle(.plain)
                    .help("New file")
            }
            if files.isEmpty {
                Text("No files yet.").font(.caption2).foregroundStyle(.secondary)
            }
            ForEach(files, id: \.name) { file in
                HStack {
                    if renamingFile == file.name {
                        TextField("Name", text: $renameText, onCommit: { Task { await commitRename(from: file.name) } })
                            .font(.caption)
                    } else {
                        Text(file.name)
                            .font(.caption)
                            .fontWeight(selectedFileName == file.name ? .bold : .regular)
                            .onTapGesture { Task { await select(file) } }
                    }
                    Spacer()
                    Menu {
                        Button("Rename") { renamingFile = file.name; renameText = file.name }
                        Button("Delete", role: .destructive) { Task { await delete(file) } }
                    } label: {
                        Image(systemName: "ellipsis")
                    }
                    .buttonStyle(.plain)
                    .menuIndicator(.hidden)
                    .frame(width: 16)
                }
                .padding(.vertical, 2)
                .padding(.horizontal, 4)
                .background(selectedFileName == file.name ? Color.accentColor.opacity(0.12) : .clear, in: RoundedRectangle(cornerRadius: 4))
            }
        }
        .frame(width: 140, alignment: .topLeading)
    }

    private var newFileSheet: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("New File").font(.headline)
            TextField("File name (e.g. solution.py)", text: $newFileName)
            HStack {
                Spacer()
                Button("Cancel") { isNewFileSheetPresented = false }
                Button("Create") { Task { await createFile() } }.disabled(newFileName.isEmpty)
                    .buttonStyle(.borderedProminent)
            }
        }
        .padding(20)
        .frame(width: 320)
    }

    // MARK: Editor + copilot actions

    private var editorAndActions: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Picker("Language", selection: $language) {
                    ForEach(ProgrammingLanguageCatalog.languages, id: \.self) { Text($0).tag($0) }
                }
                .frame(width: 160)
                Spacer()
                Button("Save") { Task { await save() } }.disabled(selectedFileName == nil)
                Button("Analyze") { Task { await analyze() } }.disabled(selectedFileName == nil)
            }
            TextEditor(text: $source).font(.system(.body, design: .monospaced)).frame(minHeight: 160)

            executionSection

            Divider()
            Text("ASK THE COPILOT").font(.caption.bold())
            TextField("e.g. \"Solve Longest Substring Without Repeating Characters\"", text: $request, axis: .vertical)
            HStack {
                Button(isBusy ? "Generating…" : "Generate") { Task { await runOperation("followup") } }.disabled(request.isEmpty || isBusy || selectedFileName == nil)
                Button("Debug") { Task { await runOperation("debug") } }.disabled(request.isEmpty || isBusy || selectedFileName == nil)
                Button("Generate Tests") { Task { await runOperation("generate_tests") } }.disabled(request.isEmpty || isBusy || selectedFileName == nil)
                Button("Explain") { Task { await runOperation("explain") } }.disabled(request.isEmpty || isBusy || selectedFileName == nil)
            }

            if let proposal {
                DiffPreviewView(proposal: proposal, originalSource: source)
                HStack {
                    Button("Apply") { Task { await apply(proposal) } }.disabled(version == nil || proposal.edits.isEmpty)
                        .buttonStyle(.borderedProminent)
                    Button("Reject") { self.proposal = nil; status = "Proposal rejected — workspace unchanged." }
                    Button("Regenerate") { Task { await runOperation(lastOperation) } }.disabled(isBusy)
                    if !proposal.tests.isEmpty {
                        Button("Save & Run Tests") { Task { await saveAndRunTests(proposal.tests) } }
                    }
                }
            }
        }
    }

    @State private var lastOperation = "followup"

    private var executionSection: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Button("Run") { Task { await run() } }
                    .disabled(selectedFileName == nil || !session.codeExecutionConsent)
                if !session.codeExecutionConsent {
                    Text("Execution consent wasn't granted for this session (Create Session > Coding Practice).")
                        .font(.caption2).foregroundStyle(.orange)
                }
            }
            if let consoleOutput {
                Text("CONSOLE").font(.caption2.bold()).foregroundStyle(.secondary)
                ScrollView {
                    Text(consoleOutput)
                        .font(.system(.caption, design: .monospaced))
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .textSelection(.enabled)
                }
                .frame(maxHeight: 120)
                .padding(6)
                .background(Color.black.opacity(0.85), in: RoundedRectangle(cornerRadius: 6))
                .foregroundStyle(.white)
            }
        }
    }

    // MARK: Actions

    private func load() async {
        do {
            files = try await coordinator.pythonIntelligenceCoordinator.loadCodeFiles(sessionID: session.id)
            if let first = files.first { await select(first) }
        } catch { status = "Local workbench unavailable." }
    }

    private func select(_ file: CodeFileResult) async {
        selectedFileName = file.name
        source = file.content
        version = file.version
        language = ProgrammingLanguageCatalog.languages.first { $0.lowercased() == file.language.lowercased() } ?? file.language.capitalized
        proposal = nil
        consoleOutput = nil
    }

    private func createFile() async {
        do {
            let file = try await coordinator.pythonIntelligenceCoordinator.saveCodeFile(sessionID: session.id, name: newFileName, language: language.lowercased(), content: "", baseVersion: nil)
            files.append(file)
            await select(file)
            isNewFileSheetPresented = false
        } catch { status = "Could not create file." }
    }

    private func delete(_ file: CodeFileResult) async {
        do {
            try await coordinator.pythonIntelligenceCoordinator.deleteCodeFile(sessionID: session.id, name: file.name)
            files.removeAll { $0.name == file.name }
            if selectedFileName == file.name {
                selectedFileName = nil
                source = ""
                version = nil
            }
        } catch { status = "Could not delete file." }
    }

    private func commitRename(from oldName: String) async {
        defer { renamingFile = nil }
        guard !renameText.isEmpty, renameText != oldName else { return }
        do {
            let renamed = try await coordinator.pythonIntelligenceCoordinator.renameCodeFile(sessionID: session.id, name: oldName, newName: renameText)
            if let index = files.firstIndex(where: { $0.name == oldName }) { files[index] = renamed }
            if selectedFileName == oldName { selectedFileName = renamed.name }
        } catch { status = "Rename failed — a file with that name may already exist." }
    }

    private func save() async {
        guard let selectedFileName else { return }
        do {
            let file = try await coordinator.pythonIntelligenceCoordinator.saveCodeFile(sessionID: session.id, name: selectedFileName, language: language.lowercased(), content: source, baseVersion: version)
            version = file.version
            if let index = files.firstIndex(where: { $0.name == selectedFileName }) { files[index] = file }
            status = "Saved version \(file.version)."
        } catch { status = "Save conflict or local worker unavailable." }
    }

    private func analyze() async {
        await save(); guard let selectedFileName, version != nil else { return }
        do {
            let result = try await coordinator.pythonIntelligenceCoordinator.analyzeCodeFile(sessionID: session.id, name: selectedFileName)
            status = result.syntaxOk ? "Syntax OK · complexity \(result.complexity) · functions \(result.functionCount ?? 0)" : "Syntax error."
        } catch { status = "Analysis unavailable." }
    }

    private func run() async {
        guard session.codeExecutionConsent else {
            status = "Execution consent wasn't granted for this session."
            return
        }
        await save(); guard let selectedFileName, version != nil else { return }
        do {
            let result = try await coordinator.pythonIntelligenceCoordinator.runCodeFile(sessionID: session.id, name: selectedFileName)
            consoleOutput = result.timedOut ? "Execution timed out." : "exit \(result.exitCode)\n\(result.stdout)\(result.stderr)"
            status = "Run complete."
        } catch { status = "Execution disabled or unavailable." }
    }

    private func runOperation(_ operation: String) async {
        await save()
        guard let selectedFileName else { return }
        lastOperation = operation
        isBusy = true
        defer { isBusy = false }
        do {
            proposal = try await coordinator.pythonIntelligenceCoordinator.codingAssist(sessionID: session.id, name: selectedFileName, operation: operation, request: request)
            status = "Proposal ready — review the diff below before applying."
        } catch {
            proposal = nil
            status = "Local coding intelligence is unavailable."
        }
    }

    private func apply(_ proposal: CodingProposalResult) async {
        guard let selectedFileName, let currentVersion = version else { return }
        do {
            let file = try await coordinator.pythonIntelligenceCoordinator.applyCodeEdits(sessionID: session.id, name: selectedFileName, baseVersion: currentVersion, edits: proposal.edits)
            source = file.content
            version = file.version
            if let index = files.firstIndex(where: { $0.name == selectedFileName }) { files[index] = file }
            self.proposal = nil
            status = "Applied — now version \(file.version)."
        } catch {
            status = "Apply failed: the file changed since this proposal was generated (stale version). Nothing was overwritten."
        }
    }

    private func saveAndRunTests(_ tests: String) async {
        guard let selectedFileName else { return }
        guard session.codeExecutionConsent else {
            status = "Execution consent wasn't granted for this session."
            return
        }
        let testFileName = "test_" + selectedFileName
        do {
            _ = try await coordinator.pythonIntelligenceCoordinator.saveCodeFile(sessionID: session.id, name: testFileName, language: language.lowercased(), content: tests, baseVersion: nil)
            let result = try await coordinator.pythonIntelligenceCoordinator.runCodeFile(sessionID: session.id, name: testFileName)
            consoleOutput = result.timedOut ? "Tests timed out." : "exit \(result.exitCode)\n\(result.stdout)\(result.stderr)"
            status = result.timedOut ? "Tests timed out." : (result.exitCode == 0 ? "Tests passed." : "Tests failed (exit \(result.exitCode)).")
        } catch { status = "Saving/running tests is unavailable (execution may be disabled)." }
    }
}

/// Line-based old/new diff, not raw character offsets — each proposed
/// edit is rendered as the text it replaces (struck through, red) versus
/// its replacement (green), computed against the source the proposal was
/// generated from.
private struct DiffPreviewView: View {
    let proposal: CodingProposalResult
    let originalSource: String

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("DIFF PREVIEW").font(.caption.bold())
            if proposal.edits.isEmpty {
                Text("No edits proposed.").font(.caption).foregroundStyle(.secondary)
            } else {
                ForEach(Array(proposal.edits.enumerated()), id: \.offset) { _, edit in
                    editRow(edit)
                }
            }
            if !proposal.explanation.isEmpty { Text(proposal.explanation).font(.caption) }
            if !proposal.complexity.isEmpty { Text("Complexity: \(proposal.complexity)").font(.caption2).foregroundStyle(.secondary) }
        }
    }

    private func editRow(_ edit: CodingEdit) -> some View {
        let bounded = boundedRange(edit)
        let oldText = bounded.map { String(originalSource[$0]) } ?? ""
        return VStack(alignment: .leading, spacing: 2) {
            if !oldText.isEmpty {
                Text("- \(oldText)")
                    .font(.system(.caption, design: .monospaced))
                    .foregroundStyle(.red)
                    .strikethrough()
                    .padding(4)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(Color.red.opacity(0.1), in: RoundedRectangle(cornerRadius: 4))
            }
            if !edit.replacement.isEmpty {
                Text("+ \(edit.replacement)")
                    .font(.system(.caption, design: .monospaced))
                    .foregroundStyle(.green)
                    .padding(4)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(Color.green.opacity(0.1), in: RoundedRectangle(cornerRadius: 4))
            }
        }
    }

    private func boundedRange(_ edit: CodingEdit) -> Range<String.Index>? {
        guard edit.start >= 0, edit.end >= edit.start, edit.end <= originalSource.utf16.count else { return nil }
        guard let start = originalSource.utf16.index(originalSource.utf16.startIndex, offsetBy: edit.start, limitedBy: originalSource.utf16.endIndex),
              let end = originalSource.utf16.index(originalSource.utf16.startIndex, offsetBy: edit.end, limitedBy: originalSource.utf16.endIndex),
              let startIndex = String.Index(start, within: originalSource),
              let endIndex = String.Index(end, within: originalSource)
        else { return nil }
        return startIndex..<endIndex
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
    @State private var requestHistory: [String] = []

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

                if !requestHistory.isEmpty {
                    DisclosureGroup("Follow-up history (\(requestHistory.count))") {
                        VStack(alignment: .leading, spacing: 2) {
                            ForEach(Array(requestHistory.enumerated()), id: \.offset) { index, item in
                                Text("\(index + 1). \(item)").font(.caption2)
                            }
                        }
                    }
                    .font(.caption.bold())
                }

                TextField("e.g. \"100M redirects/day\" or \"How do we handle hot URLs?\"", text: $request, axis: .vertical)
                Button(isBusy ? "Evolving…" : "Send Follow-up") { Task { await followup() } }.disabled(request.isEmpty || isBusy)

                Divider()
                Text("EXPORT").font(.caption.bold()).foregroundStyle(.secondary)
                HStack {
                    Button("Mermaid") { Task { await export(format: "mermaid") } }
                    Button("JSON") { Task { await export(format: "json") } }
                    Button("Markdown") { Task { await export(format: "markdown") } }
                    Button("PDF") { Task { await export(format: "pdf") } }
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
            requestHistory.append(request)
            status = "Design updated."
            request = ""
        } catch { status = "Local design intelligence is unavailable." }
    }

    private func persistLayout(_ updated: ArchitectureStateResult) async {
        do { architecture = try await coordinator.pythonIntelligenceCoordinator.saveArchitecture(sessionID: session.id, state: updated) }
        catch { status = "Could not persist layout." }
    }

    private func export(format: String) async {
        do {
            let result = try await coordinator.pythonIntelligenceCoordinator.exportArchitecture(sessionID: session.id, format: format)
            if let content = result.content {
                status = try saveTextExport(content: content, format: format)
            } else if let base64 = result.contentBase64 {
                status = try saveBinaryExport(base64: base64, format: result.format)
            }
        } catch { status = "Export unavailable." }
    }

    private func saveTextExport(content: String, format: String) throws -> String {
        let directory = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
        let ext = format == "mermaid" ? "mmd" : format == "markdown" ? "md" : format
        let url = directory.appendingPathComponent("\(session.title.isEmpty ? "architecture" : session.title)-export.\(ext)")
        try content.write(to: url, atomically: true, encoding: .utf8)
        return "Exported to \(url.path)"
    }

    private func saveBinaryExport(base64: String, format: String) throws -> String {
        guard let data = Data(base64Encoded: base64) else { return "Export failed to decode." }
        let directory = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
        let url = directory.appendingPathComponent("\(session.title.isEmpty ? "architecture" : session.title)-export.\(format)")
        try data.write(to: url)
        return "Exported to \(url.path)"
    }
}

/// An editable node/edge graph on a real drag-to-reposition canvas —
/// backed entirely by the authoritative `ArchitectureStateResult`
/// (including each node's `x`/`y`), so dragging, adding, renaming, and
/// deleting all propagate back through `onChange` (which the caller
/// persists via `design.replace`), never mutated only in local view
/// state.
private struct ArchitectureGraphView: View {
    let state: ArchitectureStateResult
    let onChange: (ArchitectureStateResult) -> Void

    @State private var newNodeLabel = ""
    @State private var newEdgeSource = ""
    @State private var newEdgeTarget = ""
    @State private var newEdgeLabel = ""
    @State private var editingNodeID: String?
    @State private var editingLabel = ""
    /// Transient, render-only positions while a drag is in progress —
    /// `design.replace` is only called once, on drag end, not on every
    /// frame of movement (which would flood the worker with RPCs and race
    /// on `base_version`).
    @State private var liveDragPositions: [String: CGPoint] = [:]

    private let canvasSize = CGSize(width: 560, height: 260)

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            canvas
            controls
        }
    }

    private var canvas: some View {
        ZStack(alignment: .topLeading) {
            RoundedRectangle(cornerRadius: 8).fill(.quaternary.opacity(0.15))
            Canvas { context, _ in
                for edge in state.edges {
                    guard let source = position(of: edge.source), let target = position(of: edge.target) else { continue }
                    var path = Path()
                    path.move(to: source)
                    path.addLine(to: target)
                    context.stroke(path, with: .color(.secondary), lineWidth: 1.5)
                    if !edge.label.isEmpty {
                        let midpoint = CGPoint(x: (source.x + target.x) / 2, y: (source.y + target.y) / 2)
                        context.draw(Text(edge.label).font(.caption2), at: midpoint)
                    }
                }
            }
            ForEach(state.nodes) { node in
                nodeView(node)
                    .position(position(of: node.id) ?? CGPoint(x: 40, y: 40))
                    .gesture(
                        DragGesture()
                            .onChanged { value in
                                liveDragPositions[node.id] = clampedPoint(value.location)
                            }
                            .onEnded { value in
                                let final = clampedPoint(value.location)
                                liveDragPositions[node.id] = nil
                                updatePosition(nodeID: node.id, to: final)
                            }
                    )
                    .onTapGesture(count: 2) { editingNodeID = node.id; editingLabel = node.label }
            }
        }
        .frame(width: canvasSize.width, height: canvasSize.height)
        .clipped()
        .popover(item: Binding(get: { editingNodeID.map(IdentifiableString.init) }, set: { editingNodeID = $0?.value })) { editing in
            VStack(alignment: .leading, spacing: 8) {
                Text("Rename Node").font(.headline)
                TextField("Label", text: $editingLabel)
                HStack {
                    Spacer()
                    Button("Save") { renameNode(id: editing.value, to: editingLabel); editingNodeID = nil }
                }
            }
            .padding().frame(width: 220)
        }
    }

    private func nodeView(_ node: ArchitectureNode) -> some View {
        VStack(spacing: 2) {
            Text(node.label).font(.caption.bold())
            Text(node.kind).font(.caption2).foregroundStyle(.secondary)
        }
        .padding(8)
        .background(Color.accentColor.opacity(0.15), in: RoundedRectangle(cornerRadius: 8))
        .overlay(RoundedRectangle(cornerRadius: 8).stroke(Color.accentColor.opacity(0.4)))
        .overlay(alignment: .topTrailing) {
            Button(role: .destructive) { deleteNode(node.id) } label: { Image(systemName: "xmark.circle.fill") }
                .buttonStyle(.plain)
                .font(.caption2)
                .offset(x: 6, y: -6)
        }
    }

    private var controls: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("Double-click a node to rename it; drag to reposition.").font(.caption2).foregroundStyle(.secondary)
            HStack {
                TextField("New node label", text: $newNodeLabel)
                Button("Add Node") { addNode() }.disabled(newNodeLabel.isEmpty)
            }
            HStack {
                TextField("source id", text: $newEdgeSource).frame(width: 90)
                TextField("target id", text: $newEdgeTarget).frame(width: 90)
                TextField("label (optional)", text: $newEdgeLabel).frame(width: 120)
                Button("Add Edge") { addEdge() }.disabled(newEdgeSource.isEmpty || newEdgeTarget.isEmpty)
            }
            if !state.edges.isEmpty {
                ForEach(Array(state.edges.enumerated()), id: \.offset) { index, edge in
                    HStack {
                        Text("\(edge.source) → \(edge.target)\(edge.label.isEmpty ? "" : " (\(edge.label))")").font(.caption2)
                        Spacer()
                        Button(role: .destructive) { deleteEdge(at: index) } label: { Image(systemName: "trash") }.buttonStyle(.plain)
                    }
                }
            }
        }
    }

    private func position(of nodeID: String) -> CGPoint? {
        if let live = liveDragPositions[nodeID] { return live }
        guard let node = state.nodes.first(where: { $0.id == nodeID }) else { return nil }
        return CGPoint(x: node.x == 0 && node.y == 0 ? defaultPosition(for: node).x : node.x, y: node.x == 0 && node.y == 0 ? defaultPosition(for: node).y : node.y)
    }

    /// New nodes (x=0,y=0, i.e. never manually placed) get spread out in a
    /// simple grid rather than all stacking at the origin.
    private func defaultPosition(for node: ArchitectureNode) -> CGPoint {
        let index = state.nodes.firstIndex(where: { $0.id == node.id }) ?? 0
        let columns = 4
        let column = index % columns
        let row = index / columns
        return CGPoint(x: 60 + CGFloat(column) * 120, y: 50 + CGFloat(row) * 80)
    }

    private func clampedPoint(_ point: CGPoint) -> CGPoint {
        CGPoint(x: min(max(point.x, 20), canvasSize.width - 20), y: min(max(point.y, 20), canvasSize.height - 20))
    }

    private func updatePosition(nodeID: String, to point: CGPoint) {
        var updated = state
        guard let index = updated.nodes.firstIndex(where: { $0.id == nodeID }) else { return }
        updated.nodes[index].x = point.x
        updated.nodes[index].y = point.y
        onChange(updated)
    }

    private func addNode() {
        var updated = state
        let id = newNodeLabel.lowercased().replacingOccurrences(of: " ", with: "_")
        updated.nodes.append(ArchitectureNode(id: id, label: newNodeLabel, kind: "service"))
        newNodeLabel = ""
        onChange(updated)
    }

    private func renameNode(id: String, to newLabel: String) {
        guard !newLabel.isEmpty else { return }
        var updated = state
        guard let index = updated.nodes.firstIndex(where: { $0.id == id }) else { return }
        updated.nodes[index].label = newLabel
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
        updated.edges.append(ArchitectureEdge(source: newEdgeSource, target: newEdgeTarget, label: newEdgeLabel))
        newEdgeSource = ""; newEdgeTarget = ""; newEdgeLabel = ""
        onChange(updated)
    }

    private func deleteEdge(at index: Int) {
        var updated = state
        updated.edges.remove(at: index)
        onChange(updated)
    }
}

private struct IdentifiableString: Identifiable {
    let value: String
    var id: String { value }
}
