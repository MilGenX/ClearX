#!/usr/bin/env python3
"""What would more RAM change? Times it on top of whatever the Mac is already doing (the real workload), per condition:
  on       ClearX on
  off      ClearX off: macOS on its own
  cleaner  ClearX off plus what memory-cleaner apps do without admin rights (allocate and touch memory so macOS drops
           its caches) whenever free memory is under 1 GB, at most once a minute
Stand-ins (bench/ram-probe.c): the app you're using (1 GB, a tenth of it touched every second, so every page every
10 s: a slow pass is that app stalling), and 4 hidden apps (400 MB each, one "switched to" every 20 s: how long until it's all back in RAM).
Also measured: the background job's progress (CPU time of llama-finetune-lora or of a build), swap in/out, SSD writes
and how long starting a tiny program takes. The app-in-use stand-in is in [protected] user for the run (ClearX treats
the app in front the same way), so ClearX never lowers or pauses it; the hidden ones are fair game.
  bench/ram-bench.py <on|off|cleaner> [seconds=300] [label]
"""
import importlib.util, json, os, signal, subprocess, sys, time

D = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
LIVE = os.path.expanduser("~/Developer/ClearX")  # the daemon's copy: on/off and the conf live there
spec = importlib.util.spec_from_file_location("cx", os.path.join(LIVE, "clearx.py"))
cx = importlib.util.module_from_spec(spec); spec.loader.exec_module(cx)
B = os.path.join(D, "bench")
PROBE = os.path.join(B, "ram-probe")
IDLE = os.path.join(B, "ram-idle")  # same binary, its own name: hidden apps aren't protected
JOBS = ("llama-finetune-lora", "clang", "clang++", "swift-frontend", "ld", "cmake", "node")


def vm():
    out = cx.sh("vm_stat")
    g = lambda k: int(__import__("re").search(k + r":\s+(\d+)", out).group(1))
    return dict(swapins=g("Swapins"), swapouts=g("Swapouts"), free=(g("Pages free") + g("Pages speculative")) * 4096,
                pageins=g("Pageins"))


def job_cpu():
    """CPU seconds so far of each background job process: pid -> (name, seconds)."""
    out = {}
    for ln in cx.sh("ps", "-axo", "pid=,time=,comm=").splitlines():
        f = ln.split(None, 2)
        if len(f) == 3 and os.path.basename(f[2]) in JOBS:
            t = f[1].replace("-", ":").split(":")
            s = 0.0
            for x in t:
                s = s * 60 + float(x)
            out[int(f[0])] = (os.path.basename(f[2]), s)
    return out


def train_step():
    p = os.environ.get("CLEARX_TRAIN_LOG", "")  # the background job's log (llama-finetune prints data=N/M)
    try:
        tail = open(p, "rb").read()[-4000:].decode(errors="replace").replace("\r", "\n")
        import re
        s = re.findall(r"data=0*(\d+)/", tail)
        return int(s[-1]) if s else None
    except OSError:
        return None


def pct(v, q):
    v = sorted(v)
    return round(v[min(len(v) - 1, int(len(v) * q))], 1) if v else None


def protect(on):
    """Adds/removes ram-probe in [protected] user of the live conf (restored exactly)."""
    path = os.path.join(LIVE, "clearx.conf")
    txt = open(path).read()
    if on and "ram-probe" not in txt:
        txt = txt.replace("\nuser =", "\nuser = ram-probe,", 1)
    elif not on:
        txt = txt.replace("\nuser = ram-probe,", "\nuser =", 1)
    open(path, "w").write(txt)


