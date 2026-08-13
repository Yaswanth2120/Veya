# Real-Time Transcription (Section 7)

This document covers the real microphone capture + local Whisper
transcription pipeline added in Section 7, layered on top of Section 6's
Swift↔Python worker bridge (see `docs/IPC_PROTOCOL.md` for the base
protocol). It replaces the Python mock feed with real transcribed speech
when local dependencies are available, and falls back cleanly when they
aren't — the mock feed and the Swift fallback pipeline from earlier
sections are both still fully intact.

## Architecture

```text
┌─────────────────────────── Swift native host ───────────────────────────┐
│                                                                          │
│  MicrophoneAudioCapture (AVAudioEngine)                                 │
│    mic tap → AVAudioConverter → mono 16kHz s16le → bounded AsyncStream  │
│         │                                                               │
│         ▼                                                               │
│  AudioChunkSender (actor)                                               │
│    bounded concurrent in-flight transcription.audio_chunk RPCs         │
│    (maxInFlight, default 2) — oversized/excess chunks dropped+counted  │
│         │  JSON Lines over the worker's stdin (see IPC_PROTOCOL.md)     │
│         ▼                                                               │
│  PythonIntelligenceCoordinator                                          │
│    picks exactly one driving source per session:                       │
│    realTranscription > pythonWorker (mock) > swiftFallback              │
│         │                                                               │
│         ▼                                                               │
│  IPCEventRouter → ConversationState → ConversationRepository (GRDB)     │
│                                                                          │
└──────────────────────────────────┬───────────────────────────────────--┘
                                    │ managed subprocess (stdin/stdout/stderr)
┌───────────────────────────────── core/veya ──────────────────────────────┐
│                                                                          │
│  ipc/dispatcher.py                                                      │
│    transcription.start / transcription.audio_chunk / transcription.stop │
│         │                                                               │
│         ▼                                                               │
│  transcription/session.py — TranscriptionSession                        │
│    buffers chunks into a rolling window (session.py + rolling_buffer.py)│
│    background asyncio.Task transcribes windows off the dispatch path    │
│         │                                                               │
│         ▼                                                               │
│  transcription/engine.py — WhisperCliTranscriptionEngine                │
│    writes each window to a temp WAV, invokes a local whisper.cpp binary,│
│    deletes the temp file immediately after (never persisted)            │
│         │                                                               │
│         ▼                                                               │
│  transcription/overlap.py — dedupe_overlap                              │
│    strips the repeated words from the retained overlap tail             │
│         │  transcript.final event                                       │
│         ▼                                                               │
│  worker.py's OutputWriter → stdout (JSON Lines, one line per message)   │
│                                                                          │
└────────────────────────────────────────────────────────────────────────┘
```

Swift owns native microphone access end-to-end (permission, capture,
format conversion, chunking, transport to the worker). Python owns
transcription processing (buffering, the local Whisper invocation,
dedup). Neither side does the other's job — same separation of
responsibility Section 6 established for the mock pipeline.

## Audio format and chunking contract

- **Capture**: microphone only (no system audio) via `AVAudioEngine`'s
  input node tap.
- **Wire format**: mono, 16kHz, signed 16-bit little-endian PCM
  (`pcm_s16le`). `MicrophoneAudioCapture` converts from whatever format the
  input hardware reports via `AVAudioConverter`.
- **Chunk size**: `MicrophoneAudioCapture`'s default is one chunk every
  0.5s (~16,000 bytes of raw PCM per chunk before base64 encoding).
- **Maximum chunk size**: `AudioIPCLimits.maxChunkBytes` (Swift) /
  `MAX_AUDIO_CHUNK_BYTES` (Python) — 65,536 raw PCM bytes, enforced
  independently on both sides. An oversized chunk is dropped client-side
  (never sent) and would additionally be rejected server-side
  (`INVALID_PARAMS`) if it ever were.
- **Sequencing**: each chunk carries a strictly increasing per-session
  `sequence` integer. The Python worker rejects out-of-order or duplicate
  sequences (`INVALID_PARAMS`) — see `TranscriptionSession._validate_sequence`.
