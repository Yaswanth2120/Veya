# Validation record

Recorded November 20, 2026. This documents a local development prototype, not production certification.

## Automated checks

| Suite | Observed outcome |
|---|---|
| Python, ResourceWarnings treated as errors | 484 discovered; 480 passed; 4 opt-in real-model suites skipped |
| Swift | 228 passed in 30 suites |
| Total executed | 708 passed |
| Signed app | Release packaging plus `codesign --verify --deep --strict` passed; signature remained valid after packaged-worker smoke execution |

Per-commit remote results are available in [GitHub Actions](https://github.com/Yaswanth2120/Veya/actions/workflows/ci.yml). The local outcomes above are recorded independently of remote CI.

Commands:

```sh
python3 -W error::ResourceWarning -m unittest discover -s core/tests
sh run-tests.sh
sh packaging/build_app.sh
codesign --verify --deep --strict "$HOME/Library/Caches/Veya/LocalBuild/Veya.app"
```

An obsolete test assumed AppKit always retained `.none` after a sharing-mode change. The regression test now intercepts the actual sharing-policy setter, verifies the requested transition, and checks verification invalidation; it does not treat a property read-back as proof of capture exclusion. The manifest override test generates its own hashed fixture rather than requiring an ignored local Whisper checkout. IPC correlation tests allow scheduler headroom while the separate timeout test retains its short deadline.

## Reproducible GPU checks

Both committed reports use the packaged Python worker, local Ollama document embeddings, GPT-OSS 120B through a loopback SSH tunnel, and synthetic data only. The client was arm64 macOS with Python 3.14.6. The rented hardware was reported as an A100 SXM4 80GB; the smoke client does not independently query GPU identity or quantization.

| Input | First usable text | Answer completion | Evidence |
|---|---:|---:|---|
| Typed, long synthetic notes | 2.048 s | 2.912 s | [JSON](results/typed-acceptance.json) |
| Paced speech replay on meeting track, no microphone track, long synthetic notes | 10.544 s | 11.359 s | [JSON](results/meeting-track-acceptance.json) |

These are single samples. Timing starts before the input RPC; speech timing includes ASR startup and paced playback. It excludes Swift render time. Streaming/speculative work can begin before playback finishes. Do not compare these rows as a model speedup or describe them as medians/p95s.

Each check requires successful local indexing, a real retrieved source, the expected fictional metric (35 percent), batching/caching in the answer, clean text deltas, and a non-failed completion. Passing these checks is not a general factuality assessment: other unsupported details can still appear.

Reproduce the typed check:

```sh
python3 core/scripts/smoke_gpu.py --long-context --json-report /tmp/typed-result.json
```

Generate synthetic speech locally (no microphone recording):

```sh
say --file-format=WAVE --data-format=LEI16@16000 --channels=1 \
  -o /tmp/veya-question.wav 'How did you reduce inference cost in the Atlas project?'
```

Test the actual bundled worker and meeting-only transcription path:

```sh
VEYA_WHISPER_BIN="$HOME/Library/Caches/Veya/LocalBuild/Veya.app/Contents/Resources/whisper-bin/whisper-cli" \
VEYA_WHISPER_MODEL="$HOME/Library/Application Support/Veya/Models/ggml-base.en-1.bin" \
PYTHONDONTWRITEBYTECODE=1 \
"$HOME/Library/Caches/Veya/LocalBuild/Veya.app/Contents/Resources/python-runtime/bin/python3" \
  core/scripts/smoke_gpu.py \
  --worker-directory "$HOME/Library/Caches/Veya/LocalBuild/Veya.app/Contents/Resources/veya-worker" \
  --audio /tmp/veya-question.wav --meeting-track --long-context \
  --json-report /tmp/meeting-result.json
```

The worker uses isolated temporary documents, memory, and index directories; user profiles and sessions are not read. The audio path replays PCM through the worker API; it does not validate physical microphone or ScreenCaptureKit capture.

## Native application checks

The running app was exercised through macOS accessibility controls:

- All four New Session fields accepted actual keyboard input.
- Valid title/resume-override input enabled creation.
- A named diagnostic session accepted a typed question, displayed a GPU-generated answer without a failure banner, ended, and returned to New Session.
- The previous 1,025-point restored overlay frame is covered by a geometry regression test and is repaired to a bounded frame.

## Owner-reported live meeting checks

On November 20, 2026, the project owner confirmed that live Zoom and Google Meet are working after manual testing. This establishes successful operation in the owner's tested setup. Platform versions, call duration, and a detailed capture checklist were not recorded; the synthetic replay timings above remain separate measurements.

## Not yet established

- Reliability across Zoom/Google Meet versions, permission changes, device switches, long calls, or app-filter configurations; Teams operation has not been reported.
- Successful physical microphone capture after every device/permission change.
- General answer accuracy, hallucination rate, retrieval Recall@k, ASR WER, or speaker diarization quality.
- A fair local-versus-GPU speed comparison or sustained-load latency percentiles.
- Portable installation on a clean Mac, Developer ID/notarization, or consistent TCC grants across ad-hoc rebuilds.
