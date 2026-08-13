# Local Document Ingestion, Retrieval & Grounded Answers (Section 9)

This document covers Section 9: local parsing/chunking/embedding/retrieval
of a session's attached documents, and using retrieved chunks to ground
Section 8's Ollama-backed answer generation with real, verifiable source
references. It replaces nothing from Sections 6–8 — real transcription,
question detection, and answer generation all still work exactly as
before when no documents are attached or retrieval finds nothing
relevant; this section only adds an optional grounding step on top.

## Architecture / data flow

```text
Swift file picker (CreateSessionView, unchanged from Phase 1)
        │  copies the file into Application Support, creates a SessionDocument row
        ▼
PythonIntelligenceCoordinator.ingestDocuments(session:documents:)
        │  knowledge.ingest RPC (path only, never file contents)
        ▼
core/veya/knowledge/ingestion.py — IngestionService
        │  validate_document_path → extract_text → chunk_text → embed → VectorStore
        │  knowledge.ingestion_started/progress/completed/failed events throughout
        ▼
IPCEventRouter → KnowledgeIngestionTracker (Swift, per-document status)
        │
        ▼ (later, when a real-transcription question is detected)
core/veya/conversation/orchestrator.py — ConversationOrchestrator
        │  KnowledgeRetriever.retrieve(session_id, question_text)
        │  → session-scoped top-k chunks above the similarity threshold
        │  → bounded context block injected into the Ollama prompt
        ▼
answer.completed event, sources: [{document_id, file_name, chunk_id, excerpt}, ...]
        ▼
IPCEventRouter → CopilotAnswer.sources: [String] ("filename: excerpt") → GRDB + overlay
```

Python owns parsing, chunking, embeddings, and retrieval. Swift remains
the owner of file selection, session lifecycle, and all persistence —
Python's own local store (`VectorStore`, SQLite) holds only *derived*
knowledge-index data (chunk text/metadata/embeddings + document ingestion
status), never sessions, transcripts, questions, or answers. Removing a
document's index data never touches the original copied file or the
`SessionDocument` row Swift owns.

## Supported formats and limitations

| Format | Extraction |
|---|---|
| `.txt` | Read as UTF-8 (invalid bytes replaced, never a hard failure) |
| `.md` | Same as `.txt` — no Markdown-aware parsing, just raw text |
| `.pdf` | Embedded text layer only, via `pypdf`. **No OCR** — a scanned PDF with no text layer extracts as empty and is rejected. |
| `.docx` | `word/document.xml`'s `<w:t>` runs, via stdlib `zipfile` + `xml.etree.ElementTree` — no external DOCX library. |

Rejected with a typed, user-safe error (never document content) and the
document's status set accordingly:

- Unsupported extension → `unsupported`
- Encrypted/password-protected PDF or DOCX → `failed`
- Malformed/corrupt file that claims to be a supported format → `failed`
- No extractable text (including scanned PDFs) → `failed`
- Larger than 20MB (`MAX_DOCUMENT_BYTES`) → `failed`

Documents, macros, scripts, and embedded links/objects are never
executed — extraction only ever reads text content out of the file.

## Local dependency / model setup

**PDF extraction** requires the `pypdf` package (pure Python, no compiled
extensions). It is declared by the worker package and should be installed
with the rest of the reproducible environment:

```sh
python3 -m pip install -e 'core[dev]'
```

If `pypdf` isn't installed, `.pdf` ingestion fails with a typed
`DocumentMalformedError` (not a crash) — `.txt`/`.md`/`.docx` are
unaffected, since they don't depend on it.

**Embeddings** use a local Ollama instance via its `/api/embed` endpoint —
the same trust boundary and stdlib-only (`urllib`) HTTP approach as
Section 8's chat provider (loopback-only by default; see
`docs/QUESTION_AND_ANSWER_INTELLIGENCE.md`'s equivalent section for the
`VEYA_OLLAMA_ALLOW_REMOTE` opt-in). This reuses the Ollama runtime Section
8 already requires rather than adding a second heavyweight ML dependency
(e.g. `sentence-transformers`) — one local runtime for both chat and
embeddings, at the cost of both needing the same instance up.

```sh
ollama pull nomic-embed-text   # ~274MB, this repo's default embedding model
```

