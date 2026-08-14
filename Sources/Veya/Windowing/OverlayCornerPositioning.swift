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