def main():
    mode = sys.argv[1]
    secs = int(sys.argv[2]) if len(sys.argv) > 2 else 300
    label = sys.argv[3] if len(sys.argv) > 3 else mode
    if not os.path.exists(PROBE) or os.path.getmtime(PROBE) < os.path.getmtime(PROBE + ".c"):
        subprocess.run(["cc", "-O2", "-o", PROBE, PROBE + ".c"], check=True)
    subprocess.run(["cp", PROBE, IDLE], check=True)
    mg = lambda *a: subprocess.run(["/usr/bin/python3", os.path.join(LIVE, "clearx.py"), *a], capture_output=True)
    mg("off" if mode in ("off", "cleaner") else "on")
    protect(True)
    base = os.path.join(B, time.strftime("ram-%Y%m%d-%H%M%S-") + label)
    procs = []
    try:
        idle = []
        for i in range(4):
            out = f"{base}.idle{i}.csv"
            idle.append((subprocess.Popen([IDLE, "idle", "400", out]), out))
        act_out = base + ".active.csv"
        warm = 30
        act = subprocess.Popen([PROBE, "active", "1000", "1000", str(secs + warm), act_out])
        procs = [p for p, _ in idle] + [act]
        time.sleep(warm)
        v0, j0, w0, s0, t0 = vm(), job_cpu(), cx.ssd_counters()[0], train_step(), time.time()
        spawn, cleans, sw_i, last_clean = [], 0, 0, 0
        while time.time() - t0 < secs:
            tt = time.time()
            subprocess.run(["/usr/bin/true"]); spawn.append((time.time() - tt) * 1000)
            k = int(time.time() - t0)
            if k >= 20 * (sw_i + 1):  # every 20 s switch to the next hidden app
                idle[sw_i % 4][0].send_signal(signal.SIGCONT)  # (if ClearX paused it, switching wakes it, as the app does)
                idle[sw_i % 4][0].send_signal(signal.SIGUSR1)
                sw_i += 1
            if mode == "cleaner" and vm()["free"] < (1 << 30) and time.time() - last_clean >= 60:
                subprocess.run([PROBE, "clean", "2048"]); cleans += 1; last_clean = time.time()
            time.sleep(max(0, 2 - (time.time() - tt)))
        v1, j1, w1, s1 = vm(), job_cpu(), cx.ssd_counters()[0], train_step()
    finally:
        for p in procs:
            p.send_signal(signal.SIGCONT); p.terminate()
        protect(False)
        mg("on")
    a = [float(ln.split(",")[1]) for ln in open(act_out) if ln.strip()]
    a = a[warm:]  # (the first 30 passes were while everything was still filling)
    sw = []
    for _, out in idle:
        rows = [float(ln.split(",")[1]) for ln in open(out) if ln.strip()]
        sw += rows[1:] if len(rows) > 1 else []  # (each one's first switch came right after it filled)
    job = {}
    for pid, (name, s) in j1.items():
        job[name] = job.get(name, 0) + s - (j0[pid][1] if pid in j0 else 0)
    r = dict(label=label, mode=mode, seconds=secs, start=time.strftime("%F %T", time.localtime(t0)),
             active_pass_ms=dict(median=pct(a, .5), p95=pct(a, .95), max=pct(a, 1), over_250ms=sum(x > 250 for x in a), n=len(a)),
             switch_ms=dict(median=pct(sw, .5), max=pct(sw, 1), n=len(sw), all=[round(x) for x in sw]),
             spawn_ms=dict(median=pct(spawn, .5), p95=pct(spawn, .95), max=pct(spawn, 1)),
             swapin_mb=round((v1["swapins"] - v0["swapins"]) * 4096 / 2**20),
             swapout_mb=round((v1["swapouts"] - v0["swapouts"]) * 4096 / 2**20),
             ssd_written_mb=round((w1 - w0) / 2**20),
             job_cpu_s={k: round(v) for k, v in job.items()}, train_steps=(s1 - s0) if s0 is not None and s1 is not None else None,
             cleaner_runs=cleans)
    json.dump(r, open(base + ".json", "w"), indent=1)
    print(json.dumps(r))


if __name__ == "__main__":
    main()
