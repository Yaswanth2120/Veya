# Question Detection & Local LLM Answer Generation (Section 8)

This document covers Section 8: Python-side question detection and local
Ollama-backed answer generation layered on top of Section 7's real
transcription pipeline. It replaces nothing from Section 7 — real
transcripts still flow exactly as before; this section adds a second,
independent capability (question/answer intelligence) that can be
unavailable without affecting transcription at all.

## Data flow

```text
Swift microphone capture (Section 7, unchanged)
        │
        ▼
Python Whisper transcription (Section 7, unchanged)
        │  transcript.final (deduplicated text)
        ▼
TranscriptionSession.on_final_transcript hook
        │
        ▼
ConversationOrchestrator.handle_final_transcript
        │
        ▼
QuestionDetector.detect()  ── not a question → nothing happens
        │  a question, above the confidence threshold
        ▼
question.detected event
        │
        ▼
context_builder.render_prompt(session_context, question_text)
        │
        ▼
OllamaProvider.generate_stream(prompt)  ── local Ollama, streaming
        │  answer.started → answer.delta* → answer.completed events
        ▼
Swift IPCEventRouter (sequence-checked) → ConversationState → GRDB + overlay
```

Swift remains the macOS host, presentation, and persistence owner exactly
as in every prior section. Python owns question detection and answer
generation; nothing here gives Python its own session database or lets it
decide what gets persisted — `IPCEventRouter` still does that translation,
the same as it always has.

## Local Ollama setup and environment variables

