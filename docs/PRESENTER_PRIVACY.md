# Presenter Privacy — Architecture

This document covers the Section 5 subsystem: Presenter Privacy, capture
verification, and Veya Safe Share. It assumes familiarity with
`ARCHITECTURE.md`.

## 1. Privacy architecture overview

Presenter Privacy has two independent paths, selected by
`PresenterPrivacyMode`:

```text
PresenterPrivacyManager (@MainActor, ObservableObject)
   ├── DisplayManager            — which displays exist, which is "preferred"
   ├── CaptureCompatibilityTester — Direct Private Overlay verification
   └── SafeShareManager
          ├── SafeShareCaptureEngine (actor)  — owns the SCStream
          └── SafeShareWindowController        — the "Veya Safe Share" window
```

`PresenterPrivacyManager` owns preferences and the current `status`; it
delegates all actual capture work to dedicated components so it stays a
thin coordinator, not a god object.

## 2. Direct Private Overlay

`PresenterPrivacyManager.applyDirectOverlayPolicyIfPossible()` sets:

```swift
window.sharingType = .none
```

on the Veya-owned overlay window. `NSWindowSharingType` (`AppKit`,
available since macOS 10.5) is a long-standing, public, documented API —
not a private one. Setting `sharingType = .none` declares to the window
server that the window's contents "may not be read by another process."

## 3. Why direct exclusion is best-effort

Setting `sharingType` changes what the *window server* advertises, but
capture pipelines vary in how faithfully they honor it:

- Some screen-recording/sharing paths respect it outright.
- Some capture frameworks (including parts of `ScreenCaptureKit` itself,
  depending on the content filter used) may still include the window.
- Third-party conferencing apps each implement their own capture path,
  which Veya cannot inspect or control.

**Setting the property is not proof of anything.** The only way to know
whether it actually worked for a specific capture path is to measure it —
which is what `CaptureCompatibilityTester` does. Veya never reports
`verified` from configuration alone; the flow is always:

```text
apply best-effort policy → run compatibility test → the measured result determines status
```

## 4. Compatibility testing

`CaptureCompatibilityTester` is an `actor` implementing
`CaptureCompatibilityTesting`. For one test run it:

1. Swaps the overlay window's `contentView` for a deterministic
   checkerboard `DiagnosticMarkerView` (4×4 grid, alternating magenta
   `(1,0,1)` / cyan `(0,1,1)`).
2. Captures 5 frames of the target display, 120ms apart, via
   `SCScreenshotManager.captureImage(contentFilter:configuration:)` — a
   single-shot capture API, not a persistent `SCStream`, since a one-off
   diagnostic doesn't need continuous streaming.
3. For each frame, crops to the overlay's mapped screen region and
   samples a 4×4 grid of average colors directly from the `CGImage`'s raw
   BGRA pixel bytes (see §9) — no OCR, ever.
4. Restores the overlay's real content, even if capture failed partway.
5. Aggregates the 5 per-frame results (see §5) into one
   `CaptureTestResult`.

The content filter used for the test is deliberately the **plain,
unfiltered display** (`SCContentFilter(display:excludingWindows: [])`) —
the test wants to know whether the overlay is visible in a raw capture of
everything on screen, the same shape of capture most screen-share
pipelines start from.

## 5. Multi-frame aggregation rules

Implemented as the pure, independently-tested `CaptureResultAggregator`:

| Detected / captured        | Result           |
|-----------------------------|-------------------|
| 0 / N (N ≥ minimum samples) | `verified`        |
| N / N                        | `overlayDetected` |
| some but not all             | `uncertain`       |
| fewer than `max(3, frameCount-1)` frames captured | `uncertain` (inconclusive — never `verified`) |

`verified` **requires** a successful capture, zero detections, and enough
samples to be conclusive. A test that only got 1 usable frame out of 5
never reports `verified`, no matter what that one frame showed.

## 6. ScreenCaptureKit

Veya uses only public, documented `ScreenCaptureKit` APIs, verified
against the installed macOS 26.2 SDK headers directly rather than assumed
from memory:

- `SCShareableContent.current` — displays, windows, running applications.
- `SCContentFilter` — see §7.
- `SCScreenshotManager.captureImage(contentFilter:configuration:)` — one-
  shot capture, used by the compatibility tester.
- `SCStream` / `SCStreamOutput` / `SCStreamDelegate` — continuous capture,
  used by Safe Share.
- `CGPreflightScreenCaptureAccess()` — permission check (`CoreGraphics`,
  public).

## 7. `SCContentFilter` configuration

