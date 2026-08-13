#!/bin/sh
# Installs Veya's real-time streaming ASR binary source
# (stream_stdin.cpp — a persistent, stdin-fed adaptation of whisper.cpp's
# own examples/stream real-time reference implementation, with SDL2/
# live-mic capture replaced by a stdin PCM reader so Swift stays the only
# microphone owner in this app) into a local whisper.cpp checkout, so it
# can be built alongside whisper-cli.
#
# whisper.cpp itself is *not* part of this repo (see .gitignore) — it's a
# separate checkout the developer builds locally (see
# docs/REALTIME_TRANSCRIPTION.md). This script is what makes Veya's own
# contribution to that checkout (this one small example target)
# reproducible and tracked in git, since anything under whisper.cpp/ is
# not.
#
# Usage: packaging/install_streaming_asr_source.sh [path/to/whisper.cpp]
# Defaults to ./whisper.cpp relative to the repo root.
set -e

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WHISPER_CPP_DIR="${1:-$REPO_ROOT/whisper.cpp}"

if [ ! -d "$WHISPER_CPP_DIR/examples" ]; then
  echo "error: no whisper.cpp checkout found at $WHISPER_CPP_DIR (expected an examples/ directory)." >&2
  echo "Clone whisper.cpp there first — see docs/REALTIME_TRANSCRIPTION.md." >&2
  exit 1
fi

TARGET_DIR="$WHISPER_CPP_DIR/examples/stream-stdin"
echo "==> Installing stream-stdin example into $TARGET_DIR"
mkdir -p "$TARGET_DIR"
cp "$REPO_ROOT/packaging/whisper-stream-stdin/stream_stdin.cpp" "$TARGET_DIR/stream_stdin.cpp"
cp "$REPO_ROOT/packaging/whisper-stream-stdin/CMakeLists.txt" "$TARGET_DIR/CMakeLists.txt"

EXAMPLES_CMAKE="$WHISPER_CPP_DIR/examples/CMakeLists.txt"
if grep -q "add_subdirectory(stream-stdin)" "$EXAMPLES_CMAKE" 2>/dev/null; then
  echo "==> $EXAMPLES_CMAKE already references stream-stdin — leaving it as-is"
else
  echo "==> Registering stream-stdin in $EXAMPLES_CMAKE"
  # Inserted right after parakeet-quantize, alongside the other
  # unconditional (non-SDL2) example targets like cli/quantize.
  awk '
    { print }
    /add_subdirectory\(parakeet-quantize\)/ && !done {
      print "    # Veya addition: a persistent, stdin-fed incremental transcription"
      print "    # binary (no SDL2/live-mic dependency — Swift owns microphone capture"
      print "    # in this app). See stream-stdin/stream_stdin.cpp for the protocol."
      print "    add_subdirectory(stream-stdin)"
      done = 1
    }
  ' "$EXAMPLES_CMAKE" > "$EXAMPLES_CMAKE.tmp" && mv "$EXAMPLES_CMAKE.tmp" "$EXAMPLES_CMAKE"
fi

echo "==> Done. Build it with:"
echo "    cmake -S \"$WHISPER_CPP_DIR\" -B \"$WHISPER_CPP_DIR/build\""
echo "    cmake --build \"$WHISPER_CPP_DIR/build\" --target whisper-stream-stdin"
