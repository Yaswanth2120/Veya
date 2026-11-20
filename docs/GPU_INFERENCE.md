# GPU inference through the existing SSH tunnel

Codex and Veya are independent clients of the same rented GPU. Veya does
not send microphone input to Codex or execute generated answers as code.

## Codex CLI

The workstation setup used Codex CLI 0.167.1 and a separate user profile.
That machine-local profile is not installed by cloning this repository and
is not required by Veya. Where configured, select it explicitly:

```sh
codex --profile vast-gptoss -C ~/ai-development
# Allow edits only for a task that needs them:
codex --profile vast-gptoss --sandbox workspace-write -C ~/ai-development
```

The normal Codex model and authentication remain unchanged. This profile
uses read-only access and on-request approvals by default. Its reduced
tool configuration disables incompatible namespaces/hosted tools and
optional plugins, MCP, browser, and multi-agent features. The model still
uses fallback metadata. A small repo was tested for file reads, patching,
and unit-test execution; large-repository compatibility is not established.
Approval policy is not a blanket prohibition on every destructive command;
keep read-only mode when you only want analysis and review proposed edits.

## Veya

Quit Veya, build with `sh packaging/build_app.sh`, then open
`~/Library/Caches/Veya/LocalBuild/Veya.app`. Copy the verified app into
`~/Applications/Veya.app` for normal use. The cache staging directory avoids
Desktop/Finder metadata interfering with signing. Development builds are
ad-hoc signed, not notarized; macOS may require renewed capture permission
after a rebuild.
Settings → AI Backend → Vast GPU via SSH → Use This Backend persists the
choice. End an active session before changing backends. To revert, choose
Local Ollama. Alternatively `sh run-gpu.sh` launches the packaged app with
the GPU backend selected for that process (saved UI choices take priority).

Keep the existing SSH tunnel forwarding Mac port 18000 to remote localhost
port 18000. Veya does not create tunnels, change the instance, or expose
ports. No token is required by the currently tested endpoint; do not paste
SSH keys or credentials into source/config files.

The input path is:

```text
Microphone / opted-in meeting audio → local Whisper → question detection
Typed question ──────────────────────────────────────┘
    → local document retrieval + approved memory
    → loopback SSH tunnel → selected served model → clean answer → Veya UI
```

GPU mode sends questions and selected conversation/document/memory text to
the rented host. Raw audio, embeddings, and session storage remain local.
Remote server retention is outside Veya's control. Local embeddings still
require Ollama running with `nomic-embed-text`. The Responses provider uses
`store: false`, ignores reasoning events, does not follow redirects or use
environment proxies, and accepts only loopback HTTP `/v1` URLs.

When Whisper is unavailable in GPU mode, a live session remains usable with
the typed question field instead of producing mock answers. Allow macOS
microphone access for spoken input. Meeting audio still requires per-session
opt-in and Screen Recording permission; it can run without microphone
capture once the conversation is initialized. The project owner confirmed live
Zoom and Google Meet operation on November 20, 2026; expanded device, permission,
and long-call coverage remains future work. See [validation](VALIDATION.md).

## Context and failure limits

Defaults: prefer `openai/gpt-oss-120b` if served, otherwise discover the sole
available model; explicit model overrides are never silently replaced.
The URL is `http://127.0.0.1:18000/v1`, with an
8192-token context budget, 1024 output tokens including reasoning, and low
reasoning effort. The tested server advertises 16384 tokens, but this is not
assumed for every deployment. Environment overrides are `VEYA_LLM_BACKEND`
(`vast`/`ollama`), `VEYA_GPU_URL`, `VEYA_GPU_MODEL`,
`VEYA_GPU_CONTEXT_WINDOW` (maximum 16384), and `VEYA_GPU_MAX_OUTPUT_TOKENS`.

Before sending, short prompts use a conservative UTF-8 byte check. Larger
prompts are counted by vLLM's `/tokenize` endpoint, leaving output tokens
plus 512 tokens for template overhead and respecting the server's maximum.
If the tokenizer is unavailable, a large prompt fails explicitly; supporting
evidence is never silently truncated. A verified larger server
window can be selected through the environment. This is not a whole-repo
context solution. Long answers/reports can also exhaust the output budget.

Tunnel failures, incomplete streams, and context overflow become explicit
failures; Veya does not silently fall back to another inference host.
The existing groundedness guard is narrow and runs after streaming; this
integration does not establish general factual accuracy.

## Reproduce synthetic acceptance checks

These opt-in commands use an isolated worker and synthetic documents:

```sh
python3 core/scripts/smoke_gpu.py
VEYA_WHISPER_BIN="$PWD/whisper.cpp/build/bin/whisper-cli" \
VEYA_WHISPER_MODEL="$HOME/Library/Application Support/Veya/Models/ggml-base.en-1.bin" \
python3 core/scripts/smoke_gpu.py --audio /path/to/mono-16khz-s16le.wav
```

The test requires a retrieved source, the correct metric, the supporting
batching/caching techniques, and usable streamed text. Timings are single
acceptance samples, not a latency benchmark or accuracy score. Speech tests
include paced playback; typed tests start at submission.

See [validation](VALIDATION.md) for recorded acceptance results and the exact measurement boundaries.