Real answer generation requires a running local Ollama instance with a
model pulled. This repository never installs or requires Ollama — see
[Fallback behavior](#fallback-behavior) for what happens without it.

```sh
# https://ollama.com
ollama pull llama3.2   # or any other model you prefer
ollama serve            # if not already running as a background service
```

| Variable | Meaning | Default |
|---|---|---|
| `VEYA_OLLAMA_URL` | Base URL of a local Ollama instance | `http://localhost:11434` |
| `VEYA_OLLAMA_MODEL` | Model name to use for generation | `llama3.2` |

Both defaults are **sensible placeholders, not guarantees** — `llama3.2`
is not pulled automatically, and `check_availability()` (called once per
session, at `transcription.start` time) verifies the configured model
actually exists locally before answer intelligence is reported available.
Nothing here ever calls a remote/cloud endpoint; `VEYA_OLLAMA_URL` is a
configuration trust boundary, not a network-egress restriction — pointing
it at a non-local address is possible but unsupported and untested.

## Question detection rules and limitations

`core/veya/conversation/question_detector.py`'s `QuestionDetector` is a
deterministic heuristic scorer — **not an LLM call** — run only on
`transcript.final` text (never partials). It scores:

- ends with `?` → strong signal
- starts with an interrogative word (why/how/what/when/who/is/are/do/
  does/did/can/could/would/should/will/...), after stripping a leading
  filler word (so/um/uh/well/okay/and/but) → strong signal, catches
  spoken questions Whisper didn't punctuate as questions
- contains `?` anywhere but not at the end → weak signal

A question is emitted only when the combined score clears
`confidence_threshold` (default `0.6`). Consecutive near-duplicate
questions (the same or a substring/superset of a recently-seen normalized
question) are suppressed — this catches the case where the same spoken
question gets split slightly differently across two overlapping Whisper
rolling windows, on top of Section 7's own overlap deduplication.

**Known limitations**: this is a punctuation/keyword heuristic, not
semantic understanding. It will miss questions phrased without any
interrogative structure ("Tell me why the migration took so long" has no
"?" and doesn't start with an interrogative word) and can occasionally
flag a non-question that happens to start with an interrogative word as
one ("How wonderful, thank you." would score on "how" alone if it also
somehow contained "?"). No semantic/LLM-based detection is planned for
this section — see the build prompt's explicit "deterministic, testable
first-pass detector" requirement.

## IPC schemas

Extends the existing versioned JSON Lines protocol from Sections 6-7 (see
`docs/IPC_PROTOCOL.md`, `docs/REALTIME_TRANSCRIPTION.md`) — no transport
changes.

**`session.start`** (extended, all new fields optional/blank-safe):
```json
{"version":1,"id":"...","type":"request","method":"session.start",
 "params":{"session_id":"...","title":"...","session_type":"...",
           "company":"...","role_or_topic":"...","session_description":"...",
           "notes":"...","preferred_answer_style":"...",
           "preferred_programming_language":"...","custom_instructions":"..."}}
```
Sent once, read-only — Swift never resends this per-question. Every field
beyond `session_id` defaults to `""` server-side, so old/minimal callers
(Section 6/7 tests, the mock feed) are unaffected.

**`answer.cancel`**:
```json
{"version":1,"id":"...","type":"request","method":"answer.cancel","params":{"session_id":"..."}}
```
Cancels whatever answer is currently generating for the session, if any —
a harmless no-op otherwise. Does not stop transcription or question
detection for future questions.

**`question.detected` event** (extended with `confidence`/`detected_at`):
```json
{"version":1,"type":"event","event":"question.detected",
 "data":{"session_id":"...","question_id":"...","text":"...",
         "confidence":0.85,"detected_at":1723488000.0}}
```
`detected_at` is a Unix timestamp (float seconds), consistent with every
other timestamp in this protocol (`started_at`/`ended_at`) — not an
ISO8601 string.

**`answer.completed` event** (extended with `sequence`/`caveat`):
```json
{"version": 1, "type": "event", "event": "answer.completed",
 "data": {
   "session_id": "...", "sequence": 42, "question_id": "...",
   "question": "Why did the migration take six weeks?",
   "talking_points": [
     "The authentication service was migrated first because other services depended on it.",
     "The rollout was staged to preserve backward compatibility.",
     "Validation and rollback safeguards added time but reduced risk."
   ],
   "sources": [], "caveat": ""
 }}
```
`sources` is always `[]` in this section — no RAG, no document retrieval,
never a fabricated citation. `answer.started`/`answer.delta` carry the
same `sequence` field.

## Sequence behavior

Each answer-generation round for a session gets an incrementing integer
`sequence` (Python: `ConversationOrchestrator`, starts at 1). A new
detected question always cancels whatever answer was still generating and
starts a new, higher sequence — only one answer generation runs at a time
per session.

`IPCEventRouter` (Swift) tracks the current sequence per attached session:
- `answer.started` is only applied if its sequence is greater than the
  current one (a stale/duplicate restart is dropped, not applied).
- `answer.delta`/`answer.completed` are only applied if their sequence
  **exactly matches** the current one — anything else (a superseded or
  cancelled round's late event) is silently dropped.

This is what makes cancellation/supersession safe without explicit
acknowledgement round-trips: Python doesn't need to know Swift dropped
something, and Swift never needs to distinguish "stale" from "malformed"
— both are just dropped.

## Fallback behavior

Priority order, decided at `beginLiveSession`/`transcription.start` time
(extends Section 7's three-way real-transcription/mock/fallback order —
see `docs/REALTIME_TRANSCRIPTION.md`):

```text
1. Real transcription + Ollama answer intelligence
   worker .ready + microphone authorized + Whisper available + Ollama available
2. Real transcription without Ollama
   worker .ready + microphone authorized + Whisper available, Ollama unavailable
   → transcripts still flow; no question detection or answer generation runs at all
3. Python mock feed
   real transcription unavailable for any reason (Section 7 behavior, unchanged)
4. Swift fallback
   worker unavailable (Section 6 behavior, unchanged)
```

**Real transcription never falls back to the mock feed just because
Ollama is unavailable.** `transcription.start`'s Whisper check is what
gates success/failure; the Ollama check is a separate, independent step
that only sets `answer_intelligence_available` in the response — a
failure there is caught, logged type-only, and simply means
`ConversationOrchestrator` never analyzes any transcript for questions at
all for that session (see `orchestrator.py`'s
`handle_final_transcript`: it no-ops immediately if `llm_provider is None`
— detecting questions nobody can answer would be a confusing dead end,
not a genuinely more useful state than showing nothing).

**UI indicator** (`PythonIntelligenceCoordinator.liveSessionIndicatorText`,
computed centrally so `LiveSessionView` never touches worker/AVFoundation
state directly) — exactly one of:

```text
Listening — live transcription
Listening — answer intelligence unavailable
Analyzing question…
Generating answer…
Demo mode — Python mock intelligence
Demo mode — Swift fallback
```

"Analyzing question…" (between `question.detected` and `answer.started`)
and "Generating answer…" (between `answer.started` and `answer.completed`)
are read directly from `ConversationState.isAnalyzingQuestion`/
`isGeneratingAnswer` — the same granular per-question state
`IPCEventRouter` already drove in Section 6, now with an added transient
"analyzing" phase. The overlay/indicator never implies a real LLM answer
was generated while the driving source is actually the Python mock feed
or Swift fallback — unchanged hard requirement from every prior section.

## Ending a session

`PythonIntelligenceCoordinator.endLiveSession` (for a real-transcription
session): sends `answer.cancel` (best-effort) to stop any in-flight Python
generation, then calls `ConversationState.cancelPendingAnswerActivity()`
directly to clear `isAnalyzingQuestion`/`isGeneratingAnswer`/
`partialAnswerText` immediately — it does not wait for an `answer.completed`
that a cancelled generation will never send. Same mechanism runs if the
worker becomes unavailable mid-session (Section 6's crash-fallback path).

## Privacy

- No raw transcript, prompt, generated answer text, audio, or API payload
  is ever written to a log. `ollama_provider.py` has no `logging` calls at
  all — verified by a test asserting the module has no `logger` symbol,
  not just by inspecting specific call sites.
- `Dispatcher`/`TranscriptionSession`/`ConversationOrchestrator` all log
  only exception *types* and method/event names on failure paths, mirroring
  the Section 6 review fix to `Dispatcher.dispatch` and Section 7's
  `TranscriptionSession` — never `str(exc)` or a traceback.
- Raw microphone audio is still never persisted (Section 7, unchanged).
- Swift/GRDB remains the sole persistence authority for transcript
  segments, detected questions, and generated answers — Python has no
  database of its own in this section either.
- No RAG, no document parsing, no embeddings — `sources` is always `[]`.
- No Presenter Privacy changes.

## What was and wasn't actually verified

**Verified for real** (not simulated):
- The full question-detection + prompt-construction + parsing pipeline
  was verified against a **real local Ollama instance** available in this
  dev environment (`qwen3:1.7b`, via `ollama serve`):
  - A single `generate_answer()` call against a real prompt completed in
    ~3.4s wall time and produced a real, on-topic answer.
  - A full `ConversationOrchestrator.handle_final_transcript()` round
    (question detection → `question.detected` → `answer.started` → 53
    `answer.delta` events → `answer.completed`) completed in ~2.7s wall
    time with real generated content.
  - The Swift-side `AnswerIntelligenceAvailabilityIntegrationTests` suite
    (gated, opt-in) proved the full chain — Swift → real worker subprocess
    → real Ollama availability check → `TranscriptionStartResult` →
    published `answerIntelligenceAvailable` — genuinely reports `true`
    end-to-end when both Whisper and Ollama are configured.
  - `core/tests/test_ollama_smoke.py` (opt-in, skipped by default) runs
    the same real-Ollama round as an automated, repeatable check.
- **Real-world format-compliance observation**: the small local model used
  for this verification (`qwen3:1.7b`) followed the requested
  `ANSWER:`/`POINTS:`/`CAVEAT:` format completely in some runs and only
  partially in others (e.g. giving an answer and caveat but no bulleted
  points) — this is normal small-model variance, not a bug. `parse_answer_text`'s
  sentence-splitting fallback exists specifically for this; see [Known
  limitations](#known-limitations).

**Not verified**:
- No real-device, real-microphone, real-spoken-question run was
  performed — this environment has no audio input hardware (unchanged
  from Section 7's own limitation). The question-detection path was
  exercised with real Whisper-transcribed text (Section 7's own real
  speech sample) and, separately, with real Ollama on hand-written
  question text — never both together against genuinely spoken audio.
- Answer quality/accuracy was not evaluated — only that real content was
  produced and correctly routed end-to-end.
- No GUI/overlay visual verification was performed.

## Known limitations

- Small local models frequently don't perfectly follow the requested
  `ANSWER:`/`POINTS:`/`CAVEAT:` format (observed directly — see above).
  `parse_answer_text` falls back to sentence-splitting the raw response
  when it can't find that structure, so an answer is still shown, but
  talking-point quality/count is less predictable with smaller/weaker
  models than with format-instruction-following ones.
- No true token-level UI throttling — every raw delta from Ollama becomes
  one `answer.delta` event (dozens per answer, as seen in real testing);
  fine for a local IPC pipe, but not tuned for very verbose models.
- Question detection is punctuation/keyword-based, not semantic (see
  above) — a heuristic false negative silently means no question was
  detected at all, with no signal to the user that something might have
  been missed.
- A generation failure mid-stream (Ollama crashes/becomes unreachable
  after starting) is reported as a degraded `answer.completed` with a
  status message in place of real talking points (see
  `ConversationOrchestrator._emit_generation_failed`) — this keeps the UI
  from hanging on "Generating answer…" forever, but is a deliberately
  simple choice, not a distinct "failed" event/UI state.
- `dedupe_overlap`/near-duplicate question suppression (Section 7/8) are
  both plain string heuristics — see `docs/REALTIME_TRANSCRIPTION.md`'s
  own known-limitations for the overlap-dedup caveat, which applies
  identically here.

## Manual verification checklist (real hardware, not yet performed)

Requires a Mac with a working microphone, real Whisper, and real Ollama —
none of which this environment has together with real audio hardware:

1. Configure `VEYA_WHISPER_BIN`/`VEYA_WHISPER_MODEL` and
   `VEYA_OLLAMA_URL`/`VEYA_OLLAMA_MODEL` (with a model actually pulled).
2. Launch the app, start a Live Session, grant microphone permission.
3. Confirm the indicator reads "Listening — live transcription".
4. Ask a real spoken question ending in a rising/interrogative form.
5. Confirm the indicator transitions "Analyzing question…" →
   "Generating answer…" → back to "Listening — live transcription", and
   the overlay shows a real (non-canned) answer with talking points.
6. Stop Ollama (`ollama stop`/kill the server) mid-session, ask another
   question; confirm the indicator reads "Listening — answer intelligence
   unavailable" and the transcript keeps flowing without hanging.
7. Ask a question, then immediately end the session; confirm no
   `answer.completed` UI update appears after the session view is gone
   (cancellation actually stopped the generation).

## Troubleshooting

- **`answer_intelligence_available` is always `false`**: confirm
  `VEYA_OLLAMA_URL`/`VEYA_OLLAMA_MODEL` are set in the environment the
  worker subprocess actually inherits (same class of issue as
  `docs/REALTIME_TRANSCRIPTION.md`'s Whisper troubleshooting entry), that
  `ollama serve` is actually running, and that the configured model has
  been pulled (`ollama pull <model>`) — `check_availability()` checks
  `/api/tags` for an exact or `:latest`-suffixed match.
- **Questions are detected but never answered**: check
  `answer_intelligence_available` first (see above) — no provider means
  `handle_final_transcript` no-ops before question detection even runs,
  so this specific symptom ("detected but never answered") shouldn't
  happen; if it does, check Python stderr for `"Unhandled ... during
  answer generation"` (type-only, per the privacy rules above).
- **The indicator seems stuck on "Generating answer…"**: confirm
  `ConversationOrchestrator._emit_generation_failed`'s degraded
  `answer.completed` is actually reaching Swift — check the `sequence`
  matches what `IPCEventRouter` currently has tracked; a stale sequence
  (e.g. from a cancelled/superseded round) is dropped by design, not a bug.
