import CoreGraphics

/// Pure geometry for the overlay's default/migrated corner placement —
/// extracted so it's unit-testable against synthetic rects without a
/// real `NSPanel`/`NSScreen` (see `OverlayWindowController`, the only
/// caller). Section 19: also detects a *restored* saved frame that
/// looks like the stale pre-fix screen-center default, so an existing
/// user who saved that position before the corner-default fix existed
/// gets migrated to a corner too, not just first-launch users.
enum OverlayCornerPositioning {
    static let defaultMargin: CGFloat = 24
    /// How close to dead-center (in points, either axis) a restored
    /// frame has to be to be treated as "this was the old buggy
    /// screen-center default," not a position the user chose themselves.
    static let centeredDetectionTolerance: CGFloat = 40

    static func repairedFrame(_ frame: CGRect, preferredSize: CGSize, in visibleFrame: CGRect) -> CGRect {
        let widthLimit = min(CGFloat(640), visibleFrame.width - 2 * defaultMargin)
        let heightLimit = min(CGFloat(520), visibleFrame.height - 2 * defaultMargin)
        if frame.width < 300 || frame.height < 150 || frame.width > widthLimit || frame.height > heightLimit {
            let size = CGSize(width: min(preferredSize.width, widthLimit), height: min(preferredSize.height, heightLimit))
            return CGRect(origin: cornerOrigin(forPanelSize: size, in: visibleFrame), size: size)
        }
        return CGRect(x: min(max(frame.minX, visibleFrame.minX), visibleFrame.maxX - frame.width),
                      y: min(max(frame.minY, visibleFrame.minY), visibleFrame.maxY - frame.height),
                      width: frame.width, height: frame.height)
    }

    /// The bottom-right-corner origin for a panel of `size` within
    /// `visibleFrame`, `margin` points in from each edge.
    static func cornerOrigin(forPanelSize size: CGSize, in visibleFrame: CGRect, margin: CGFloat = defaultMargin) -> CGPoint {
        CGPoint(x: visibleFrame.maxX - size.width - margin, y: visibleFrame.minY + margin)
    }

    /// `true` if `frame`'s center falls within `centeredDetectionTolerance`
    /// points of `screenFrame`'s center on both axes.
    static func looksCentered(_ frame: CGRect, in screenFrame: CGRect, tolerance: CGFloat = centeredDetectionTolerance) -> Bool {
        let screenCenter = CGPoint(x: screenFrame.midX, y: screenFrame.midY)
        let frameCenter = CGPoint(x: frame.midX, y: frame.midY)
        return abs(frameCenter.x - screenCenter.x) < tolerance && abs(frameCenter.y - screenCenter.y) < tolerance
    }
}
