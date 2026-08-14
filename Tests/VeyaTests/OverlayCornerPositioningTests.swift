import CoreGraphics
import Testing
@testable import Veya

@Suite("OverlayCornerPositioning (Section 19)")
struct OverlayCornerPositioningTests {
    @Test("corner origin anchors to the bottom-right with the configured margin")
    func cornerOriginAnchorsBottomRight() {
        let screen = CGRect(x: 0, y: 0, width: 1920, height: 1080)
        let panelSize = CGSize(width: 380, height: 190)
        let origin = OverlayCornerPositioning.cornerOrigin(forPanelSize: panelSize, in: screen, margin: 24)

        let expectedX: CGFloat = 1516
        let expectedY: CGFloat = 24
        #expect(origin.x == expectedX)
        #expect(origin.y == expectedY)
    }

    @Test("a frame at dead-center is detected as centered")
    func deadCenterIsDetected() {
        let screen = CGRect(x: 0, y: 0, width: 1920, height: 1080)
        let centeredFrame = CGRect(x: (1920 - 440) / 2, y: (1080 - 440) / 2, width: 440, height: 440)
        #expect(OverlayCornerPositioning.looksCentered(centeredFrame, in: screen))
    }

    @Test("a frame in a screen corner is not detected as centered")
    func cornerFrameIsNotDetectedAsCentered() {
        let screen = CGRect(x: 0, y: 0, width: 1920, height: 1080)
        let cornerFrame = CGRect(x: 1920 - 380 - 24, y: 24, width: 380, height: 190)
        #expect(!OverlayCornerPositioning.looksCentered(cornerFrame, in: screen))
    }

    @Test("a frame just outside the tolerance is not detected as centered")
    func justOutsideToleranceIsNotCentered() {
        let screen = CGRect(x: 0, y: 0, width: 2000, height: 1000)
        let screenCenter = CGPoint(x: 1000, y: 500)
        // Offset well beyond the tolerance on the x-axis.
        let frame = CGRect(x: screenCenter.x + 100 - 220, y: screenCenter.y - 220, width: 440, height: 440)
        #expect(!OverlayCornerPositioning.looksCentered(frame, in: screen, tolerance: 40))
    }

    @Test("a frame just inside the tolerance is detected as centered")
    func justInsideToleranceIsCentered() {
        let screen = CGRect(x: 0, y: 0, width: 2000, height: 1000)
        let screenCenter = CGPoint(x: 1000, y: 500)
        let frame = CGRect(x: screenCenter.x + 10 - 220, y: screenCenter.y - 220, width: 440, height: 440)
        #expect(OverlayCornerPositioning.looksCentered(frame, in: screen, tolerance: 40))
    }
}
