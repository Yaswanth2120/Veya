import AppKit
import SwiftUI
import Testing
@testable import Veya

@MainActor
@Suite(.serialized)
struct SessionFormInteractionTests {
    @Test func nativeFieldAcceptsEditingAndUpdatesBinding() async throws {
        _ = NSApplication.shared
        var value = ""
        let binding = Binding<String>(get: { value }, set: { value = $0 })
        let hosting = NSHostingView(rootView: EditableSessionField(label: "Interview title", text: binding, identifier: "test.title"))
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 500, height: 100), styleMask: [.titled], backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        window.contentView = hosting
        hosting.layoutSubtreeIfNeeded()
        defer { window.close() }
        func findField(_ view: NSView) -> NSTextField? {
            if let field = view as? NSTextField { return field }
            for child in view.subviews { if let found = findField(child) { return found } }
            return nil
        }
        let field = try #require(findField(hosting))
        #expect(field.isEditable && field.isSelectable)
        #expect(window.makeFirstResponder(field))
        let editor = try #require(field.currentEditor() as? NSTextView)
        editor.insertText("GPU interview", replacementRange: NSRange(location: 0, length: 0))
        editor.didChangeText()
        #expect(value == "GPU interview")
    }

    @Test func corruptTallOverlayIsRepaired() {
        let screen = CGRect(x: 0, y: 0, width: 1710, height: 1073)
        let broken = CGRect(x: 670, y: 48, width: 380, height: 1025)
        let repaired = OverlayCornerPositioning.repairedFrame(broken, preferredSize: CGSize(width: 380, height: 190), in: screen)
        #expect(repaired.height == 190)
        #expect(screen.contains(repaired))
        #expect(repaired.maxX < screen.maxX)
    }

    @Test func importedResumeMakesCreationRequirementsClear() {
        let model = CreateSessionViewModel()
        #expect(model.validationMessage == "Enter an interview title.")
        model.title = "Practice"
        #expect(model.validationMessage != nil)
        model.handleFileImport(.success([URL(fileURLWithPath: "/tmp/resume.docx")]))
        #expect(model.hasResumeDocument)
        #expect(model.validationMessage == nil)
        model.handleFileImport(.success([URL(fileURLWithPath: "/tmp/resume.docx")]))
        #expect(model.attachedDocuments.count == 1)
    }
}
