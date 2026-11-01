#!/bin/zsh
# ram-bench.py in the order on, off, cleaner, on (drift from heat or the background job shows up as a gap between the two ons)
#   benchmarks/ram-series.sh [seconds per run, default 300] [tag]
H=${0:A:h}; S=${1:-300}; T=${2:-a}
for m in on off cleaner on; do
  /usr/bin/python3 $H/ram-bench.py $m $S $T-$m; sleep 30
done
