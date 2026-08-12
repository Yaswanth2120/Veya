import Carbon.HIToolbox
import AppKit

/// Registers system-wide hotkeys via the Carbon `RegisterEventHotKey` API
/// (still the standard, documented mechanism for global hotkeys that don't
/// require Accessibility/Input-Monitoring permission). Two hotkeys for this
/// phase: toggle overlay visibility, toggle compact/expanded mode.
@MainActor
final class GlobalHotkeyManager {
    typealias Handler = () -> Void

    private struct HotKeyBinding {
        let id: UInt32
        let hotKeyRef: EventHotKeyRef
        let handler: Handler
    }

    private nonisolated(unsafe) var bindings: [UInt32: HotKeyBinding] = [:]
    private nonisolated(unsafe) var eventHandlerRef: EventHandlerRef?
    private var nextID: UInt32 = 1

    private let signature: OSType = {
        // "Veya" as a four-char code.
        let bytes: [UInt8] = Array("Veya".utf8)
        return bytes.reduce(OSType(0)) { ($0 << 8) | OSType($1) }
    }()

    init() {
        installEventHandler()
    }

    deinit {
        if let eventHandlerRef {
            RemoveEventHandler(eventHandlerRef)
        }
        for binding in bindings.values {
            UnregisterEventHotKey(binding.hotKeyRef)
        }
    }

    /// Registers a hotkey. `keyCode` is a Carbon virtual key code (see
    /// `HIToolbox/Events.h`, e.g. `kVK_ANSI_O`); `modifiers` combine
    /// `cmdKey`, `optionKey`, `controlKey`, `shiftKey`.
    @discardableResult
    func register(keyCode: UInt32, modifiers: UInt32, handler: @escaping Handler) -> Bool {
        let id = nextID
        nextID += 1

        var hotKeyRef: EventHotKeyRef?
        let hotKeyID = EventHotKeyID(signature: signature, id: id)
        let status = RegisterEventHotKey(keyCode, modifiers, hotKeyID, GetApplicationEventTarget(), 0, &hotKeyRef)

        guard status == noErr, let hotKeyRef else { return false }
        bindings[id] = HotKeyBinding(id: id, hotKeyRef: hotKeyRef, handler: handler)
        return true
    }

    func unregisterAll() {
        for binding in bindings.values {
            UnregisterEventHotKey(binding.hotKeyRef)
        }
        bindings.removeAll()
    }

    private func installEventHandler() {
        var eventType = EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed))
        let selfPointer = Unmanaged.passUnretained(self).toOpaque()

        InstallEventHandler(
            GetApplicationEventTarget(),
            { _, eventRef, userData in
                guard let eventRef, let userData else { return noErr }
                var hotKeyID = EventHotKeyID()
                let status = GetEventParameter(
                    eventRef,
                    EventParamName(kEventParamDirectObject),
                    EventParamType(typeEventHotKeyID),
                    nil,
                    MemoryLayout<EventHotKeyID>.size,
                    nil,
                    &hotKeyID
                )
                guard status == noErr else { return status }

                let manager = Unmanaged<GlobalHotkeyManager>.fromOpaque(userData).takeUnretainedValue()
                MainActor.assumeIsolated {
                    manager.bindings[hotKeyID.id]?.handler()
                }
                return noErr
            },
            1,
            &eventType,
            selfPointer,
            &eventHandlerRef
        )
    }
}
