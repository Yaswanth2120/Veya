import AppKit
import SwiftUI

/// Native field-editor handling keeps session fields editable even after
/// focus has moved between the main window and a nonactivating panel.
struct EditableSessionField: NSViewRepresentable {
    let label: String
    @Binding var text: String
    var identifier: String
    var focusesOnMount = false
    var multiline = false

    func makeCoordinator() -> Coordinator { Coordinator(text: $text) }

    func makeNSView(context: Context) -> SessionTextField {
        let field = SessionTextField()
        field.identifier = NSUserInterfaceItemIdentifier(identifier)
        field.setAccessibilityIdentifier(identifier)
        field.setAccessibilityLabel(label)
        field.placeholderString = label
        field.isEditable = true
        field.isSelectable = true
        field.isBezeled = true
        field.bezelStyle = .roundedBezel
        field.font = .systemFont(ofSize: 14)
        field.usesSingleLineMode = !multiline
        field.cell?.wraps = multiline
        field.maximumNumberOfLines = multiline ? 4 : 1
        field.delegate = context.coordinator
        field.focusesOnMount = focusesOnMount
        return field
    }

    func updateNSView(_ field: SessionTextField, context: Context) {
        context.coordinator.text = $text
        if field.stringValue != text { field.stringValue = text }
    }

    final class Coordinator: NSObject, NSTextFieldDelegate {
        var text: Binding<String>
        init(text: Binding<String>) { self.text = text }
        func controlTextDidChange(_ notification: Notification) {
            guard let field = notification.object as? NSTextField else { return }
            text.wrappedValue = field.stringValue
        }
    }
}

final class SessionTextField: NSTextField {
    var focusesOnMount = false
    override func mouseDown(with event: NSEvent) {
        NSApp.activate(ignoringOtherApps: true)
        window?.makeKeyAndOrderFront(nil)
        super.mouseDown(with: event)
    }
    override func viewDidMoveToWindow() {
        super.viewDidMoveToWindow()
        guard focusesOnMount, let window else { return }
        DispatchQueue.main.async { [weak self, weak window] in
            guard let self, let window else { return }
            window.makeKeyAndOrderFront(nil)
            window.makeFirstResponder(self)
        }
    }
}
