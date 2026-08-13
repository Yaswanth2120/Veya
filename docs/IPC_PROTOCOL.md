# Swift ↔ Python IPC Protocol

Section 6 introduces a Python-driven mocked live-session pipeline,
connected to the existing Swift app over a managed-subprocess JSON Lines
protocol. This document covers the wire protocol, the Swift/Python code
that implements it, lifecycle/restart behavior, privacy rules, fallback
behavior, and troubleshooting.

## 1. Architecture

```text
┌─────────────────────────────── Swift (Veya.app) ───────────────────────────────┐
│                                                                                  │
│  AppCoordinator                                                                 │
│      │ owns                                                                     │
│      ▼                                                                          │
│  PythonIntelligenceCoordinator ── decides Python-driven vs. Swift-fallback      │
│      │                    │                                                     │
│      │ owns               │ owns                                                │
│      ▼                    ▼                                                     │
│  PythonWorkerManager   IPCEventRouter ──► ConversationState / ConversationRepository
│  (Process lifecycle,       ▲                (existing Section 1-5 code, unchanged)
│   state machine,           │ events
│   restart, health)         │
│      │                     │
│      ▼                     │
│  IPCClient (actor) ────────┘
│  (request/response correlation, timeouts, event stream)
│      │
│      ▼
│  ProcessStdioTransport (IPCTransport)
│      │ stdin/stdout pipes
└──────┼───────────────────────────────────────────────────────────────────────┘
       │
       ▼
┌────────────────────────────── Python (core/veya) ───────────────────────────────┐
│                                                                                  │
│  worker.py — Worker                                                             │
│      │ reads stdin, dispatches, writes stdout via one OutputWriter              │
│      ▼                                                                          │
│  ipc/dispatcher.py — Dispatcher + WorkerContext                                 │
│      │                                                                          │
│      ▼                                                                          │
│  mock/live_feed.py — deterministic mocked event sequence                        │
│                                                                                  │
│  stderr ──► structured, metadata-only logs (never IPC)                          │
└───────────────────────────────────────────────────────────────────────────────┘
```

**Transport**: a managed subprocess only (`Process` + 3 pipes). No
localhost HTTP, no sockets, no manually-started server. `IPCTransport` is
a small protocol (`Sources/Veya/Bridge/IPCClient.swift`) so `IPCClient`
and `IPCEventRouter` never talk to `Process`/`Pipe` directly — a future
Unix-domain-socket transport is a new `IPCTransport` conformance, not a
rewrite of the request/response/event machinery.

## 2. Wire format

Newline-delimited JSON (JSON Lines): exactly one JSON object per line.

- **stdout** carries protocol messages only.
- **stderr** carries Python's structured logs only — Swift never parses
  stderr as IPC.
- One dedicated output path (`core/veya/worker.py`'s `OutputWriter`,
  guarded by an `asyncio.Lock`) serializes every stdout write, so a
  response and a concurrently-emitted event can never interleave their
  bytes onto the same or adjacent lines.

All keys are `snake_case` on the wire. Swift DTOs
(`Sources/Veya/Bridge/IPCModels.swift`) use `camelCase` with
`JSONEncoder/Decoder.keyEncodingStrategy = .convertToSnakeCase` /
`.convertFromSnakeCase` — DTO properties spell acronyms as `Id`/`Url`
(never `ID`/`URL`), since Foundation's converter treats each capital
letter as a new word (`sessionID` would round-trip as `session_i_d`).
Python side uses plain `dataclasses` (`core/veya/ipc/protocol.py`).

### Message shapes

```jsonc
// request (Swift → Python)
{"version": 1, "id": "<uuid>", "type": "request", "method": "system.ping", "params": {}}

// response (Python → Swift)
{"version": 1, "id": "<uuid>", "type": "response", "result": {"pong": true}}

// error (Python → Swift)
{"version": 1, "id": "<uuid>", "type": "error", "error": {"code": "INVALID_REQUEST", "message": "..."}}

// event (Python → Swift, unsolicited)
{"version": 1, "type": "event", "event": "transcript.partial", "data": {"session_id": "...", "text": "..."}}
```

`version` is checked on every incoming message on both sides; a mismatch
is a typed protocol error (`UNSUPPORTED_VERSION` in Python,
`_protocol.malformed` diagnostic event in Swift), never a crash.

## 3. V1 RPC methods

| Method | Params | Result |
|---|---|---|
| `system.ping` | `{}` | `{"pong": true}` |
| `system.info` | `{}` | `{"protocol_version": 1, "worker_version": "0.1.0", "pid": 1234}` |
| `worker.shutdown` | `{}` | `{"ok": true}` |
| `session.start` | `{"session_id": "...", "title": "...", "session_type": "..."}` | `{"ok": true}` |
| `session.stop` | `{"session_id": "..."}` | `{"ok": true}` |
| `mock.start_live_feed` | `{"session_id": "..."}` | `{"ok": true}` |
| `mock.stop_live_feed` | `{"session_id": "..."}` | `{"ok": true}` |

