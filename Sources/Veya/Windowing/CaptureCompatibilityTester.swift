import AppKit
import SwiftUI
@preconcurrency import ScreenCaptureKit
import CoreGraphics

protocol CaptureCompatibilityTesting: Sendable {
    func test(overlayWindow: NSWindow, displayID: UInt32) async throws -> CaptureTestResult
}

/// Runs Veya's local capture-verification diagnostic: temporarily swaps the
/// overlay window's content for a deterministic checkerboard marker,
/// captures several frames of the target display with `ScreenCaptureKit`
/// (`SCScreenshotManager`, not a persistent `SCStream` — a single-shot
/// capture per frame is all this diagnostic needs), and checks pixel data
/// for the marker.
///
/// This tells you whether **Veya's own local capture diagnostic** detects
/// the overlay — it is not proof of behavior in any specific third-party
/// conferencing app. See `docs/PRESENTER_PRIVACY.md`.
actor CaptureCompatibilityTester: CaptureCompatibilityTesting {
    /// Number of frames sampled per test, and the minimum that must
    /// actually succeed for the result to be conclusive — see the
    /// aggregation rules in `docs/PRESENTER_PRIVACY.md`.
    private let frameCount: Int
    private let frameIntervalNanoseconds: UInt64

    init(frameCount: Int = 5, frameIntervalNanoseconds: UInt64 = 120_000_000) {
        self.frameCount = frameCount
        self.frameIntervalNanoseconds = frameIntervalNanoseconds
    }

    func test(overlayWindow: NSWindow, displayID: UInt32) async throws -> CaptureTestResult {
        let macOSVersion = ProcessInfo.processInfo.operatingSystemVersionString
        let testID = UUID()
        let testedAt = Date()

        guard CGPreflightScreenCaptureAccess() else {
            throw PresenterPrivacyError.screenCapturePermissionDenied
        }

        let shareableContent = try await SCShareableContent.current
        guard let scDisplay = shareableContent.displays.first(where: { $0.displayID == displayID }) else {
            return CaptureTestResult(
                id: testID, testedAt: testedAt, macOSVersion: macOSVersion, appVersion: AppVersion.current,
                displayID: displayID, overlayDetected: nil, confidence: 0, status: .unsupported,
                diagnosticMessage: "The selected display isn't available for ScreenCaptureKit capture."
            )
        }

        let displayCGFrame = CGDisplayBounds(CGDirectDisplayID(displayID))
        let primaryScreenHeight = await MainActor.run { NSScreen.screens.first?.frame.height ?? 0 }

        guard primaryScreenHeight > 0, displayCGFrame.width > 0, displayCGFrame.height > 0 else {
            return CaptureTestResult(
                id: testID, testedAt: testedAt, macOSVersion: macOSVersion, appVersion: AppVersion.current,
                displayID: displayID, overlayDetected: nil, confidence: 0, status: .error,
                diagnosticMessage: "Could not determine display geometry."
            )
        }

        let originalContent = await MainActor.run { () -> NSView? in
            let original = overlayWindow.contentView
            overlayWindow.contentView = NSHostingView(rootView: DiagnosticMarkerView())
            overlayWindow.displayIfNeeded()
            return original
        }

        // Give the marker a moment to actually be composited before sampling.
        try? await Task.sleep(nanoseconds: 100_000_000)

        let overlayFrame = await MainActor.run { overlayWindow.frame }

        let filter = SCContentFilter(display: scDisplay, excludingWindows: [])
        let configuration = SCStreamConfiguration()
        configuration.width = Int(displayCGFrame.width)
        configuration.height = Int(displayCGFrame.height)
        configuration.showsCursor = false

        var detections: [Bool] = []
        var captureFailures = 0

        for frameIndex in 0..<frameCount {
            if frameIndex > 0 {
                try? await Task.sleep(nanoseconds: frameIntervalNanoseconds)
            }
            do {
                let image = try await SCScreenshotManager.captureImage(contentFilter: filter, configuration: configuration)
                let cropRect = OverlayCropRectCalculator.cropRect(
                    overlayWindowFrame: overlayFrame,
                    primaryScreenHeight: primaryScreenHeight,
                    displayCGFrame: displayCGFrame,
                    capturedImagePixelSize: CGSize(width: image.width, height: image.height)
                )

                if let detected = DiagnosticMarkerDetector.detect(in: image, cropRect: cropRect) {
                    detections.append(detected)
                } else {
                    captureFailures += 1
                }
            } catch {
                captureFailures += 1
            }
        }

        await MainActor.run {
            overlayWindow.contentView = originalContent
        }

        let total = detections.count
        let detectedCount = detections.filter { $0 }.count
        let aggregate = CaptureResultAggregator.aggregate(
            detections: detections,
            captureFailures: captureFailures,
            frameCount: frameCount
        )

        return CaptureTestResult(
            id: testID,
            testedAt: testedAt,
            macOSVersion: macOSVersion,
            appVersion: AppVersion.current,
            displayID: displayID,
            overlayDetected: total > 0 ? detectedCount > 0 : nil,
            confidence: aggregate.confidence,
            status: aggregate.status,
            diagnosticMessage: aggregate.message
        )
    }
}

