#!/bin/bash
set -o pipefail; cd "$(dirname "$0")"
echo "== ClearX GPU setup =="
xcode-select -p >/dev/null 2>&1 || { echo "Installing Apple command line tools (approve the popup, then run this again)"; xcode-select --install; exit 1; }
swift build -c release 2>&1 | tail -20 || exit 1
./.build/release/ClearXGPU boost
echo; echo "Enter your Mac password once to install the fan governor:"
bash ./install.sh max
echo; ./.build/release/ClearXGPU status
echo; echo "All done. You can close this window."
