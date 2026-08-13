# Python Worker Packaging (Release Plan)

This phase (Section 6) does not package the Python worker for
distribution — `PythonWorkerConfiguration.resolveDefault()` resolves
`python3` dynamically via `/usr/bin/env` and locates `core/` via this
project's own source tree. That's fine for development (`swift run`/
`swift test` against a checkout of this repository) but not for a
release build handed to a user who may not have Python installed, may
have an incompatible version, or is launching the app as a GUI bundle
with a minimal `PATH` that doesn't include Homebrew/pyenv/etc.

This document is the executable release plan. Development still uses the
checkout-relative worker configuration; the bundle wiring below is not yet
implemented.

## The problem with the dev-time defaults

1. **`/usr/bin/env python3`** depends on the *user's* environment having a
   working `python3` on `PATH`, at a compatible version, with no
   conflicting site-packages. A GUI app launched from Finder/Dock gets a
   much smaller `PATH` than a Terminal shell (no shell profile is
   sourced), so even a `python3` the user can run from Terminal may not
   be found by a launched `.app`.
2. **`PythonWorkerConfiguration.projectRelativeDefaultWorkerDirectory()`**
   resolves `core/` from this source file's `#filePath`, which is a
   *build-machine* path baked in at compile time — meaningless once the
   app is copied to another machine or even just moved on the same one.

## Recommended approach: a bundled, standalone Python runtime

1. **Use a standalone, relocatable CPython build** — e.g. the
   [`python-build-standalone`](https://github.com/indygreg/python-build-standalone)
   project (also what tools like `uv`/`rye` bundle), or `python.org`'s
   official "framework" installer output. These don't depend on the
   system having Python installed at all, and don't require Homebrew.
2. **Build a locked worker environment** in CI from `core/pyproject.toml`
   (`pypdf>=5,<7` today), then vendor both
   `core/veya` and its installed site-packages into
   the app bundle's `Resources/` directory, e.g.
   `Veya.app/Contents/Resources/python-runtime/` (the interpreter) and
   `Veya.app/Contents/Resources/veya-worker/` (the `veya` package).
3. **Swift resolves paths from the bundle at runtime**, not from source
   file locations:
   ```swift
   let runtimeURL = Bundle.main.resourceURL!
       .appendingPathComponent("python-runtime/bin/python3")
   let workerDirectoryURL = Bundle.main.resourceURL!
       .appendingPathComponent("veya-worker")
   ```
   `PythonWorkerConfiguration` already has the right *shape* for this —
   only `resolveDefault()`'s fallback branch needs to change to check
   `Bundle.main` before falling back to the dev-time `/usr/bin/env` +
   `#filePath` behavior. The `VEYA_PYTHON_EXECUTABLE`/
   `VEYA_WORKER_DIRECTORY` environment variable overrides should remain
   for development and CI regardless.
4. **Code-sign and notarize the bundled interpreter** like any other
   bundled binary — Gatekeeper applies to everything inside `Resources/`
   that's executable.
5. **Strip what isn't needed** from the standalone Python build (tests,
   `idle`, docs, `.pyc` for unused stdlib modules) to keep the app bundle
   size reasonable, but retain the declared PDF/XML dependencies.

## Repeatable build procedure

1. Build a clean, pinned Python environment in CI:
   `python3 -m venv .venv && .venv/bin/python -m pip install --upgrade pip && .venv/bin/python -m pip install -e 'core[dev]'`.
2. Run `.venv/bin/python -m unittest discover -s core/tests` and capture
   `pip freeze` as the release build manifest. Promote a lockfile before
   shipping so transitive dependency versions are reproducible.
3. Copy the validated runtime, `veya` package, and third-party packages to
   `Veya.app/Contents/Resources/`; do not invoke `pip` on an end-user Mac.
4. Set `PythonWorkerConfiguration` to the bundled interpreter and worker
   directory when `Bundle.main.resourceURL` is available. Keep the
   `VEYA_PYTHON_EXECUTABLE` and `VEYA_WORKER_DIRECTORY` overrides for CI.
5. Code-sign every executable in the runtime and any Whisper binary, then
   notarize the complete `.app` after the final bundle is assembled.

## Models and macOS permissions

- Whisper remains an external local binary/model in development. A release
  must either bundle signed, architecture-matched Whisper assets or make an
  explicit first-run local download flow; model paths must resolve beneath
  the application support/model root, never arbitrary user input.
- Ollama is not bundled by this plan. The app uses a locally running
  loopback service by default; its chat and embedding models remain the
  user's local Ollama installation and are optional.
- Add `NSMicrophoneUsageDescription` to the release target's Info.plist
  before packaging. It must be present in the signed app bundle; a runtime
  permission request alone is insufficient.

## Alternative: a packaged single-file worker executable

Tools like [PyInstaller](https://pyinstaller.org) or
[Nuitka](https://nuitka.net) can produce a single native executable from
`core/veya/__main__.py` with the interpreter and stdlib embedded. This
avoids shipping a separate interpreter tree, at the cost of a slower
build step and (for PyInstaller) a larger, less transparent binary. Worth
evaluating against the standalone-runtime approach once real packaging
work starts — the two aren't mutually exclusive to prototype in parallel.

## What does *not* change

- The IPC protocol (`docs/IPC_PROTOCOL.md`) is transport- and
  packaging-agnostic — nothing about JSON Lines over stdin/stdout cares
  whether the process on the other end is a bundled interpreter or a
  system one.
- `core/veya`'s code doesn't need to change; it has no packaging-specific
  logic today.
- `PythonWorkerManager`'s lifecycle/restart/health-check logic is
  unaffected — only *how the executable path and working directory are
  resolved* changes.

## Out of scope for this document

Auto-updating the bundled runtime, supporting user-supplied Python
versions, and a first-run model downloader.