`session.start` receives only what Python needs to label its mocked
output. **Swift remains the sole owner of `Session` creation, retrieval,
status transitions, and persistence** — there is no `session.create`,
`session.get`, `session.list`, Python-side session repository, or
Python-side SQLite in this phase.

### Error codes

`INVALID_REQUEST`, `UNSUPPORTED_VERSION`, `METHOD_NOT_FOUND`,
`INVALID_PARAMS`, `SESSION_NOT_FOUND`, `ALREADY_RUNNING`, `NOT_RUNNING`,
`INTERNAL_ERROR` (see `core/veya/ipc/errors.py`). `INTERNAL_ERROR` never
includes the original exception message or traceback — only a generic
safe string; the real error is logged (metadata-only) to stderr.

## 4. Mock pipeline events

Emitted in this fixed order per session by `core/veya/mock/live_feed.py`:

```text
session.started
(per transcript line) transcript.partial* → transcript.final
(on the one canned question line) question.detected → answer.started → answer.delta* → answer.completed
session.ended
```

Example `answer.completed`:

```json
{"version":1,"type":"event","event":"answer.completed","data":{"session_id":"session-uuid","question_id":"question-uuid","question":"Why did the migration take six weeks?","talking_points":["Authentication dependency","Staged rollout","Backward compatibility","Final validation"],"sources":["Migration Notes"]}}
```

### Example JSON Lines exchange

```text
→ {"version":1,"id":"1","type":"request","method":"session.start","params":{"session_id":"s1","title":"Migration Recap","session_type":"meeting"}}
← {"version":1,"id":"1","type":"response","result":{"ok":true}}
→ {"version":1,"id":"2","type":"request","method":"mock.start_live_feed","params":{"session_id":"s1"}}
← {"version":1,"id":"2","type":"response","result":{"ok":true}}
← {"version":1,"type":"event","event":"session.started","data":{"session_id":"s1"}}
← {"version":1,"type":"event","event":"transcript.partial","data":{"session_id":"s1","text":"Thanks everyone"}}
← {"version":1,"type":"event","event":"transcript.final","data":{"session_id":"s1","id":"seg-1","text":"Thanks everyone for joining...","started_at":0.0,"ended_at":0.4,"is_final":true}}
← ... more transcript lines ...
← {"version":1,"type":"event","event":"question.detected","data":{"session_id":"s1","question_id":"q1","text":"So why did the migration take six weeks in total?"}}
← {"version":1,"type":"event","event":"answer.started","data":{"session_id":"s1","question_id":"q1"}}
← {"version":1,"type":"event","event":"answer.delta","data":{"session_id":"s1","question_id":"q1","delta":"Authentication dependency"}}
← ... more deltas ...
← {"version":1,"type":"event","event":"answer.completed","data":{"session_id":"s1","question_id":"q1","question":"...","talking_points":[...],"sources":["Migration Notes"]}}
← ... remaining transcript lines ...
← {"version":1,"type":"event","event":"session.ended","data":{"session_id":"s1"}}
```

## 5. Event routing (Swift)

`IPCEventRouter` (`Sources/Veya/Bridge/IPCEventRouter.swift`) is the only
place that maps worker events onto `ConversationState`:

| Event | Effect |
|---|---|
| `transcript.partial` | `ConversationState.setPartialTranscript` — transient, never persisted |
| `transcript.final` | `ConversationState.ingestTranscriptSegment` — appended + persisted, no auto-detection |
| `question.detected` | `ConversationState.ingestDetectedQuestion` — appended + persisted |
| `answer.started` | `ConversationState.setAnswerGenerating(true)` |
| `answer.delta` | `ConversationState.setPartialAnswer` — transient |
| `answer.completed` | `ConversationState.ingestAnswer` — sets `currentAnswer` + persisted |
| `session.started`/`session.ended` | informational only — Swift already owns session status via `AppCoordinator` |

Events are routed **sequentially, in arrival order** —
`PythonWorkerManager`'s single event-consumer loop `await`s
`IPCEventRouter.route(_:)` directly rather than spawning an unstructured
`Task` per event, which is what guarantees `transcript.final` is applied
before `question.detected`, which is applied before `answer.completed`,
etc.

`ConversationState` deliberately exposes two separate sets of entry
points: `ingest(_:)` (used by the Swift fallback's `MockTranscriptSource`,
runs the *canned* question-detection + answer-generation itself) and
`ingestTranscriptSegment`/`ingestDetectedQuestion`/`ingestAnswer` (used by
`IPCEventRouter`, no auto-detection — Python already decided). Using the
wrong pair would either silently duplicate detection or silently drop it.

## 6. Worker discovery & configuration

`PythonWorkerConfiguration.resolveDefault()`
(`Sources/Veya/Bridge/PythonWorkerConfiguration.swift`):

| Setting | `VEYA_PYTHON_EXECUTABLE` set | unset (dev default) |
|---|---|---|
| Executable | the env var's path, args `["-m", "veya"]` | `/usr/bin/env`, args `["python3", "-m", "veya"]` — resolves `python3` dynamically via `PATH`, same mechanism as a `#!/usr/bin/env python3` shebang |
| Worker directory | `VEYA_WORKER_DIRECTORY` if set | resolved from this source file's compile-time `#filePath`, walking up to the repo root's `core/` — valid only for `swift run`/`swift test` against a checkout of this repository |

