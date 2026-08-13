# Veya

Veya is a native macOS real-time conversation copilot. It listens locally (real microphone capture + local Whisper transcription), detects questions, and generates grounded answers with a local LLM (Ollama) — with a coding copilot, a system-design copilot, session reports, and durable user-approved memory, all running on-device. There are no cloud APIs anywhere in this app: transcription, retrieval, embeddings, and generation are all local by default, and the loopback-only policy is enforced in code, not just by convention.

Swift/SwiftUI (AppKit-hosted) owns the UI, all session/transcript/question/answer persistence (GRDB/SQLite), and process lifecycle. A local Python worker (`core/veya`) is spawned as a subprocess and communicates over a versioned JSON Lines protocol on stdin/stdout; it owns only *derived* data — transcription, LLM calls, the knowledge/embedding index, coding/design workspace state, and durable approved memory — never the source-of-truth session data.

## What's implemented

### Core session & UI (Sections 1–6)
- Session creation, live session view, overlay window, dashboard, previous-sessions history, personal profile, settings.
- GRDB-backed persistence: `Session`, `SessionDocument`, `TranscriptSegment`, `DetectedQuestion`, `CopilotAnswer`, `UserProfile`, `SessionReport` (versioned migrations in `DatabaseManager`).
- A versioned JSON Lines IPC protocol (`docs/IPC_PROTOCOL.md`) between Swift and the Python worker, with a bounded restart/health-check lifecycle (`PythonWorkerManager`) and a three-way fallback order (real transcription → Python mock feed → Swift fallback) so the app is never left with no active pipeline.
- Presenter Privacy / Safe Share: a capture-compatibility tester, a screen-capture engine that excludes the overlay from what's shared, and a settings UI to configure it (`docs/PRESENTER_PRIVACY.md`).

### Real transcription (Section 7)
- `MicrophoneAudioCapture` (AVFoundation) streams mono 16kHz PCM chunks over IPC; the Python worker buffers them in a rolling window and transcribes with a local `whisper.cpp` CLI binary (`docs/REALTIME_TRANSCRIPTION.md`). No raw audio is ever persisted.

### Question detection & answer generation (Section 8)
- Local question detection + an `LLMProvider` abstraction backed by Ollama only. `OllamaProvider` enforces a **loopback-only** policy (`VEYA_OLLAMA_URL` must resolve to localhost unless `VEYA_OLLAMA_ALLOW_REMOTE=1` is explicitly set) — see `docs/QUESTION_AND_ANSWER_INTELLIGENCE.md`.

### Document ingestion & grounded answers (Section 9)
- `.txt/.md/.pdf/.docx` ingestion, chunking, local embeddings (Ollama), and a pure-Python cosine-similarity SQLite vector store — grounded answers cite real retrieved chunks, never fabricated sources (`docs/KNOWLEDGE_RETRIEVAL.md`).

