#!/bin/zsh
# Quick ClearX ON vs OFF comparison: the same clean MyDAW Release build, cut off after a fixed window, in the order
# ON, OFF, ON so drift from heat or other work shows up. Compares what was built in the window (objects) and how the
# Mac felt meanwhile (spawn_ms, wake_ms, fsync_ms in the JSON). A window isn't a whole build: use mydaw-bench.sh for that.
# Needs bench/work with a configured build-off (left by mydaw-bench.sh). Skips the build queue ([queue] skip).
#   ./bench/mydaw-window.sh [seconds per window, default 240]
D=~/Developer/ClearX
W=$D/bench/work; B=$W/build-off
S=${1:-240}
mg() { /usr/bin/python3 $D/clearx.py "$@"; }
trap 'mg on' EXIT INT TERM
window() {  # label on|off
  (cd $B && cmake --build . --config Release --target clean >/dev/null 2>&1)
  find $B -name '*.o' -delete; rm -rf $B/ModuleCache
  if [ $2 = off ]; then mg off; else mg on; mg tick; fi
  echo "$(date +%T) start $1"
  mg bench run $1 --max-seconds $S -- "cmake --build $B --config Release -- -quiet ONLY_ACTIVE_ARCH=NO MODULE_CACHE_DIR=$B/ModuleCache" > /dev/null
  echo "$(date +%T) $1: objects built $(find $B -name '*.o' | wc -l | tr -d ' ')"
  mg on; sleep 30
}
window mydaw-win-on1 on
window mydaw-win-off off
window mydaw-win-on2 on
echo "$(date +%T) done"
