#!/bin/sh
# Runs the Veya unit tests.
#
# Xcode.app isn't installed on this machine (Xcode Command Line Tools
# only), so swift-testing's framework lives under the CLT toolchain rather
# than a path `swift test` searches by default. The needed -F/-rpath flags
# are also declared in Package.swift's VeyaTests target for `swift build`
# purposes, but `swift test` only reliably launches the test runner with
# real results when those flags are also passed on the command line here
# (observed on this toolchain — the Package.swift-only settings compile
# fine but the runner then exits silently). Once a full Xcode.app is
# installed, plain `swift test` should work and this script becomes
# unnecessary.
set -e

TESTING_FRAMEWORKS="/Library/Developer/CommandLineTools/Library/Developer/Frameworks"

swift test \
  -Xswiftc -F -Xswiftc "$TESTING_FRAMEWORKS" \
  -Xswiftc -Xfrontend -Xswiftc -disable-cross-import-overlays \
  -Xlinker -F -Xlinker "$TESTING_FRAMEWORKS" \
  -Xlinker -rpath -Xlinker "$TESTING_FRAMEWORKS" \
  "$@"