Neither default hardcodes `/usr/bin/python3` or a developer-specific
absolute path. See `docs/PYTHON_PACKAGING.md` for the production
replacement.

## 7. Lifecycle & restart

```swift
enum PythonWorkerState: Equatable {
    case stopped, starting, ready, unhealthy, restarting, failed(String)
}
```

- **Startup**: `PythonWorkerManager.start()` launches the process,
  registers the ready continuation *before* `process.run()` (see the code
  comment on `launchProcessAndWaitForReady()` — registering it afterward
  is a real "lost wakeup" race: a fast-starting restarted worker can emit
  `worker.ready` before a separately-scheduled continuation-setup step
  gets a chance to run), and waits for the `worker.ready` event (default
  timeout 10s).
- **Health checking**: `system.ping` runs every 10s (configurable), but
  **only while a Python-driven Live Session is active** —
  `PythonIntelligenceCoordinator` starts it after `mock.start_live_feed`
  succeeds and stops it when the session ends. `.ready` only degrades to
  `.unhealthy` after `maxConsecutivePingFailuresBeforeUnhealthy` (default
  3) *consecutive* failures — one transient failure doesn't flip it.
- **Crash detection & restart**: `Process.terminationHandler` is guarded
  by an integer generation counter incremented on every launch (not by
  comparing `Process` object identity, which testing showed can behave
  inconsistently across rapid restarts) and an idempotency check so a
  duplicate callback for the same generation can't double-handle one
  crash. An unexpected exit fails every pending RPC request
  (`IPCClient.stop()` → `workerUnavailable`), then restarts with
  exponential backoff (`restartBackoffBaseSeconds * 2^(attempt-1)`) up to
  `maxRestartAttempts` (default 3) — **never restarts forever**; beyond
  the bound, state becomes `.failed(reason)`.
- **Graceful shutdown**: `stop()` sets `.stopped` *before* terminating the
  process (so the termination handler recognizes it as expected and
  doesn't trigger a restart), sends `worker.shutdown` best-effort, then
  terminates the process regardless.

## 8. Privacy & logging

**Python** (`core/veya/worker.py`'s `configure_logging`): stderr only,
structured (`%(asctime)s %(levelname)s %(name)s %(message)s`),
metadata-only (method names, ids, counts) — never transcript text,
answer text, prompts, or documents.

**Swift** (`Sources/Veya/Bridge/BridgeLog.swift`, mirrors the existing
`PrivacyLog` pattern): worker startup/ready/exit/restart, request
lifecycle metadata, protocol errors — same content restriction. Captured
stderr is treated as untrusted: Swift retains only a bounded 20-line list
of `worker stderr bytes=<count>` diagnostics
(`PythonWorkerManager.recentStderrLines`), never the original line.

## 9. Fallback behavior

The app is fully usable when Python is unavailable. Deciding logic lives
in `PythonIntelligenceCoordinator.beginLiveSession(state:session:)`:

```text
worker.state == .ready?
  no  → ConversationState.start() (the existing Swift MockTranscriptSource
        timer/canned pipeline) — drivingSource = .swiftFallback
  yes → session.start + mock.start_live_feed RPCs
          succeed → ConversationState.beginPythonDrivenSession(),
                     IPCEventRouter.attach(...) — drivingSource = .pythonWorker
          throw    → same fallback as "no" above
```

`LiveSessionView` shows a small, non-intrusive indicator
("Intelligence: Python worker (mock) ✓" / "Intelligence: Swift fallback —
Python worker unavailable ⚠") so the app never silently claims
Python-backed mock intelligence is active when it isn't. When the worker
*is* healthy, only the Python-driven feed runs — the Swift timer is never
started alongside it (no two competing pipelines).

## 10. Troubleshooting

| Symptom | Likely cause |
|---|---|
| Worker never reaches `.ready` | `python3` not on `PATH`, or `core/veya` not importable from `VEYA_WORKER_DIRECTORY` — inspect worker state and bounded stderr byte-count diagnostics |
| Every Live Session uses the Swift fallback | Worker `.failed` after exceeding `maxRestartAttempts`, or never started — check `PythonWorkerManager.state` |
| `INVALID_PARAMS` on `session.start` | Missing/empty `session_id` — Swift always sends `session.id.uuidString` |
| Events stop mid-session | Worker crashed (see restart backoff above) or `mock.stop_live_feed`/`session.stop` was called early |
| A response never arrives | RPC timeout (`configuration.rpcTimeout`, default 5s) — `IPCClientError.timeout(method:)` |
| Duplicate/garbled stdout lines | Should be impossible — `OutputWriter`'s lock serializes every write; if seen, it's a bug, not expected behavior |

### Manual smoke test

```bash
cd core
python3 -m veya
```

Then paste JSON Lines requests on stdin, e.g.:

```json
{"version":1,"id":"1","type":"request","method":"system.ping","params":{}}
```

See `pipeline.py`'s header comment for why that file is unrelated legacy
experimentation, not the real worker.