### Real-time turn detection & low-latency answering (Section 14)
Replaces the old "judge every ~4s Whisper window independently" question detection, which failed on any question spoken across more than one window and couldn't recognize statement-form prompts like "Tell me about yourself."
- **Local VAD** (`core/veya/transcription/turn_detection.py`): a chunk-level, energy-based (RMS amplitude) speech/silence state machine — `speech_started`/`speech_continuing`/`silence_candidate`/`turn_finalized` — running independently of and faster than Whisper's window cadence. Configurable silence duration (default 1.2s — long enough that a normal mid-sentence pause doesn't end a turn), a minimum speech duration before silence can finalize anything, and a max-turn-duration safety cap. A trailing open turn is force-finalized at session close so nothing spoken right at the end is dropped.
- **`TurnAssembler`** (`core/veya/conversation/turn_assembler.py`): coalesces `transcript.final` fragments spanning multiple Whisper windows into one complete spoken turn, deduplicating window overlap the same way transcription already does, and only finalizes on a real endpoint (a VAD boundary, explicit session stop, or the max-duration cap) — never on an individual fragment.
- **Two-stage semantic classification** (`core/veya/conversation/semantic_classifier.py`): a fast deterministic gate (extended to recognize statement-form prompts — "Tell me about yourself," "Walk me through your resume," "Q1, explain...") decides clear cases immediately; only genuinely ambiguous turns make a local Ollama call for structured JSON classification (`is_answer_request`/`confidence`/`normalized_question`/`reason_category`), safely parsed and validated, falling back to the deterministic gate's own verdict if Ollama is unavailable, slow, or returns malformed output — never crashes transcription.
- Swift gets three new events (`turn.state`, `question.classifying`, `question.rejected`) driving a real six-state Live Session indicator — Listening / Transcribing / Waiting for speaker to finish / Understanding question / Generating answer / Local AI unavailable — plus a prominent answer panel (previously never rendered at all) with partial vs. finalized transcript shown separately, and a Check Local AI action. The floating overlay now defaults to a screen corner instead of dead-center on first launch, so it doesn't cover the main answer panel.
- **Real streaming preview**: a short (default 2s) trailing window is re-transcribed roughly once a second while speech is ongoing, emitting real `transcript.partial` events from the real engine (not the mock feed) — never persisted, never fed into turn assembly or question detection, purely a live preview Swift replaces on the next partial or clears on the next `transcript.final` (`core/veya/transcription/session.py`).
- **Mid-sentence interrogatives now reach the ambiguous band**: an earlier review found the deterministic gate's default scoring could only land on {0, 0.2, 0.6, 0.65, 0.75} — none of which fell inside the configured semantic-classification band `[0.35, 0.6)` — so real ambiguous prompts were always decided by the heuristic gate and never actually reached Ollama. A new mid-sentence-interrogative signal (an interrogative word present but not leading, e.g. "the caching layer, how does that scale") now scores in-band without double-counting when the sentence also leads with an interrogative.
- **Honest, multi-leg latency measurements** (previously a single number that implicitly excluded real audio→transcript latency — withdrawn):
  - Real audio → first `transcript.partial`: **~955ms** (real `jfk.wav` sample through real `whisper.cpp`, real-time chunk pacing, `core/tests/test_realtime_pipeline_latency_smoke.py`).
  - Real audio → first `transcript.final`: **~3958ms** — the dominant cost, bounded below by the ~4s Whisper rolling-window size itself; a VAD turn boundary can fire earlier (e.g. ~1.5s) but the transcript text for that turn does not exist until the window completes.
  - Finalized turn → `question.detected`: **~0.2ms** — pure local computation, but this leg only starts once a transcript exists, so it is dwarfed by the audio→transcript leg above; text is injected directly in this measurement, not spoken (`core/tests/test_ollama_smoke.py`).
  - `question.detected` → first `answer.delta`: **~2.0s** (real local Ollama generation, `qwen3:1.7b`).
  - All figures measured on this development machine; environment/hardware-specific, not a benchmark claim. **End-to-end microphone → first answer content is dominated by the ~4s transcription window, not by classification or generation** — "real-time / low-latency" should be read as "faster turn *boundary* detection than before," not as sub-second answer latency.
- **Known limitation, not fixed**: there is no diarization or speaker-role model. VAD is energy-based (speech/silence only) and cannot tell the interviewer's speech from the candidate's own speech — Veya can still attempt to answer the candidate's own speech if it resembles a prompt. This is an acknowledged gap, not a silent one.

### Coding Copilot (Section 11)
- Python owns versioned coding-workspace state (`core/veya/coding/workspace.py`): every file has a version and a bounded follow-up history that survives edits/applies.
- RPCs: `coding.list_files`, `coding.upsert_file`, `coding.apply_edits`, `coding.followup`, `coding.debug`, `coding.generate_tests`, `coding.explain`, `coding.analyze`, `coding.run`.
- Every LLM proposal returns `base_version`, an explanation, minimal non-overlapping edits, proposed tests, and a complexity estimate. Swift renders a diff preview with explicit **Apply / Reject / Regenerate** actions; rejecting never mutates the workspace, and applying against a stale version is safely rejected.
- Local execution (`coding.run`) is opt-in (`VEYA_CODE_EXECUTION_ENABLED=1`), time- and output-bounded, runs in an isolated temp directory with an empty environment, never invokes a shell, and is explicitly documented as *not* a security sandbox.

