#!/usr/bin/env python3
"""Who writes to the SSD? Samples per-process disk I/O (libproc proc_pid_rusage, RUSAGE_INFO_V4: your own
processes, no root) over N seconds, plus the system totals (IOKit) and swap (vm_stat).
Kernel swap and root daemons (Spotlight, fileproviderd) can't be read per process without root; they show
up as the difference between the system total and the per-process sum.
  python3 bench/io-top.py [seconds]"""
import ctypes, os, re, subprocess, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
import clearx as mg  # noqa: E402

lib = ctypes.CDLL("/usr/lib/libproc.dylib")


def io(pid):
    buf = (ctypes.c_uint64 * 64)()
    if lib.proc_pid_rusage(pid, 4, ctypes.byref(buf)) != 0:  # RUSAGE_INFO_V4
        return None
    # rusage_info_v4: 16-byte uuid, then uint64s; diskio bytes read/written are fields 16 and 17
    return buf[2 + 16], buf[2 + 17]


def snap():
    out = {}
    for p in mg.proc_list().values():
        v = io(p["pid"])
        if v:
            out[p["pid"]] = (p["name"], *v)
    return out


def vm():
    vs = mg.sh("vm_stat")
    g = lambda k: int(re.search(k + r":\s+(\d+)", vs).group(1)) * 4096
    return g("Swapouts"), g("Swapins")


secs = int(sys.argv[1]) if len(sys.argv) > 1 else 300
a, (w0, r0), (so0, si0) = snap(), mg.ssd_counters(), vm()
time.sleep(secs)
b, (w1, r1), (so1, si1) = snap(), mg.ssd_counters(), vm()
per = {}
for pid, (n, rd, wr) in b.items():
    if pid in a:
        per.setdefault(n, [0, 0])
        per[n][0] += rd - a[pid][1]; per[n][1] += wr - a[pid][2]
M = lambda x: f"{x / 2**20:8.0f} MB"
tw = w1 - w0
print(f"{secs}s: SSD written {M(tw)}, read {M(r1 - r0)}; swap-out {M(so1 - so0)}, swap-in {M(si1 - si0)}")
print(f"  per day at this rate: written {tw / secs * 86400 / 2**30:.0f} GB")
print("top writers (your processes):")
for n, (rd, wr) in sorted(per.items(), key=lambda kv: -kv[1][1])[:10]:
    print(f"  {M(wr)} written {M(rd)} read  {n}")
s = sum(v[1] for v in per.values())
print(f"  {M(max(0, tw - s))}  kernel swap + root daemons (Spotlight, file providers, system) + filesystem overhead")
