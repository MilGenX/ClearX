#!/usr/bin/env python3
"""Replays the pool level rules over recorded benchmarks/slowness-*.json samples: the old rules (allocated swap while
swapping) next to the current ones. Samples have no macOS pressure level or free memory, so pressure counts as normal,
the old low-free rule can't fire (old counts are a minimum) and "ready" (free + cache) stands in for available.
  python3 benchmarks/replay-levels.py [--swap-gb N] [files...]    N: pretend this much swap stays allocated, as it does now
"""
import collections, glob, json, os, sys
GB = 1 << 30
W = 60
args = sys.argv[1:]
swap_gb = None
if args[:1] == ["--swap-gb"]:
    swap_gb = float(args[1]); args = args[2:]


def old(swap, delta_mb):
    if delta_mb < 8:
        return "green"
    return "red" if swap >= 3.5 * GB else "orange" if swap >= 3 * GB else "yellow" if swap >= 2 * GB else "green"


def new(rate, ready):
    if rate >= 50 and ready < 2 * GB:
        return "red"
    if rate >= 25 and ready < 4 * GB:
        return "orange"
    return "yellow" if rate >= 8 else "green"


HERE = os.path.dirname(os.path.realpath(__file__))
for f in args or sorted(glob.glob(os.path.join(HERE, "slowness-*.json"))):
    rows = json.load(open(f))["rows"]
    co, cn, hist, peak = collections.Counter(), collections.Counter(), [], 0
    for a, b in zip(rows, rows[1:]):
        dt = (b["t"] - a["t"]) or 10
        d = max(0, b["swapout"] - a["swapout"]) / (1 << 20)
        hist.append((b["t"], d / dt, dt))
        rate = sum(r * x for t, r, x in hist if b["t"] - t < W) / W
        peak = max(peak, rate)
        co[old(swap_gb * GB if swap_gb is not None else b["swap"], d)] += 1
        cn[new(rate, b["ready"])] += 1
    fmt = lambda c: " ".join(f"{k}={c[k]}" for k in ("green", "yellow", "orange", "red"))
    print(f"{os.path.basename(f)}: {len(rows) - 1} ticks, peak 60 s swap-out {peak:.0f} MB/s")
    print(f"  old: {fmt(co)}\n  new: {fmt(cn)}")