Two different filters are used for two different purposes:

**Compatibility test** (§4): `SCContentFilter(display:excludingWindows:)`
with an empty exclusion list — capture *everything*, so the test can see
whether the overlay leaks through.

**Safe Share** (§8): `SCContentFilter(display:excludingApplications:exceptingWindows:)`,
excluding the `SCRunningApplication` that matches Veya's own process
(`ProcessInfo.processInfo.processIdentifier == app.processID`). This is
the header-documented initializer for "capture this display, minus
everything owned by these applications." Because Veya is excluded at the
**application** level, not per-window, every Veya-owned window is
excluded in one filter — overlay, Dashboard, Settings, and the Safe Share
window itself. That's also what prevents Safe Share from recursively
capturing its own output — see §12.

## 8. Veya Safe Share — rendering pipeline

```text
SCStream (SafeShareCaptureEngine, actor)
   │  SCStreamOutput.stream(_:didOutputSampleBuffer:of:) — off-actor callback
   ▼
AsyncStream<SafeShareFrame> (bufferingPolicy: .bufferingNewest(1))
   │  SafeShareManager's frame-consumer Task
   ▼
SafeShareDisplayNSView.enqueue(_:)
   │  its own backing layer IS an AVSampleBufferDisplayLayer
   ▼
"Veya Safe Share" window
```

`AVSampleBufferDisplayLayer` is the purpose-built AVFoundation layer for
displaying a stream of `CMSampleBuffer`s with minimal CPU/GPU work — no
manual Metal pipeline, no per-frame `CGImage`/`NSImage` conversion. Frames
are enqueued only when `isReadyForMoreMediaData` is true; otherwise
they're dropped (see §11).

`SafeShareView` (`UI/SafeShare/SafeShareView.swift`) is a thin
`NSViewRepresentable` wrapper — it exists so the window's content fits
Veya's usual SwiftUI-hosting pattern, but frame delivery bypasses SwiftUI
entirely: `SafeShareWindowController.render(_:)` calls
`SafeShareDisplayNSView.enqueue(_:)` directly, never going through
SwiftUI's diffing per frame.

## 9. Diagnostic marker & Retina handling

The checkerboard's pixel sampling never assumes `1 point == 1 pixel`. The
coordinate math is factored into `OverlayCropRectCalculator` — a pure,
side-effect-free function, unit tested (`OverlayCropRectCalculatorTests`)
against synthetic 1×, 1.5×, and 2× (Retina) scenarios, a secondary
display, and a window entirely off-display, independent of any real
window/display/ScreenCaptureKit I/O:

1. Converts the overlay's `NSWindow.frame` (AppKit: origin bottom-left of
   the primary screen, Y-up) into `CoreGraphics`/`ScreenCaptureKit` global
   display coordinates (origin top-left, Y-down) by flipping against the
   **primary** screen's height — not the target display's, since Quartz's
   global origin is always anchored to the primary display regardless of
   which display the overlay is actually on.
2. Computes the crop rectangle in **image pixel space** using the actual
   returned `CGImage`'s `width`/`height` divided by the display's point
   size (`CGDisplayBounds`) — not a hardcoded scale factor, and not
   `NSScreen.backingScaleFactor` — so it's correct whether
   `SCScreenshotManager` honors the requested capture resolution exactly
   or not. Verified empirically on this project's Retina dev machine:
   `CGDisplayBounds` and `CGDisplayPixelsWide`/`High` both report the
   *logical point size* (matching `NSScreen.frame`), not the raw
   framebuffer pixel count — so both `overlayWindowFrame` and
   `displayCGFrame` are already in the same unit (points) before this
   step, and only this step introduces the points→pixels conversion.
3. `CaptureCompatibilityTester` then reads pixel bytes directly from the
   cropped `CGImage`'s `CGDataProvider` (`SCScreenshotManager` returns
   32-bit BGRA `CGImage`s for SDR captures, per its header documentation —
   Veya never requests HDR) rather than re-rendering into a second
   `CGContext`, which would reintroduce exactly the top/bottom row-order
   ambiguity this avoids.

## 10. Permission model

Safe Share and the compatibility test both require **Screen Recording**
permission, because Veya itself is capturing the display. The overlay
(Direct Private Overlay or Normal mode) requires **no** special
permission — it's just a window. Veya is careful to only mention Screen
Recording permission in the context of capture (Safe Share, compatibility
test), never as a requirement for the overlay to simply display.

`CGPreflightScreenCaptureAccess()` is checked before any capture attempt;
a denial surfaces as `PresenterPrivacyError.screenCapturePermissionDenied`
with a message explaining *why* Veya is asking.

