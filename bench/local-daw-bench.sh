#!/bin/zsh
# clearx OFF vs ON benchmark on the real workload: a clean MyDAW Release build with MyDAW's own build flags
# (cmake Xcode generator, ONLY_ACTIVE_ARCH=NO) while Brave runs with its tabs.
# Read-only to ~/Developer/MyDAW: the committed source (git archive) and the already-fetched deps are copied
# into bench/work, so nothing in XDAW is touched. Each run gets its own build dir and module cache.
# Safety: either run is stopped (SIGINT, then SIGTERM) if swap grows > 3 GB or available memory < 500 MB.
#   ./bench/mydaw-bench.sh            then read REPORT.md / bench/run-*-mydaw-{off,on}.json
set -e
D=~/Developer/ClearX
W=$D/bench/work; X=~/Developer/MyDAW/src
mg() { /usr/bin/python3 $D/clearx.py "$@"; }
rm -rf $W; mkdir -p $W/src $W/deps
git -C $X archive HEAD | tar -x -C $W/src
for d in juce qrcodegen shine link rubberband; do cp -R $X/build/_deps/$d-src $W/deps/; done
for r in off on; do
  cmake -S $W/src -B $W/build-$r -G Xcode -DCMAKE_OSX_DEPLOYMENT_TARGET=12.0 -DFETCHCONTENT_UPDATES_DISCONNECTED=ON \
    -DFETCHCONTENT_FULLY_DISCONNECTED=ON -DFETCHCONTENT_SOURCE_DIR_JUCE=$W/deps/juce-src \
    -DFETCHCONTENT_SOURCE_DIR_QRCODEGEN=$W/deps/qrcodegen-src -DFETCHCONTENT_SOURCE_DIR_SHINE=$W/deps/shine-src \
    -DFETCHCONTENT_SOURCE_DIR_LINK=$W/deps/link-src -DFETCHCONTENT_SOURCE_DIR_RUBBERBAND=$W/deps/rubberband-src > $W/configure-$r.log
done
set +e
quiet() {  # no other build, CPU at full speed, load settled; after 45 min runs anyway (load_start in the JSON says so)
  echo "$(date +%T) waiting for a quiet machine"
  local n=0
  until ! pgrep -f "auto-src/build|xcodebuild|ctest --test-dir" >/dev/null && pmset -g therm | grep -q "CPU_Speed_Limit.*= 100" \
        && [ "$(sysctl -n vm.loadavg | awk '{print int($2)}')" -lt 4 ]; do
    n=$((n + 1)); [ $n -ge 180 ] && { echo "$(date +%T) NOT quiet after 45 min, running anyway"; return; }
    sleep 15
  done
  sleep 60; echo "$(date +%T) quiet"
}
build() { echo "cmake --build $W/build-$1 --config Release -- -quiet ONLY_ACTIVE_ARCH=NO MODULE_CACHE_DIR=$W/build-$1/ModuleCache"; }
trap 'mg on' EXIT INT TERM     # clearx always ends up back on
quiet; mg off
mg bench run mydaw-off --guard-swap 3G --guard-avail 500M --max-seconds 5400 -- "$(build off)"
mg on; quiet; mg tick; sleep 11; mg tick
mg bench run mydaw-on --guard-swap 3G --guard-avail 500M --max-seconds 5400 -- "$(build on)"
rm -rf $W
echo "$(date +%T) done"
