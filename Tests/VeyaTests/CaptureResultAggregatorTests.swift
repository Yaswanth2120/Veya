import Testing
@testable import Veya

@Suite("CaptureResultAggregator")
struct CaptureResultAggregatorTests {
    @Test("0/5 marker detections is a verified candidate")
    func zeroOfFiveIsVerified() {
        let aggregate = CaptureResultAggregator.aggregate(
            detections: [false, false, false, false, false],
            captureFailures: 0,
            frameCount: 5
        )
        #expect(aggregate.status == .verified)
        #expect(aggregate.confidence == 1.0)
    }

    @Test("5/5 marker detections is overlayDetected")
    func fiveOfFiveIsOverlayDetected() {
        let aggregate = CaptureResultAggregator.aggregate(
            detections: [true, true, true, true, true],
            captureFailures: 0,
            frameCount: 5
        )
        #expect(aggregate.status == .overlayDetected)
        #expect(aggregate.confidence == 1.0)
    }

    @Test("a mixed result is uncertain, never verified or overlayDetected")
    func mixedResultIsUncertain() {
        let aggregate = CaptureResultAggregator.aggregate(
            detections: [true, false, true, false, false],
            captureFailures: 0,
            frameCount: 5
        )
        #expect(aggregate.status == .uncertain)
    }

    @Test("insufficient successful captures never produces verified")
    func insufficientSamplesIsUncertainNotVerified() {
        let aggregate = CaptureResultAggregator.aggregate(
            detections: [false],
            captureFailures: 4,
            frameCount: 5
        )
        #expect(aggregate.status == .uncertain)
        #expect(aggregate.status != .verified)
    }

    @Test("one dropped frame out of five still allows a conclusive verdict")
    func oneDroppedFrameIsStillConclusive() {
        let aggregate = CaptureResultAggregator.aggregate(
            detections: [false, false, false, false],
            captureFailures: 1,
            frameCount: 5
        )
        #expect(aggregate.status == .verified)
    }

    @Test("never produces verified when any detection occurred")
    func neverVerifiedWithAnyDetection() {
        let aggregate = CaptureResultAggregator.aggregate(
            detections: [true, false, false, false, false],
            captureFailures: 0,
            frameCount: 5
        )
        #expect(aggregate.status != .verified)
    }
}