- **Backpressure**: `AudioChunkSender` bounds concurrent in-flight
  `transcription.audio_chunk` RPCs (`maxInFlight`, default 2). Once that
  bound is reached, new chunks are dropped and counted — never queued
  unboundedly, and capture is never blocked waiting for Python.
  `MicrophoneAudioCapture` itself also uses a bounded `AsyncStream`
  (`bufferingNewest`), so a slow consumer drops the newest chunk rather
  than growing memory without limit. Both drop counts are tracked as
  metadata (`droppedChunkCount`) only — never logged with content.
- **No per-chunk synchronous wait**: sending a chunk (`AudioChunkSender.send`)
  returns immediately; the RPC's response is awaited in a background task,
  not on the capture path.

## IPC additions

Three new RPC methods, added to the existing versioned JSON Lines
protocol (see `docs/IPC_PROTOCOL.md` for the base envelope shapes) —
nothing about the transport or existing methods changed.

```text
transcription.start        Swift → Python   begin a transcription session
transcription.audio_chunk  Swift → Python   one chunk of PCM audio
transcription.stop         Swift → Python   end a transcription session
```

**`transcription.start`**
```json
{"version":1,"id":"...","type":"request","method":"transcription.start",
 "params":{"session_id":"...","sample_rate_hz":16000,"channels":1,"encoding":"pcm_s16le"}}
```
Only `channels: 1` and `encoding: "pcm_s16le"` are accepted (`INVALID_PARAMS`
otherwise). If local Whisper isn't configured/available, this returns a
`TRANSCRIPTION_UNAVAILABLE` error instead of `{"ok": true}` — see
[Fallback behavior](#fallback-behavior).

**`transcription.audio_chunk`**
```json
{"version":1,"id":"...","type":"request","method":"transcription.audio_chunk",
 "params":{"session_id":"...","sequence":42,"started_at_seconds":12.4,
           "duration_seconds":0.5,"audio_base64":"..."}}
```
Returns `{"ok": true}` as soon as the chunk is validated and buffered —
*not* once it's been transcribed. Whether it completed a rolling window is
invisible at the RPC level; the resulting `transcript.final` event (if
any) arrives later, asynchronously.

**`transcription.stop`**
```json
{"version":1,"id":"...","type":"request","method":"transcription.stop","params":{"session_id":"..."}}
```
Flushes any trailing buffered audio (transcribing it only if it's more
than just the retained overlap tail — see `TranscriptionSession.close`)
before returning.

**`transcript.final` event** (unchanged shape from Section 6 — the same
event mock/real pipelines both use):
```json
{"version":1,"type":"event","event":"transcript.final",
 "data":{"session_id":"...","id":"...","text":"...","started_at":12.4,"ended_at":16.4,"is_final":true}}
```

Every message is still one standalone JSON line; stdout is still
protocol-only; Python logs are still stderr-only.

## Rolling-window transcription (why, not true streaming)

`whisper.cpp`'s CLI takes one whole audio file per invocation — it has no
incremental/streaming ingest mode. `core/veya/transcription/rolling_buffer.py`
works around that: incoming chunks accumulate until a full window
(`window_seconds`, default 4.0s) is buffered, at which point that window
is transcribed, and an `overlap_seconds` tail (default 1.0s) is kept so
the next window starts slightly before the previous one ended — otherwise
a word spoken right at the cut point could be lost to either side.
`core/veya/transcription/overlap.py`'s `dedupe_overlap` then strips the
resulting repeated words from the new window's text before it's emitted.

**This means only `transcript.final` events are emitted from the real
pipeline — no `transcript.partial` events.** The mock feed (Section 6)
still emits partials; real transcription does not, because there is no
genuine partial result available without true streaming support from the
underlying engine. This is a known, deliberate limitation, not an
oversight — building real incremental partials would require replacing
the whisper.cpp CLI with a streaming-capable API, out of scope here.

## Measured latency (real, not estimated)

Measured on this development machine (Apple Silicon, `ggml-base.en.bin`
model) by transcribing a real 4-second window of speech
(`whisper.cpp/samples/jfk.wav`) through the actual
`WhisperCliTranscriptionEngine.transcribe_pcm`:

```
4s window transcribed in ~0.48s wall time
text: "And so my fellow Americans asked" (truncated to the window)
```

This is **one measurement on one machine with one model**, not a
guarantee — it does not account for: a larger/slower model, sustained
concurrent load from other work the worker is doing, thermal throttling,
or different hardware. With the default 4s window and ~0.5s measured
transcription time, effective end-to-end latency for a completed window
is roughly **window duration + processing time** (~4.5s from when speech
starts to when its transcript arrives) — this is fundamentally a
batched-window design, not low-latency streaming; do not represent it as
such.

## Local Whisper / whisper.cpp setup

Real transcription requires a locally built `whisper.cpp` (or compatible)
CLI binary and a `.bin` model file. This repository does not bundle
either — see `docs/PYTHON_PACKAGING.md` for the (unimplemented) plan to
bundle a runtime for release builds. For development:

```sh
# from a whisper.cpp checkout
cmake -B build && cmake --build build --config Release -j
./models/download-ggml-model.sh base.en    # or any other ggml model
```

Then point the worker at both:

```sh
export VEYA_WHISPER_BIN=/path/to/whisper.cpp/build/bin/whisper-cli
export VEYA_WHISPER_MODEL=/path/to/whisper.cpp/models/ggml-base.en.bin
```

No default/hardcoded path is ever assumed — if either variable is unset,
or points at a missing/non-executable file, `transcription.start` returns
`TRANSCRIPTION_UNAVAILABLE` and the app falls back automatically (below).
`Process` (Swift) inherits its parent's environment by default, so these
variables set in the shell that launches the app (or `swift test`) reach
the Python worker subprocess unchanged.

### Real-time streaming engine (Section 15)

The CLI path above only ever produces a transcript once a whole ~4s
window completes — genuinely real-time behavior needs a persistent,
incremental engine instead. `packaging/whisper-stream-stdin/` (tracked in
*this* repo, since `whisper.cpp/` itself is gitignored — see below) is a
small additional whisper.cpp example: `examples/stream/stream.cpp`'s own
real-time sliding-window algorithm, with its SDL2 live-microphone capture
replaced by a stdin PCM reader, so Swift stays the app's one and only
microphone owner. It emits JSON-Lines partial/final hypotheses to stdout
as it re-decodes a bounded, sliding trailing window roughly once a
second — a real persistent process, not a batch CLI invoked repeatedly.

Install and build it into your local whisper.cpp checkout:

```sh
packaging/install_streaming_asr_source.sh          # defaults to ./whisper.cpp
cmake --build whisper.cpp/build --target whisper-stream-stdin
```

`veya/transcription/streaming_provider.py`'s `resolve_streaming_binary_path()`
looks for a `whisper-stream-stdin` binary next to `VEYA_WHISPER_BIN`
automatically (or `VEYA_WHISPER_STREAM_BIN` explicitly) — no extra env
var is required if both binaries live in the same directory, which is
exactly how `packaging/build_app.sh` bundles them. When the streaming
binary is available, `transcription.start` uses it as the primary engine
(`asr_provider: "streaming"` in its response); otherwise it falls back to
the batch CLI path above, reported honestly as `asr_provider:
"degraded_batch"` — never silently presented as the same thing.

## Environment configuration

| Variable              | Meaning                                    | Required for real transcription |
|------------------------|---------------------------------------------|----------------------------------|
| `VEYA_WHISPER_BIN`     | Path to a local whisper.cpp-compatible CLI  | Yes |
| `VEYA_WHISPER_MODEL`   | Path to a `.bin` ggml model file            | Yes |
| `VEYA_PYTHON_EXECUTABLE` / `VEYA_WORKER_DIRECTORY` | Worker discovery (Section 6, unchanged) | No |

## Fallback behavior

Selection order, decided fresh at the start of every Live Session by
`PythonIntelligenceCoordinator.beginLiveSession`:

```text
1. Real transcription   worker .ready AND an AudioCapturing is configured
                         AND microphone permission is authorized AND
                         Python's transcription.start succeeds
2. Python mock feed     worker .ready, but (1) wasn't available for any
                         reason — no audioCapture configured, permission
                         not authorized, or transcription.start failed
3. Swift fallback       the worker itself is unavailable, or
                         session.start itself failed
```

Only one pipeline ever drives a `ConversationState` at a time — never the
mock feed and real transcription together, never the Swift timer running
alongside either. If the worker crashes *during* an active real-transcription
or mock-feed session, `PythonIntelligenceCoordinator` observes the state
transition (`PythonWorkerManager.stateChangeHandler`, from Section 6's
mid-session-crash fix) and switches the live session to the Swift fallback
timer — capture is stopped and `transcription.stop`/`mock.stop_live_feed`
are best-effort attempted first.

**UI indicator** (`PythonIntelligenceCoordinator.liveSessionIndicatorText`,
shown in `LiveSessionView`) — exactly one of:

```text
Listening — live transcription
Demo mode — Python mock intelligence
Demo mode — Swift fallback
Microphone permission required
Transcription setup unavailable
```

The app never claims real transcription (or Python-backed mock
intelligence) is active when the driving source is actually something
else — this was already a hard requirement from Section 6 and applies
identically here.

**Production wiring**: real capture is opted into explicitly in
`AppDelegate.swift` (`PythonIntelligenceCoordinator(audioCapture:
MicrophoneAudioCapture())`), not as a default anywhere else —
`PythonIntelligenceCoordinator()`'s own default is `audioCapture: nil`
(real transcription disabled), so constructing a coordinator in tests, or
anywhere other than the real app launch path, never touches AVFoundation
or prompts for microphone permission.

## Question detection / answer generation are unaffected

Real transcript text is only ever routed through
`ConversationState.ingestTranscriptSegment(_:)` — the same granular,
detection-free entry point Section 6's `IPCEventRouter` already used for
mock-feed transcript events. Real transcription never triggers the Swift
fallback's canned keyword-based question detection or the canned answer
generator; those remain exclusively the Swift-fallback pipeline's
behavior, per this section's explicit non-goals.

## Privacy

- Raw microphone audio is never persisted. `WhisperCliTranscriptionEngine`
  writes each rolling window to a `tempfile.TemporaryDirectory()`-backed
  WAV file that is deleted immediately after the whisper subprocess exits
  (success or failure) — this is transient processing storage, not
  persistence, and the path is never logged.
- Transcript persistence is unchanged from Section 6: Swift/GRDB is the
  sole persistence authority (`ConversationRepository`), reached the same
  way mock-feed transcripts always were, via `IPCEventRouter`.
- Logging stays metadata-only everywhere in this section: `BridgeLog`
  entries mention byte counts, chunk sequence numbers, and error *types*,
  never raw audio, transcript text, or file paths.
  `TranscriptionSession`'s background transcription failures are logged
  as `"Unhandled %s while transcribing a window" % type(exc).__name__` —
  never `str(exc)` — mirroring the Section 6 review fix to
  `Dispatcher.dispatch`.
- No cloud transcription APIs are called anywhere in this pipeline; the
  only external process invoked is the local `whisper.cpp`-style CLI.

## Known limitations

- No true streaming partials from real transcription (see
  [Rolling-window transcription](#rolling-window-transcription-why-not-true-streaming)
  above) — only `transcript.final`, once per completed ~4s window.
- Effective latency is on the order of several seconds per window, not
  sub-second — see [Measured latency](#measured-latency-real-not-estimated).
- `NSMicrophoneUsageDescription` cannot currently be set: this project is
  still a plain SPM executable (no `Info.plist`/app bundle — same gap
  `docs/PYTHON_PACKAGING.md` notes for the Python runtime). Real
  microphone permission prompting has not been exercised against a proper
  `.app` bundle as a result; see the manual verification checklist below.
- `dedupe_overlap` is a plain word-level prefix/suffix match, not a real
  text-alignment algorithm — it can occasionally under- or over-strip if
  Whisper's wording differs slightly between two overlapping windows
  (e.g. due to punctuation/capitalization differences at the boundary).
- Overlap-tail flush on `transcription.stop` only transcribes if genuinely
  new audio arrived since the last completed window, to avoid a redundant
  Whisper call for content that's already covered — but very short
  trailing speech (less than the configured overlap) can still be lost at
  session end if it never accumulated a full window.

## What was and wasn't actually verified

- **Verified for real**: the Python-side transcription pipeline
  (rolling buffer, overlap dedup, dispatcher validation, sequencing) via
  71 automated unit/integration tests with a fake engine (no real Whisper
  required) plus a manual, opt-in end-to-end test
  (`Tests/VeyaTests/PythonWorkerManagerIntegrationTests.swift`'s
  `RealTranscriptionIntegrationTests` suite, and
  `core/tests/test_whisper_smoke.py`) that ran the **real** `whisper.cpp`
  CLI against real speech audio through the full Swift → Python → Whisper
  → event-routing → `ConversationState` path, and confirmed real
  transcript text was produced and persisted. This proves the mechanism
  works end-to-end on this machine with this model — see
  [Measured latency](#measured-latency-real-not-estimated).
- **Not verified**: real hardware microphone capture. This development
  environment has no audio input device or interactive GUI session, so
  `MicrophoneAudioCapture`'s `AVAudioEngine`/`AVAudioConverter` code has
  been written carefully and compiles/type-checks correctly, but has
  **not** been run against real hardware, and no real microphone
  permission prompt has been exercised. Do not treat this as confirmed
  working until the manual checklist below has actually been run on a
  Mac with a microphone.

## Manual verification checklist (not yet performed)

Requires a Mac with a working microphone, a built `whisper.cpp` binary +
model, and (ideally) a proper `.app` bundle so `NSMicrophoneUsageDescription`
can be set — none of which this environment has. To actually verify:

1. Set `VEYA_WHISPER_BIN`/`VEYA_WHISPER_MODEL` to a real local build.
2. Launch the app (`swift run`), start a Live Session.
3. Confirm a real macOS microphone permission prompt appears; grant it.
4. Confirm the indicator reads "Listening — live transcription".
5. Speak a full sentence; confirm real transcript text appears within a
   few seconds (not the canned mock script).
5a. End that session, then start a **second** Live Session and speak
    again; confirm real transcript text still appears. `AppDelegate`
    injects one `MicrophoneAudioCapture` instance for the app's entire
    lifetime, so this specifically catches stream-reuse regressions
    (`chunks()`'s cached `AsyncStream` must be reset on `stop()`, not just
    finished, or the second session silently gets a dead, already-finished
    stream and receives no audio).
6. Deny microphone permission (or revoke it in System Settings and
   restart) and start a new session; confirm the indicator reads
   "Microphone permission required" and the session still runs (Python
   mock feed).
7. Unset `VEYA_WHISPER_BIN`/`VEYA_WHISPER_MODEL` and start a session with
   permission granted; confirm the indicator reads "Transcription setup
   unavailable" and the session still runs (Python mock feed).
8. End a real-transcription session; confirm the microphone indicator
   (macOS menu bar) turns off promptly.

## Troubleshooting

- **`transcription.start` always fails with `TRANSCRIPTION_UNAVAILABLE`**:
  check `VEYA_WHISPER_BIN`/`VEYA_WHISPER_MODEL` are set in the environment
  the app/worker actually launches from (a `.app` launched from
  Finder/Dock has a much smaller `PATH`/environment than a Terminal shell
  — see `docs/PYTHON_PACKAGING.md` for the same class of issue), and that
  both paths exist and the binary is executable.
- **No `transcript.final` events ever arrive despite `.realTranscription`
  being active**: confirm chunks are actually reaching Python — check
  `AudioChunkSender`'s `droppedChunkCount` (bounded-queue drops) and
  `failedChunkCount` (RPC failures); a very bursty/non-real-time chunk
  delivery pattern (e.g. in a test that doesn't pace chunks) can exceed
  `maxInFlight` and drop most chunks — real capture paced at
  `chunkDuration` real-time intervals does not hit this in practice.
  Also confirm enough audio has accumulated for one full rolling window
  (default 4s) — nothing is transcribed before that.
  See `Tests/VeyaTests/PythonWorkerManagerIntegrationTests.swift`'s
  `RealTranscriptionIntegrationTests` test for a comparable working example.
- **A worker crash mid-session doesn't seem to fall back**: confirm
  `PythonWorkerManager.stateChangeHandler` is actually wired (it's set in
  `PythonIntelligenceCoordinator.init`) — see `docs/IPC_PROTOCOL.md` §7 for
  the underlying Section 6 mechanism this section reuses unchanged.
