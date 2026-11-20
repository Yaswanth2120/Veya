# Resume material

Use two or three bullets, matched to the role. These describe implemented and tested engineering work; they do not claim production adoption or measured accuracy improvements.

## Recommended AI engineering bullets

- Built a native macOS AI interview-practice assistant integrating local Whisper transcription, document-grounded retrieval, and GPT-OSS 120B inference served through vLLM over an SSH tunnel.
- Engineered a streaming Swift/Python pipeline with versioned JSONL IPC, bounded answer scheduling, provider health/model discovery, token-budget validation, and reasoning-output filtering while keeping raw audio and embeddings local.
- Validated the system with 708 passing automated tests across Python and Swift, reproducible packaged-worker speech/RAG/GPU acceptance checks, and native UI interaction tests; packaged an ad-hoc signed macOS development app.

## More retrieval-focused alternative

- Implemented session-scoped RAG with local embeddings and SQLite cosine retrieval, fixed evidence loss between indexed chunks and answer prompts, and added source metadata, context-budget checks, and regression coverage.

## Claims to avoid

Do not claim a numerical accuracy gain, hallucination-free answers, production scale, live Zoom/Meet certification, full automatic speaker identification, or a portable notarized release. Those have not been established. The 708 passing tests exclude four opt-in skipped tests. GPU smoke timings are individual synthetic samples, not representative benchmarks or a measured speedup.

See [validation evidence](VALIDATION.md) and [the roadmap](../README.md#limits-and-next-improvements) before adding future metrics.