| Variable | Meaning | Default |
|---|---|---|
| `VEYA_OLLAMA_URL` | Same instance Section 8's chat provider uses | `http://localhost:11434` |
| `VEYA_OLLAMA_EMBEDDING_MODEL` | Embedding model name | `nomic-embed-text` |
| `VEYA_OLLAMA_ALLOW_REMOTE` | Opt-in to a non-loopback `VEYA_OLLAMA_URL` | unset (disallowed) |

A deterministic `FakeEmbeddingProvider` (hash-based bag-of-words vectors —
no model, no I/O) is used throughout the test suite; real embeddings are
never required to run `./run-tests.sh`/`python3 -m unittest discover`.

## Index storage location

`~/Library/Application Support/Veya/KnowledgeIndex/knowledge.sqlite` — a
sibling of `SessionDocuments/` (the copied-document files) and
`veya.sqlite` (Swift/GRDB's own database), never the same file. Swift
passes both paths to the worker subprocess as environment variables:

| Variable | Meaning |
|---|---|
| `VEYA_DOCUMENTS_DIRECTORY` | `~/Library/Application Support/Veya/SessionDocuments/` — the *only* filesystem location `knowledge.ingest` is ever allowed to read beneath |
| `VEYA_KNOWLEDGE_INDEX_DIRECTORY` | Where `knowledge.sqlite` lives |

Both default to the real Application Support paths if unset (matching
`CreateSessionViewModel`'s own default), but `PythonWorkerManager` always
sets them explicitly for the real worker subprocess it launches.

## Ingestion IPC

**`knowledge.ingest`** — fire-and-forget, same pattern as
`mock.start_live_feed`: the RPC acknowledges "ingestion started," not
"ingestion finished."

```json
{"version":1,"id":"...","type":"request","method":"knowledge.ingest",
 "params":{"session_id":"...","document_id":"...","file_name":"architecture.pdf",
           "file_extension":"pdf","file_path":"/local/app-support/path/architecture.pdf"}}
```

`file_path` must already be `SessionDocument.storedPath` — the app-managed
copy Swift made when the document was attached. Python canonicalizes it
(`Path.resolve(strict=True)`) and rejects it (`DocumentPathInvalidError`,
surfaced as an `unsupported`/`failed` ingestion, never a crash) unless it
resolves strictly beneath `VEYA_DOCUMENTS_DIRECTORY` — this rejects
directory traversal, symlink escapes, and any path outside that directory,
including a nonexistent file or a directory passed as if it were a file.
**Whole document contents are never sent over JSON Lines** — only the path.

**`knowledge.remove`** / **`knowledge.status`**:
```json
{"version":1,"id":"...","type":"request","method":"knowledge.remove","params":{"document_id":"..."}}
{"version":1,"id":"...","type":"request","method":"knowledge.status","params":{"document_id":"..."}}
```
`knowledge.status`'s result: `{"status": "not_indexed"|"indexing"|"ready"|"failed"|"unsupported"}`.
`knowledge.remove` deletes the document's row and (via `ON DELETE CASCADE`)
every chunk derived from it — the original copied file and `SessionDocument`
row are untouched (Swift owns those).

**`knowledge.retrieve`** — mainly a diagnostics/manual-testing entry
point; the production grounded-answer path retrieves *internally* in
Python during answer generation, not via a Swift-initiated round trip:
```json
{"version":1,"id":"...","type":"request","method":"knowledge.retrieve","params":{"session_id":"...","query":"..."}}
```
Result: `{"sources": [{"document_id","file_name","chunk_id","excerpt"}, ...]}`.

**Events**: `knowledge.ingestion_started` / `knowledge.ingestion_progress`
(`stage`: `"chunked"`/`"embedded"`, `chunk_count`) /
`knowledge.ingestion_completed` / `knowledge.ingestion_failed`
(`status`, `reason` — always a safe typed description, never document text).

## Chunking / retrieval configuration

`core/veya/knowledge/models.py`:

| `ChunkingConfig` field | Default |
|---|---|
| `target_chunk_characters` | 800 |
| `overlap_characters` | 150 |
| `max_excerpt_length` | 240 |

| `RetrievalConfig` field | Default |
|---|---|
| `top_k` | 5 |
| `similarity_threshold` | 0.3 |
| `max_context_characters` | 4000 |
| `max_excerpt_length` | 240 |

Character counts, not tokens — a simple, dependency-free, fully
deterministic proxy for "how much text fits in a bounded prompt," which
is all this needs. Chunk IDs are stable (`sha256(document_id:chunk_index)`,
truncated) — re-ingesting the same document is a clean replace, not an
accumulating duplicate set.

## Source-grounding rules

1. A question triggers retrieval only when it was actually detected
   (Section 8's `QuestionDetector`) *and* answer intelligence (Ollama
   chat) is available — retrieval never runs standalone.
2. Retrieval is strictly session-scoped (`VectorStore.search` joins on
   `session_id` and `documents.status = 'ready'`) — never crosses
   sessions, never includes an in-progress or failed document's stale
   chunks.
3. Chunks below `similarity_threshold` are dropped; if nothing meets the
   threshold, the answer is generated **without** a document context
   block and `sources` is `[]` — never a forced/fabricated citation.
4. When chunks are included, the prompt gets an explicit, delimited
   "Supporting context from the user's session documents" block plus an
   instruction to use it only for document-specific claims and to prefer
   an explicit caveat over silently resolving a conflict with it.
5. `sources` on `answer.completed` always corresponds exactly to what was
   actually retrieved for *that* answer — built directly from the
   retrieved chunk list, never from anything the model said. The model
   cannot invent a filename, chunk ID, or excerpt that reaches Swift.
6. Ollama being unavailable, or retrieval finding nothing, never falls
   real transcription back to the mock feed — same guarantee Section 8
   established for chat-only unavailability.

## Swift integration

- `KnowledgeIngestionTracker` (`Bridge/`): app-lifetime `ObservableObject`
  keyed by document `UUID`, updated only by `IPCEventRouter` from
  `knowledge.ingestion_*` events — independent of any single Live
  Session's attach/detach lifecycle, since a document's index status
  should keep reading correctly whether or not a session is currently
  live. Exposed as `PythonIntelligenceCoordinator.knowledgeIngestionTracker`
  (same instance `IPCEventRouter` updates) so views never touch the
  router directly.
- `CreateSessionViewModel.save()` now records the `SessionDocument`s it
  actually persisted (`lastCreatedDocuments`); `CreateSessionView` calls
  `PythonIntelligenceCoordinator.ingestDocuments(session:documents:)`
  right after a successful save, before starting the Live Session.
  Fire-and-forget per document — a failure (worker not ready, RPC error)
  only marks that document `failed` in the tracker; it never deletes the
  copied file, the `SessionDocument` row, or blocks session
  creation/start.
- `LiveSessionView`'s side panel gained a "DOCUMENTS" section listing each
  attached document with its live status
  (`DocumentIngestionStatus.displayText`): **Not indexed** / **Indexing…**
  / **Ready** / **Failed to index** / **Unsupported document** — the
  exact five states the build prompt specifies.
- `IPCEventRouter` folds each structured `answer.completed` source
  (`{document_id, file_name, chunk_id, excerpt}`) into a compact
  `"filename: excerpt"` string appended to `CopilotAnswer.sources:
  [String]` — no `CopilotAnswer`/GRDB schema change, and `OverlayView`
  already renders `sources.first` exactly as before (`if let source =
  answer.sources.first { Text("Source: \(source)") }`), so the overlay's
  existing compact, only-when-present source display needed **no
  changes** to pick this up.

## Privacy and security

- Process locally only — no network calls for parsing (stdlib +
  `pypdf`), no network calls for embeddings/chat beyond the local Ollama
  instance (loopback-enforced by default, same as Section 8).
- `validate_document_path` is the one and only filesystem read boundary:
  canonicalized, `strict=True` resolved, and required to sit beneath
  `VEYA_DOCUMENTS_DIRECTORY` — rejects traversal, symlink escapes, and
  any path Swift didn't itself create by copying an attached document.
- Document text, chunks, embeddings, excerpts, transcript text, prompts,
  and generated answers are never written to a log. `knowledge.ingestion_failed`'s
  `reason` field is always a static, typed description (see
  `knowledge/errors.py`) — verified directly by a test that plants
  sensitive content in a document and asserts it never appears in the
  emitted failure event.
- Raw microphone audio is still never persisted (Sections 7–8, unchanged).
- No Presenter Privacy changes.

## What was and wasn't actually verified

**Verified for real** (not simulated):
- Real PDF extraction via `pypdf` against a hand-crafted minimal PDF with
  a real text-drawing stream (`pypdf` recovers from its non-standard
  xref table via its own fallback parser — a real, observed behavior,
  not assumed).
- Real DOCX extraction via stdlib `zipfile`/`ElementTree` against a
  hand-crafted `.docx`.
- Real PDF encryption detection: a real encrypted PDF was generated with
  `pypdf.PdfWriter.encrypt(...)` and confirmed to raise
  `DocumentEncryptedError` on read.
- Real local embeddings via a real Ollama instance + `nomic-embed-text`
  (pulled during this session) — `OllamaEmbeddingProvider.check_availability()`
  and `.embed(...)` both exercised against the real `/api/embed` endpoint.
- **The full grounded-answer pipeline end-to-end for real**: a real
  document was chunked, embedded via real Ollama embeddings, stored in a
  real `VectorStore`, retrieved for a real detected question, injected
  into a real prompt, and answered by a real Ollama chat model
  (`qwen3:1.7b`) — the resulting `answer.completed` event carried a real,
  correct source reference (`document_id`/`file_name`/`chunk_id`/`excerpt`
  all pointing at the actual ingested content) and talking points that
  genuinely reflected the document's content, not a hallucination.
- The real Swift↔Python `knowledge.ingest` RPC + `knowledge.ingestion_*`
  event pipeline, against a real worker subprocess and a real file on
  disk, reaching a real terminal status in `KnowledgeIngestionTracker`.

**Not verified**:
- No real GUI interaction — attaching a file via the actual file picker,
  watching the "DOCUMENTS" section update live, or seeing a real overlay
  source line render was not performed (this environment has no
  interactive GUI session).
- Answer *quality*/grounding *accuracy* at scale was not evaluated —
  only that the mechanism correctly threads real retrieved content
  through to a real answer once, for one document/question pair.
- OCR was not implemented or tested — scanned PDFs are explicitly out of
  scope for this section (see Known limitations).
- No large-document/large-corpus performance testing was performed.

## Manual verification checklist (not yet performed)

1. Attach a `.txt`, `.md`, `.pdf`, and `.docx` file when creating a
   session; confirm all four show "Indexing…" then "Ready" in the
   DOCUMENTS section within a few seconds.
2. Attach an unsupported file type (e.g. `.zip`); confirm it shows
   "Unsupported document" and the session still starts normally.
3. Attach a password-protected PDF; confirm "Failed to index" and that
   the original file is still present on disk (`SessionDocuments/`).
4. With Whisper + Ollama (chat + `nomic-embed-text`) all configured, ask
   a real spoken question whose answer is only in an attached document;
   confirm the overlay shows a "Source: filename: excerpt" line and that
   the excerpt is genuinely from the document.
5. Ask a question unrelated to any attached document; confirm no source
   line appears and the answer doesn't claim to cite anything.
6. Remove a document mid-session (once a removal UI exists — not built
   in this section) and confirm subsequent answers no longer cite it.

## Known limitations

- **No OCR.** A scanned PDF (image-only, no embedded text layer)
  extracts as empty text and is rejected (`DocumentEmptyError`) — by
  design for this section, not a bug.
- **No cloud retrieval/embeddings** — everything is local-only; there is
  no fallback to a hosted embedding or vector-search service if Ollama
  is unavailable, only "answer intelligence unavailable" (same as
  Section 8's chat-only case) or "ungrounded answer" (no sources).
- Chunking is character-based, not sentence/paragraph-aware — a chunk
  boundary can land mid-sentence. The configured overlap mitigates but
  doesn't eliminate this.
- DOCX parsing rejects DTD/entity declarations before stdlib XML parsing,
  and applies archive-entry, decompressed-size, compression-ratio, and
  extracted-text limits. It remains deliberately narrow V1 parsing, not a
  general-purpose hostile-document sandbox.
- Retrieval relevance is only as good as the embedding model — a small
  local model can miss semantically-related-but-differently-worded
  content. No re-ranking or hybrid (keyword + vector) search is
  implemented.
- No document versioning: re-ingesting a document with the same
  `document_id` replaces its chunks entirely; there's no history of
  prior ingestions.
