import CoreGraphics

/// Pure geometry for `CaptureCompatibilityTester`'s Retina-safe pixel
/// mapping — extracted so it can be unit tested against synthetic
/// coordinates without any real window/display/ScreenCaptureKit I/O.
///
/// Two coordinate flips happen here, and both are worth spelling out
/// because they're easy to get backwards:
///
/// 1. **AppKit → CoreGraphics global space.** `NSWindow.frame` uses
///    AppKit's coordinate system: origin at the bottom-left of the
///    primary screen, Y increasing upward. `CGDisplayBounds` (and
///    `ScreenCaptureKit`'s display geometry, which is the same
///    `CGDirectDisplayID`-keyed space) uses Quartz's global display
///    coordinate system: origin at the top-left of the primary display, Y
///    increasing downward. Converting requires flipping against the
///    *primary* screen's height, not the target display's — the global
///    origin is always anchored to the primary display regardless of
///    which display the overlay is actually on.
///
/// 2. **Points → captured pixels.** `CGDisplayBounds` reports the
///    display's bounds in **points** (verified empirically on this
///    project's Retina dev machine: `CGDisplayBounds` and
///    `CGDisplayPixelsWide`/`High` both report the *logical* point size,
///    matching `NSScreen.frame`, not the raw framebuffer pixel count).
///    The `CGImage` `ScreenCaptureKit` actually hands back may or may not
///    be at native Retina resolution depending on what
///    `SCStreamConfiguration.width`/`height` requested and what the
///    system actually honors. Rather than assume a scale factor (e.g.
///    hardcoding 2×, or trusting `NSScreen.backingScaleFactor`), this
///    computes the true scale from the *actual returned image's*
///    dimensions divided by the display's point size, every time — so it
///    self-corrects regardless of what resolution capture actually
///    produced. This is the concrete mechanism behind
///    `docs/PRESENTER_PRIVACY.md`'s "never assume 1 point == 1 pixel."
enum OverlayCropRectCalculator {
    /// - Parameters:
    ///   - overlayWindowFrame: `NSWindow.frame`, AppKit coordinates (points).
    ///   - primaryScreenHeight: `NSScreen.screens.first!.frame.height`, points.
    ///   - displayCGFrame: `CGDisplayBounds(displayID)`, points, Quartz global space.
    ///   - capturedImagePixelSize: The actual `CGImage`'s `(width, height)` in pixels.
    /// - Returns: The overlay's region within the captured image, in image
    ///   pixel coordinates, clipped to the image's bounds.
    static func cropRect(
        overlayWindowFrame: CGRect,
        primaryScreenHeight: CGFloat,
        displayCGFrame: CGRect,
        capturedImagePixelSize: CGSize
    ) -> CGRect {
        guard displayCGFrame.width > 0, displayCGFrame.height > 0 else { return .zero }

        // Step 1: AppKit (bottom-left origin, Y-up) → Quartz global space
        // (top-left origin, Y-down), still in points.
        let overlayCGFrame = CGRect(
            x: overlayWindowFrame.minX,
            y: primaryScreenHeight - overlayWindowFrame.maxY,
            width: overlayWindowFrame.width,
            height: overlayWindowFrame.height
        )

        // Step 2: points → captured pixels, using the *measured* scale.
        let scaleX = capturedImagePixelSize.width / displayCGFrame.width
        let scaleY = capturedImagePixelSize.height / displayCGFrame.height

        let pixelRect = CGRect(
            x: (overlayCGFrame.minX - displayCGFrame.minX) * scaleX,
            y: (overlayCGFrame.minY - displayCGFrame.minY) * scaleY,
            width: overlayCGFrame.width * scaleX,
            height: overlayCGFrame.height * scaleY
        )

        let imageBounds = CGRect(origin: .zero, size: capturedImagePixelSize)
        return pixelRect.intersection(imageBounds)
    }
}