## 11. Performance considerations (8 GB M2 target)

- `AsyncStream(bufferingPolicy: .bufferingNewest(1))` — the capture engine
  never holds more than the newest undelivered frame; a slow consumer
  causes dropped frames, not unbounded memory growth.
- `SCStreamConfiguration.queueDepth = 3` — kept small.
- `SafeShareQuality.efficiency` renders at 0.75× the display's native
  point size and suggests 15 FPS; `.balanced`/`.quality` use native size
  at 30 FPS. 30 FPS is the default, not 60 — see build prompt §5.2.
- No frame is ever written to disk, transcoded, or converted to
  PNG/JPEG. `CMSampleBuffer`s go straight from `SCStream` to
  `AVSampleBufferDisplayLayer`.
- The stream stops immediately (`SCStream.stopCapture()`,
  `AsyncStream.Continuation.finish()`) on `stop()`, on the consumer task
  being cancelled, or if the frame consumer disappears without an
  explicit `stop()` (`onTermination`).

## 12. Single-display workflow

On the primary target machine (MacBook Air M2, one built-in Retina
display), `DisplayManager.builtInDisplay` is the only display and is
always selected by default (`preferredDisplayID == nil` falls back to the
built-in display, then to the first available display). No user action is
required to pick a display.

## 13. Multi-display workflow

`DisplayManager` re-enumerates `NSScreen.screens` on
`NSApplication.didChangeScreenParametersNotification` (connect,
disconnect, resolution/arrangement change). `PresenterPrivacyPreferences
.preferredDisplayID` lets the user pin a specific display; if that display
disconnects, `DisplayManager.selectPreferredDisplay` falls back to the
built-in display, then to the first available one, rather than failing.

## 14. Recursion prevention

Because Safe Share's `SCContentFilter` excludes Veya's entire application
(§7), the Safe Share window is automatically excluded from its own
capture source — there is no special-case code needed to avoid a
"hall of mirrors." This was a deliberate design choice: excluding by
*application* rather than by individual window means every future
Veya-owned window (including ones added in later phases) is safe by
construction, not by remembering to add it to an exclusion list.

## 15. Known limitations

- Direct Private Overlay's verification only tells you what **Veya's own
  local diagnostic** sees in a plain `ScreenCaptureKit` display capture —
  it is not a guarantee about any specific third-party app's capture
  path. Veya never claims otherwise in its UI copy.
- The compatibility test temporarily replaces the overlay's visible
  content with the checkerboard marker for roughly half a second; a user
  watching closely will see it flash.
- Safe Share requires Screen Recording permission; if it's revoked while
  running, the stream stops and the Safe Share window hides (see
  `SafeShareCaptureEngine.handleStreamStopped`).
- `SCScreenshotManager.captureImage` is a single-shot API; the
  compatibility test's 5 sequential captures take roughly half a second
  in total (5 × ~120ms), not instantaneous.
- **`NSWindow.sharingType = .none` is sticky.** Verified empirically: once
  a window's `sharingType` has been set to `.none`, AppKit does not honor
  setting it back to `.readOnly` on that *same* window instance — the
  window server treats the exclusion as one-way. Practically, this means
  switching Presenter Privacy mode away from Direct Private Overlay
  mid-session does not "un-hide" the current overlay window from sharing;
  it stays excluded until a new overlay window is created (Veya creates a
  fresh one per Live Session, so the next session is unaffected). This
  fails safe — a window the user marked private doesn't silently become
  shareable again — but it does mean `PresenterPrivacyManager
  .applyWindowSharingPolicy()`'s restore-to-`.readOnly` call is a genuine
  best-effort attempt, not a guarantee, for windows that already had
  `.none` applied.

## 16. Troubleshooting

| Symptom | Likely cause |
|---|---|
| "Veya needs Screen Recording permission…" | Grant it in System Settings → Privacy & Security → Screen Recording, then retry. |
| Compatibility test always returns `uncertain` | Too many frames failed to capture — check Screen Recording permission and that the overlay window still exists. |
| Safe Share window is blank | Frame stream hasn't started yet, or `isReadyForMoreMediaData` is stuck `false` — check `PresenterPrivacyDebugView` (DEBUG builds) for `framesReceived`/`framesDropped`. |
| Safe Share stops unexpectedly | Permission revoked mid-capture, display disconnected, or a stream error — see `PrivacyLog` output. |

## 17. Manual testing

See `docs/PRESENTER_PRIVACY_TESTING.md`.
