import CoreGraphics
import Testing
@testable import Veya

@Suite("OverlayCropRectCalculator")
struct OverlayCropRectCalculatorTests {
    /// A 1512×982-point primary display (matches a real 14" MacBook Air
    /// point resolution), captured at native 2× Retina pixel resolution.
    @Test("Retina (2x): a window in the top-right maps to the correct pixel rect")
    func retinaTopRightWindow() {
        let displayCGFrame = CGRect(x: 0, y: 0, width: 1512, height: 982)
        let primaryScreenHeight: CGFloat = 982

        // AppKit frame: 300x200pt window, positioned with its bottom-left
        // corner 50pt from the right edge and 700pt up from the bottom —
        // i.e. near the top-right of the screen.
        let overlayWindowFrame = CGRect(x: 1512 - 350, y: 700, width: 300, height: 200)

        let crop = OverlayCropRectCalculator.cropRect(
            overlayWindowFrame: overlayWindowFrame,
            primaryScreenHeight: primaryScreenHeight,
            displayCGFrame: displayCGFrame,
            capturedImagePixelSize: CGSize(width: 3024, height: 1964) // native 2x
        )

        // Expected: scale is exactly 2x. AppKit top edge (y=900) is 82pt
        // from the top of the 982pt-tall screen, so in Quartz global space
        // (top-left origin) the window's top-left y is 82pt -> 164px.
        let expectedX: CGFloat = 2324
        let expectedY: CGFloat = 164
        let expectedWidth: CGFloat = 600
        let expectedHeight: CGFloat = 400
        #expect(crop.origin.x == expectedX)
        #expect(crop.origin.y == expectedY)
        #expect(crop.width == expectedWidth)
        #expect(crop.height == expectedHeight)
    }

    @Test("non-Retina (1x): scale factor is measured as 1, not assumed")
    func nonRetinaScale() {
        let displayCGFrame = CGRect(x: 0, y: 0, width: 1920, height: 1080)
        let overlayWindowFrame = CGRect(x: 100, y: 100, width: 400, height: 300)

        let crop = OverlayCropRectCalculator.cropRect(
            overlayWindowFrame: overlayWindowFrame,
            primaryScreenHeight: 1080,
            displayCGFrame: displayCGFrame,
            capturedImagePixelSize: CGSize(width: 1920, height: 1080) // 1x
        )

        let expectedWidth: CGFloat = 400
        let expectedHeight: CGFloat = 300
        #expect(crop.width == expectedWidth)
        #expect(crop.height == expectedHeight)
    }

    @Test("scale factor is derived from the actual captured image, not backingScaleFactor")
    func scaleIsMeasuredNotAssumed() {
        // A display reporting 1512x982 points, but a capture that (for
        // whatever reason — a requested downscale, a HiDPI-unaware path,
        // etc.) came back at 1.5x rather than the "expected" 2x. The
        // calculator must not silently assume 2x.
        let displayCGFrame = CGRect(x: 0, y: 0, width: 1512, height: 982)
        let overlayWindowFrame = CGRect(x: 0, y: 0, width: 200, height: 100)

        let crop = OverlayCropRectCalculator.cropRect(
            overlayWindowFrame: overlayWindowFrame,
            primaryScreenHeight: 982,
            displayCGFrame: displayCGFrame,
            capturedImagePixelSize: CGSize(width: 2268, height: 1473) // 1.5x
        )

        let expectedWidth: CGFloat = 300 // 200 * 1.5
        let expectedHeight: CGFloat = 150 // 100 * 1.5
        #expect(crop.width == expectedWidth)
        #expect(crop.height == expectedHeight)
    }

    @Test("a window positioned at the exact bottom-left of the screen maps to the bottom-left of the image")
    func bottomLeftWindow() {
        let displayCGFrame = CGRect(x: 0, y: 0, width: 1000, height: 800)
        let overlayWindowFrame = CGRect(x: 0, y: 0, width: 100, height: 50)

        let crop = OverlayCropRectCalculator.cropRect(
            overlayWindowFrame: overlayWindowFrame,
            primaryScreenHeight: 800,
            displayCGFrame: displayCGFrame,
            capturedImagePixelSize: CGSize(width: 1000, height: 800)
        )

        // AppKit bottom-left (y=0) is the *bottom* of the screen, which in
        // Quartz's top-left-origin space is near the maximum Y, not zero.
        let expectedX: CGFloat = 0
        let expectedY: CGFloat = 750 // 800 - 50
        #expect(crop.origin.x == expectedX)
        #expect(crop.origin.y == expectedY)
    }

    @Test("a window on a secondary (non-primary) display still maps correctly")
    func secondaryDisplayWindow() {
        // A second display to the right of a 1512-wide primary, itself
        // 1920x1080 points, whose CGDisplayBounds origin is offset.
        let displayCGFrame = CGRect(x: 1512, y: 0, width: 1920, height: 1080)
        let overlayWindowFrame = CGRect(x: 1512 + 100, y: 100, width: 200, height: 100)

        let crop = OverlayCropRectCalculator.cropRect(
            overlayWindowFrame: overlayWindowFrame,
            primaryScreenHeight: 982, // primary's height, not the secondary's
            displayCGFrame: displayCGFrame,
            capturedImagePixelSize: CGSize(width: 1920, height: 1080)
        )

        let expectedX: CGFloat = 100
        let expectedWidth: CGFloat = 200
        let expectedHeight: CGFloat = 100
        #expect(crop.origin.x == expectedX)
        #expect(crop.width == expectedWidth)
        #expect(crop.height == expectedHeight)
    }

    @Test("a window entirely outside the display bounds crops to empty, not a crash")
    func windowOutsideDisplayBounds() {
        let displayCGFrame = CGRect(x: 0, y: 0, width: 1000, height: 800)
        let overlayWindowFrame = CGRect(x: 5000, y: 5000, width: 100, height: 100)

        let crop = OverlayCropRectCalculator.cropRect(
            overlayWindowFrame: overlayWindowFrame,
            primaryScreenHeight: 800,
            displayCGFrame: displayCGFrame,
            capturedImagePixelSize: CGSize(width: 1000, height: 800)
        )

        #expect(crop.isEmpty)
    }
}