// MARK: - Diagnostic marker

/// A deterministic checkerboard signature — never OCR, never real screen
/// content. `DiagnosticMarkerDetector` looks for exactly this pattern.
enum DiagnosticMarker {
    static let gridSize = 4
    static let colorA = Color(red: 1, green: 0, blue: 1) // magenta
    static let colorB = Color(red: 0, green: 1, blue: 1) // cyan
    static let rgbA: (r: Double, g: Double, b: Double) = (1, 0, 1)
    static let rgbB: (r: Double, g: Double, b: Double) = (0, 1, 1)
}

struct DiagnosticMarkerView: View {
    var body: some View {
        Canvas { context, size in
            let gridSize = DiagnosticMarker.gridSize
            let cell = CGSize(width: size.width / CGFloat(gridSize), height: size.height / CGFloat(gridSize))
            for row in 0..<gridSize {
                for col in 0..<gridSize {
                    let isA = (row + col).isMultiple(of: 2)
                    let rect = CGRect(
                        x: CGFloat(col) * cell.width,
                        y: CGFloat(row) * cell.height,
                        width: cell.width,
                        height: cell.height
                    )
                    context.fill(Path(rect), with: .color(isA ? DiagnosticMarker.colorA : DiagnosticMarker.colorB))
                }
            }
        }
        .accessibilityHidden(true)
    }
}

/// Reads raw pixel bytes from the captured `CGImage` directly (no OCR, no
/// secondary `CGContext` re-render) to check whether the checkerboard
/// pattern from `DiagnosticMarkerView` is present in the given region.
enum DiagnosticMarkerDetector {
    /// `nil` means "couldn't sample this frame" (not "marker absent") —
    /// callers must treat that as a capture failure, not evidence.
    static func detect(in image: CGImage, cropRect: CGRect) -> Bool? {
        let gridSize = DiagnosticMarker.gridSize
        guard cropRect.width >= CGFloat(gridSize * 2), cropRect.height >= CGFloat(gridSize * 2) else {
            return nil
        }

        var matches = 0
        var sampled = 0

        for row in 0..<gridSize {
            for col in 0..<gridSize {
                let cellRect = CGRect(
                    x: cropRect.minX + cropRect.width * CGFloat(col) / CGFloat(gridSize),
                    y: cropRect.minY + cropRect.height * CGFloat(row) / CGFloat(gridSize),
                    width: cropRect.width / CGFloat(gridSize),
                    height: cropRect.height / CGFloat(gridSize)
                )
                guard let color = averageColor(in: image, rect: cellRect) else { continue }
                sampled += 1
                let expected = (row + col).isMultiple(of: 2) ? DiagnosticMarker.rgbA : DiagnosticMarker.rgbB
                if colorsMatch(color, expected) {
                    matches += 1
                }
            }
        }

        guard sampled > 0 else { return nil }
        return Double(matches) / Double(sampled) >= 0.75
    }

    private static func colorsMatch(
        _ a: (r: Double, g: Double, b: Double),
        _ b: (r: Double, g: Double, b: Double),
        tolerance: Double = 0.25
    ) -> Bool {
        abs(a.r - b.r) < tolerance && abs(a.g - b.g) < tolerance && abs(a.b - b.b) < tolerance
    }

    /// `SCScreenshotManager` returns 32-bit BGRA `CGImage`s for SDR
    /// captures (the default — Veya never requests HDR), documented on
    /// `captureImage(contentFilter:configuration:)`. Reads bytes directly
    /// rather than re-rendering into a fresh context, since that would
    /// reintroduce the exact top/bottom row-order ambiguity this avoids.
    private static func averageColor(in image: CGImage, rect: CGRect) -> (r: Double, g: Double, b: Double)? {
        let intRect = rect.integral
        guard intRect.width >= 1, intRect.height >= 1,
              let cropped = image.cropping(to: intRect),
              let data = cropped.dataProvider?.data,
              let bytes = CFDataGetBytePtr(data)
        else {
            return nil
        }

        let bytesPerPixel = cropped.bitsPerPixel / 8
        guard bytesPerPixel == 4 else { return nil }
        let bytesPerRow = cropped.bytesPerRow
        let width = cropped.width
        let height = cropped.height
        guard width > 0, height > 0 else { return nil }

        let stepX = max(width / 6, 1)
        let stepY = max(height / 6, 1)

        var sumR = 0.0, sumG = 0.0, sumB = 0.0
        var count = 0

        var y = 0
        while y < height {
            var x = 0
            while x < width {
                let offset = y * bytesPerRow + x * bytesPerPixel
                guard offset + 2 < CFDataGetLength(data) else { break }
                sumB += Double(bytes[offset])
                sumG += Double(bytes[offset + 1])
                sumR += Double(bytes[offset + 2])
                count += 1
                x += stepX
            }
            y += stepY
        }

        guard count > 0 else { return nil }
        return (sumR / Double(count) / 255.0, sumG / Double(count) / 255.0, sumB / Double(count) / 255.0)
    }
}