### System Design Copilot (Section 12)
- Python owns durable `ArchitectureState` (nodes, edges, decisions, assumptions, requirements, risks, trade-offs, action items) per session, versioned the same way as coding workspaces.
- RPCs: `design.get`, `design.replace`, `design.followup` (LLM-evolves state in place — untouched nodes/decisions survive), `design.export` (Mermaid / JSON / Markdown / PDF — the PDF is a small hand-rolled generator, no third-party dependency). JSON is authoritative; every other format is a derived export.
- Swift renders an editable node/edge graph and an export UI.

### Session Reports & Durable Memory (Section 13)
- `session.analyze` synthesizes a report (summary, topics, decisions, action items, unanswered questions, preparation gaps) from transcript/question/answer data Swift sends narrowly for that one call. The report is saved durably in Python (`ReportStore`, JSON-per-session under `~/Library/Application Support/Veya/SessionReports/`) *and* persisted by Swift/GRDB — `session.report.get` survives a full worker-process restart, verified with a real-subprocess test that stops one worker and fetches the report from a completely independent second worker process.
- Durable, user-approved memory (`core/veya/memory/store.py`, local SQLite under the managed application-support root, never remote): candidates are proposed (never silently saved) by `session.analyze`, require explicit `memory.approve`/`memory.reject`, and only approved memory is retrievable in future sessions' prompts. Swift's `MemoryReviewView` (Settings → Memory) is the approve/reject/edit/delete UI.

### Packaging
- `packaging/build_app.sh` builds an actual unsigned, installable `Veya.app`: a release Swift binary, a bundled Python venv with `pypdf` installed, and a copy of the `core/veya` worker package. `PythonWorkerConfiguration` resolves the bundled runtime from `Bundle.main` at launch (falling back to `/usr/bin/env python3` + a checkout-relative path only in dev), so the packaged app never depends on the end user's own Python install. `VEYA_PYTHON_EXECUTABLE`/`VEYA_WORKER_DIRECTORY` still override both for development.
- **First-launch Whisper model manager** (`WhisperModelManager`): downloads the configured model over HTTPS, verifies its SHA-256 before it's ever trusted, writes atomically (temp file + rename), never re-downloads a valid cached model, and re-downloads rather than trusts a corrupted cache. The manifest (`packaging/whisper_model_manifest.json`) is a real per-architecture (arm64/x86_64) dictionary — Apple Silicon and Intel resolve to genuinely different models — and has no hard-coded/guessed hash anywhere: every value was produced by actually downloading the referenced release asset and hashing it with `shasum -a 256`. Verified for real: a clean launch of the packaged app downloaded the real model over HTTPS, verified its hash, and the exact downloaded file was fed to `whisper-cli` and produced a correct transcript of the JFK sample.

### Local AI status, session deletion, and transcript cleanliness
A round of hands-on GUI testing surfaced product-level gaps real subprocess/unit tests alone didn't catch — fixed:
- **Local AI status panel** (Settings → Local AI): a new `system.llm_status` RPC reports whether Ollama is reachable, which model is configured, whether that exact model is installed, and what is — with actionable repair text (e.g. `ollama pull <model>`) instead of a silent "No local LLM was available." The configured model is now a Settings-level choice (`LocalAIPreferencesStore`), applied to the worker immediately via a restart, rather than a hard-coded default with no way to change it from the app.
- **Session deletion**: Previous Sessions now has a delete action with a confirmation dialog. It calls a new `session.delete_data` RPC that cleans up every Python-owned artifact for that session (coding workspace, architecture state, knowledge-index documents/chunks, never-approved memory candidates, cached report — approved memory deliberately outlives the session it came from), removes the on-disk document copies, then deletes the GRDB `Session` row, which cascades to every related table.
- **Transcript cleanliness**: whisper.cpp's non-speech tags (`[BLANK_AUDIO]`, `(silence)`, etc.) are now filtered at the transcription source in Python — they're never emitted as a `transcript.final` event — and Previous Sessions also filters them at display time, so sessions transcribed before this fix don't still show raw tags.

