#!/bin/sh
# Builds an actual, installable, ad-hoc signed Veya.app for local use.
#
# This bundles a *venv-based* Python runtime (built with `python3 -m venv
# --copies` against whatever `python3` is on the build machine's PATH),
# not the fully standalone/relocatable interpreter docs/PYTHON_PACKAGING.md
# describes as the ideal (e.g. python-build-standalone) — that requires
# fetching a prebuilt interpreter tarball for the target architecture,
# which this script deliberately does not attempt (no assumption of
# reliable network access, and nothing here should silently download and
# trust an unverified binary). The bundled venv still makes the app
# launchable without requiring python3 on PATH. It still depends on the
# build machine's underlying Python/framework installation.
#
# Usage: packaging/build_app.sh [output_directory]
set -e

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# Desktop/iCloud/Finder metadata can race code signing. Stage outside the
# source tree; an explicit output directory remains supported.
OUTPUT_DIR="${1:-$HOME/Library/Caches/Veya/LocalBuild}"
APP_PATH="$OUTPUT_DIR/Veya.app"
CONTENTS="$APP_PATH/Contents"

echo "==> Building Veya (release)"
cd "$REPO_ROOT"
swift build -c release

echo "==> Creating app bundle structure at $APP_PATH"
rm -rf "$APP_PATH"
mkdir -p "$CONTENTS/MacOS" "$CONTENTS/Resources"

echo "==> Copying executable"
cp "$REPO_ROOT/.build/release/Veya" "$CONTENTS/MacOS/Veya"

echo "==> Copying Info.plist"
cp "$REPO_ROOT/packaging/Info.plist" "$CONTENTS/Info.plist"

echo "==> Copying Whisper model manifest (metadata only — no model bytes bundled; downloaded/verified on first use)"
cp "$REPO_ROOT/packaging/whisper_model_manifest.json" "$CONTENTS/Resources/whisper_model_manifest.json"
mkdir -p "$CONTENTS/Resources/ThirdPartyNotices"
cp "$REPO_ROOT/packaging/whisper-stream-stdin/UPSTREAM_LICENSE" "$CONTENTS/Resources/ThirdPartyNotices/whisper.cpp.LICENSE"

if [ -x "$REPO_ROOT/whisper.cpp/build/bin/whisper-cli" ]; then
  echo "==> Bundling whisper-cli binary (degraded-fallback transcription engine)"
  mkdir -p "$CONTENTS/Resources/whisper-bin"
  cp "$REPO_ROOT/whisper.cpp/build/bin/whisper-cli" "$CONTENTS/Resources/whisper-bin/whisper-cli"
else
  echo "==> No local whisper-cli build found at whisper.cpp/build/bin/whisper-cli — skipping (real transcription will stay unavailable in this bundle until one is added)"
fi

# Section 15: the genuine incremental streaming ASR engine — real partial
# hypotheses roughly every second, not just a batch transcript once a
# fixed window completes. `dispatcher.py` prefers this over whisper-cli
# whenever both are present; whisper-cli above remains the degraded
# fallback if this one is ever missing.
if [ -x "$REPO_ROOT/whisper.cpp/build/bin/whisper-stream-stdin" ]; then
  echo "==> Bundling whisper-stream-stdin binary (real-time streaming transcription engine)"
  mkdir -p "$CONTENTS/Resources/whisper-bin"
  cp "$REPO_ROOT/whisper.cpp/build/bin/whisper-stream-stdin" "$CONTENTS/Resources/whisper-bin/whisper-stream-stdin"
else
  echo "==> No local whisper-stream-stdin build found — skipping (this bundle will use the degraded batch-CLI fallback for real transcription until one is added)"
fi

echo "==> Copying veya worker source"
mkdir -p "$CONTENTS/Resources/veya-worker"
cp -R "$REPO_ROOT/core/veya" "$CONTENTS/Resources/veya-worker/veya"
rm -rf "$CONTENTS/Resources/veya-worker/veya"/**/__pycache__ 2>/dev/null || true
find "$CONTENTS/Resources/veya-worker/veya" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true

echo "==> Building bundled Python runtime (venv, --copies)"
BUNDLED_PYTHON="$CONTENTS/Resources/python-runtime"
python3 -m venv --copies "$BUNDLED_PYTHON"
"$BUNDLED_PYTHON/bin/python3" -m pip install --upgrade pip >/dev/null
"$BUNDLED_PYTHON/bin/python3" -m pip install "pypdf>=5,<7" >/dev/null

echo "==> Verifying the bundled worker imports cleanly"
"$BUNDLED_PYTHON/bin/python3" -c "import sys; sys.path.insert(0, '$CONTENTS/Resources/veya-worker'); import veya" \
  || { echo "FATAL: bundled veya package failed to import" >&2; exit 1; }

echo "==> Signing local helpers and app bundle (bind Info.plist and resources to com.veya.app)"
python3 - "$APP_PATH" <<'PY'
import pathlib
import subprocess
import sys
root = pathlib.Path(sys.argv[1])
listing = subprocess.run(["/usr/bin/xattr", "-r", "-s", str(root)], check=True, capture_output=True, text=True).stdout
for line in listing.splitlines():
    path, separator, attribute = line.rpartition(": ")
    if separator and attribute in {"com.apple.FinderInfo", "com.apple.ResourceFork"}:
        pathlib.Path(path).relative_to(root)
        subprocess.run(["/usr/bin/xattr", "-s", "-d", attribute, path], check=True)
PY
SIGNING_IDENTITY="${VEYA_CODESIGN_IDENTITY:--}"
for HELPER in "$CONTENTS/Resources/whisper-bin/whisper-cli" "$CONTENTS/Resources/whisper-bin/whisper-stream-stdin"; do
  if [ -f "$HELPER" ]; then
    codesign --force --sign "$SIGNING_IDENTITY" "$HELPER"
  fi
done
codesign --force --sign "$SIGNING_IDENTITY" "$BUNDLED_PYTHON/bin/python3"
/usr/bin/xattr -s -d com.apple.FinderInfo "$APP_PATH" 2>/dev/null || true
codesign --force --sign "$SIGNING_IDENTITY" --identifier com.veya.app "$APP_PATH"
codesign --verify --deep --strict "$APP_PATH"

echo "==> Done: $APP_PATH"
echo "    Launch with: open \"$APP_PATH\""
echo "    (local development signature; not notarized for public distribution)"
