#!/bin/sh
set -eu
APP_BINARY="$HOME/Library/Caches/Veya/LocalBuild/Veya.app/Contents/MacOS/Veya"
if [ ! -x "$APP_BINARY" ]; then
  APP_BINARY="$HOME/Applications/Veya.app/Contents/MacOS/Veya"
fi
if [ ! -x "$APP_BINARY" ]; then
  echo "Build the app first: sh packaging/build_app.sh" >&2
  exit 1
fi
export VEYA_LLM_BACKEND=vast
exec "$APP_BINARY"