## Repository layout

```
Sources/Veya/          Swift app (SwiftUI + AppKit hosting, GRDB persistence, IPC bridge)
Tests/VeyaTests/        Swift unit + real-subprocess integration tests (swift-testing)
core/veya/               Python worker: transcription, LLM, knowledge/RAG, coding, design, memory, IPC dispatcher
core/tests/               Python unit tests (unittest)
docs/                     Design/behavior docs for each subsystem (IPC protocol, privacy, packaging, etc.)
packaging/                .app build script, Info.plist, Whisper model manifest
```

## Building & running (development)

```sh
# Python worker environment
python3 -m pip install --break-system-packages -e 'core[dev]'
python3 -m unittest discover -s core/tests -t core

# Swift app
swift build
./run-tests.sh          # swift test, with the flags this toolchain needs for swift-testing
swift run Veya
```

The dev-time worker launches via `/usr/bin/env python3 -m veya` with `core/` as its working directory — no packaging step is required to develop against it. Real local integrations (Whisper, Ollama) are picked up automatically when configured:

- `VEYA_WHISPER_BIN` / `VEYA_WHISPER_MODEL` — a local `whisper.cpp` CLI binary + model for real transcription.
- `VEYA_OLLAMA_URL` / `VEYA_OLLAMA_MODEL` — a local Ollama instance for real question answering, coding/design assistance, and report synthesis. Must be loopback unless `VEYA_OLLAMA_ALLOW_REMOTE=1` is set.

Without either configured, the app still runs end-to-end using the Swift/Python mock pipeline.

## Building an installable app

```sh
packaging/build_app.sh
open .build/package/Veya.app     # or right-click > Open — it's unsigned
```

## Privacy & security posture

- No cloud APIs: transcription, embeddings, and generation are all local; Ollama access is loopback-restricted by default.
- Swift/GRDB is the sole persistence authority for session, transcript, question, and answer data; Python only ever sees narrowly-scoped copies for one RPC call and owns only its own derived data (transcription output, the knowledge index, coding/design workspace state, durable approved memory).
- Worker stderr is metadata-only (message type/byte counts) — never transcript, prompt, model output, or document content. This is enforced in code (e.g. suppressing a third-party PDF parser's own loggers) and covered by regression tests, not just policy.
- Document/model paths are always validated to resolve beneath an app-managed directory — never an arbitrary user-supplied path.
- Local code execution is opt-in, bounded, and explicitly not a sandbox.
- See `docs/SECURITY_AND_PRIVACY.md`, `docs/PRESENTER_PRIVACY.md`, and `docs/IPC_PROTOCOL.md` for the full detail behind each of these.

## Testing

- **Python**: 321 tests (`python3 -m unittest discover -s core/tests -t core`), 3 skipped by default (they require a real local Ollama).
- **Swift**: 167 tests across 23 suites (`./run-tests.sh`), including a real-subprocess integration suite that launches the actual `core/veya` worker (no mocks) to verify IPC, coding/design/report/memory RPCs, crash-restart recovery, session deletion cascade, Local AI status, turn-state routing, and knowledge ingestion end-to-end.
- Several suites opportunistically exercise real local Whisper/Ollama when `VEYA_WHISPER_BIN`/`VEYA_WHISPER_MODEL`/`VEYA_OLLAMA_URL`/`VEYA_OLLAMA_MODEL` are set, and are skipped otherwise — they never run against a mock standing in for a real local model.
