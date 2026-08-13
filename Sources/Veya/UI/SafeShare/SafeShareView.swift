import AppKit
import AVFoundation
import SwiftUI

/// The AppKit view that actually renders Safe Share frames. Its backing
/// layer *is* an `AVSampleBufferDisplayLayer` — the efficient, purpose-
/// built way to display a stream of `CMSampleBuffer`s with minimal CPU/GPU
/// overhead, with no manual Metal work and no per-frame image conversion.
final class SafeShareDisplayNSView: NSView {
    private let sampleBufferLayer = AVSampleBufferDisplayLayer()

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        wantsLayer = true
    }

    required init?(coder: NSCoder) {
        super.init(coder: coder)
        wantsLayer = true
    }

    override func makeBackingLayer() -> CALayer {
        sampleBufferLayer.videoGravity = .resizeAspect
        return sampleBufferLayer
    }

    func enqueue(_ sampleBuffer: CMSampleBuffer) {
        guard sampleBufferLayer.isReadyForMoreMediaData else { return }
        if sampleBufferLayer.status == .failed {
            sampleBufferLayer.flush()
        }
        sampleBufferLayer.enqueue(sampleBuffer)
    }
}

/// Thin SwiftUI wrapper so the AppKit rendering view can be hosted the same
/// way the rest of Veya's windows are, per the build prompt's file layout.
/// Frame delivery bypasses SwiftUI entirely — `SafeShareWindowController`
/// calls `SafeShareDisplayNSView.enqueue(_:)` directly.
struct SafeShareView: NSViewRepresentable {
    let displayView: SafeShareDisplayNSView

    func makeNSView(context: Context) -> SafeShareDisplayNSView { displayView }
    func updateNSView(_ nsView: SafeShareDisplayNSView, context: Context) {}
}
