#!/usr/bin/env python3
"""15-minute evidence sampler for "everything feels slow". Every 10 s: memory pressure %, swap in/out,
compressor, CPU speed limit, load, GPU %, disk bytes (IOKit), per-process CPU time and disk I/O
(proc_pid_rusage, own processes), short-lived process churn by name. Writes bench/slowness-<time>.json.
  python3 bench/slowness-sample.py [seconds]"""
import collections, ctypes, json, os, re, subprocess, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
import clearx as mg  # noqa: E402
lib = ctypes.CDLL("/usr/lib/libproc.dylib")


def rus(pid):
    b = (ctypes.c_uint64 * 64)()
    return (b[2 + 1] + b[2 + 0], b[2 + 16], b[2 + 17]) if lib.proc_pid_rusage(pid, 4, ctypes.byref(b)) == 0 else None


def procs():
    out = {}
    for l in subprocess.run(["ps", "-axo", "pid=,ppid=,comm="], capture_output=True, text=True).stdout.splitlines():
        p = l.split(None, 2)
        if len(p) == 3:
            out[int(p[0])] = p[2].split("/")[-1][:40]
    return out


def vm():
    vs = mg.sh("vm_stat")
    g = lambda k: int((re.search(k + r":\s+(\d+)", vs) or [0, 0])[1]) * 4096
    return g("Swapins"), g("Swapouts")


T = int(sys.argv[1]) if len(sys.argv) > 1 else 900
tb = mg.mach_timebase() if hasattr(mg, "mach_timebase") else None
start, last_seen, cpu, io_w, io_r, born = time.time(), {}, collections.Counter(), collections.Counter(), \
    collections.Counter(), collections.Counter()
rows, prev = [], {}
w0, r0 = mg.ssd_counters(); si0, so0 = vm()
while time.time() - start < T:
    t = time.time()
    P = procs()
    for pid, n in P.items():
        v = rus(pid)
        if v is None:
            continue
        if pid not in prev:
            if t - start > 1:
                born[n] += 1
            prev[pid] = (n, v)  # baseline for processes we can read (new ones count from 0)
            if t - start > 1:
                prev[pid] = (n, (0, 0, 0))
        n0, v0 = prev[pid]
        cpu[n] += v[0] - v0[0]; io_r[n] += v[1] - v0[1]; io_w[n] += v[2] - v0[2]
        prev[pid] = (n, v)
    m = mg.metrics(); spd, _ = mg.thermal(); g = mg.gpu_full() or {}
    w, r = mg.ssd_counters(); si, so = vm()
    rows.append(dict(t=round(t - start), pressure=100 - int(mg.sh("sysctl", "-n", "kern.memorystatus_level").strip() or 100),
                     ready=m["free"] + m["cached"], compressed=m["compressed"], swap=m["swap"], swapin=si, swapout=so,
                     written=w, read=r, speed=spd, load=mg.loadavg()[0], gpu=g.get("util"), vram=g.get("used")))
    time.sleep(max(0, 10 - (time.time() - t)))
w1, r1 = mg.ssd_counters(); si1, so1 = vm()
# rusage cpu times are mach ticks on Intel = ns
res = dict(seconds=round(time.time() - start), written=w1 - w0, read=r1 - r0, swapin=si1 - si0, swapout=so1 - so0,
           user_written=sum(io_w.values()), rows=rows,
           top_cpu=[(n, round(c / 1e9 / T * 100, 1)) for n, c in cpu.most_common(25)],
           top_write=[(n, w) for n, w in io_w.most_common(15)], top_read=[(n, r) for n, r in io_r.most_common(10)],
           born=born.most_common(20))
path = os.path.join(os.path.dirname(os.path.realpath(__file__)), time.strftime("slowness-%Y%m%d-%H%M%S.json"))
json.dump(res, open(path, "w"), indent=1)
print(path)
