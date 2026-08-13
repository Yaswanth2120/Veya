# Presenter Privacy — Manual Test Matrix

## Primary target

```text
MacBook Air M2
8 GB unified memory
macOS Tahoe 26.x
Built-in Retina Display
```

## Setup

1. `swift build` (or `swift run Veya`).
2. Launch the app → Dashboard → Settings → Presenter Privacy.
3. Toggle "Enable Presenter Privacy" on.

## Checklist

Run through this list on the primary target machine before signing off on
a change to this subsystem. For each row, record ✅/❌ + notes.

| # | Test | Steps | Expected |
|---|---|---|---|
| 1 | Direct overlay visible locally | Select "Private Overlay — Experimental". Start a Live Session. | Overlay renders normally on the physical display, exactly as before this phase. |
| 2 | Veya capture diagnostic | With overlay visible, tap "Run Capture Test". | Status becomes one of Verified / Overlay Detected / Uncertain / Unsupported / Error — never silently stuck on "Testing…". The overlay briefly flashes the checkerboard marker, then returns to normal content. |
| 3 | Safe Share output excludes Veya | Select "Safe Share — Recommended". Start Safe Share on the built-in display. | The "Veya Safe Share" window shows the desktop; no Veya window (Dashboard, Settings, overlay) appears in it. |
| 4 | Safe Share output shows desktop normally | With Safe Share running, open/move a non-Veya app window. | The Safe Share window reflects it within roughly one frame interval. |
| 5 | No recursion in Safe Share | With Safe Share running, move the "Veya Safe Share" window itself around the display it's capturing. | The Safe Share window does not appear inside its own content (no hall-of-mirrors). |
| 6 | Safe Share window can be selected as a shareable window | With Safe Share running, open Zoom/Meet/Teams/Webex/QuickTime/OBS's screen-share picker. | "Veya Safe Share" appears in the window list like any normal window. |
| 7 | Performance at 15 FPS | Set Frame Rate to 15 (Efficiency quality), run Safe Share for 2+ minutes. | Smooth enough for presentation use; no runaway CPU. |
| 8 | Performance at 30 FPS | Set Frame Rate to 30 (Balanced quality), run Safe Share for 2+ minutes. | Smooth; check `PresenterPrivacyDebugView` for `framesDropped` staying low relative to `framesReceived`. |
| 9 | Memory usage | Run Safe Share for 5+ minutes at 30 FPS. Watch `PresenterPrivacyDebugView`'s memory row (DEBUG build) or Activity Monitor. | No continuous upward drift — memory should plateau, not grow unbounded. |
| 10 | Sleep/wake | Start Safe Share, put the Mac to sleep, wake it. | Stream either resumes cleanly or stops gracefully (no crash, no zombie window). |
| 11 | Display changes | Start Safe Share on the built-in display, connect/disconnect an external display. | `DisplayManager` re-enumerates; if the captured display disconnects, Safe Share stops gracefully rather than crashing. |

## Third-party app observation (manual only — no hard-coded assumptions)

For each app below, manually verify Direct Private Overlay's *actual*
on-screen behavior when that app's screen-share/record feature is active,
and record the result. Veya's own `CaptureCompatibilityTester` result is
Veya's local diagnostic only — it is not a substitute for checking real
apps, and this project makes no hard-coded compatibility claims about any
of them.

| Application | Version | Share Mode | macOS Version | Result | Date | Notes |
|---|---|---|---|---|---|---|
| Zoom | | | | | | |
| Google Meet | | | | | | |
| Microsoft Teams | | | | | | |
| Webex | | | | | | |
| QuickTime Player (screen recording) | | | | | | |
| OBS Studio | | | | | | |

Fill in one row per test run. Keep historical rows rather than
overwriting them — compatibility can change across app/OS updates.
