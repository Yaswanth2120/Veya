# Security and Privacy Boundaries

Veya is a native Swift macOS host with a local Python worker. The host sends
microphone PCM and session metadata to the worker over private JSON Lines
stdin/stdout. The worker returns transcript, question, answer, and document
source events only to that host process. No component intentionally uploads
audio, documents, prompts, or answers.

## Local-only boundaries

- Ollama chat and embedding URLs must be loopback (`localhost`, `127.*`, or
  `::1`) by default. A non-loopback `VEYA_OLLAMA_URL` is rejected unless the
  user explicitly sets `VEYA_OLLAMA_ALLOW_REMOTE=1` before worker launch.
- Whisper is launched locally. Its binary and model paths are deployment
  configuration, not document input.
- Document ingestion accepts only regular files that resolve beneath the
  managed SessionDocuments root. Traversal and symlinks that escape that
  root are rejected. The worker's SQLite index stays beneath the managed
  KnowledgeIndex root.

## Parsing and resource limits

- Supported V1 formats are txt, md, PDF, and DOCX. A source file is limited
  to 20 MiB; extracted text is limited to 2,000,000 characters.
- DOCX archives are limited to 1,000 entries, 40 MiB total uncompressed
  content, and a 100:1 per-entry compression ratio. Encrypted archives are
  rejected.
- DOCX XML declarations containing DTDs or entities are rejected before
  stdlib XML parsing; the parser never fetches external entities. Scanned
  PDFs/OCR, macros, and document versioning are not supported.

## Logging policy

Bridge and privacy logs may record method/event names, IDs, counts, sizes,
state transitions, and error *types*. They must never record transcript or
answer text, prompts, document text/excerpts, raw PCM, model responses, raw
stderr, or arbitrary exception messages. Worker stderr is reduced to a
bounded byte-count diagnostic before it is retained or logged. Parser
diagnostics are suppressed at source.

## Manual release checks

Before release, verify a real microphone permission prompt and two
sequential live sessions, Safe Share permissions, a malformed/encrypted PDF,
a hostile DOCX archive, and that no sensitive payload appears in macOS
Console logs. Also validate signing/notarization of the bundled Python and
Whisper executables on a clean Mac.
