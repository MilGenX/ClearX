#!/usr/bin/env python3
"""memguard v2 - keeps a 16 GB Intel Mac out of swap death.

Lowers priority (renice + taskpolicy background QoS), pauses (SIGSTOP) memory hogs,
caps parallel compiler memory, predicts exhaustion from the trend, and resumes
(SIGCONT) when pressure drops. Never sends SIGKILL. Stdlib only.

Usage:
  memguard status | log [N] | junk | json      (status shows the 20 GB RAM+VRAM pool)
  memguard tick [--dry-run] [--force-level L]
  memguard pause <name> | resume all | resume <name>
  memguard pause-pid|resume-pid|bg-pid|fg-pid|quit-pid|throttle-pid|unthrottle-pid <pid>
  memguard bench [seconds]   (CSV snapshot: speed limit, load, swap rate, VRAM, GPU%)
  memguard bench run <label> [--guard-swap 3G] [--guard-avail 500M] -- <command>   (workload benchmark)
  memguard off | on
  memguard maintain [all|scan|clean <cat,...>|large|startup|disable <label>|enable <label>
                     |uninstall <App> [--vendor com.x] [--apply]|updates|security|health|storage|optimize]
  memguard boost [--json] [--quit "App, App"]   Speed Up: real actions only, measured before/after
  memguard optimize [--apply]   measure every limit of this Mac; --apply sets the safe user-level items
"""
import configparser, fnmatch, json, os, re, signal, subprocess, sys, time

HOME = os.path.dirname(os.path.realpath(__file__))
CONF = os.environ.get("MEMGUARD_CONF", os.path.join(HOME, "memguard.conf"))
STATE_DIR = os.environ.get("MEMGUARD_STATE", os.path.join(HOME, "state"))
LOG_DIR = os.environ.get("MEMGUARD_LOGS", os.path.join(HOME, "logs"))
STATE_FILE = os.path.join(STATE_DIR, "state.json")
OFF_FILE = os.path.join(STATE_DIR, "disabled")
LOG_FILE = os.path.join(LOG_DIR, "memguard.log")
LEVELS = ["green", "yellow", "orange", "red"]
PAGE = 4096
MY_UID = os.getuid()
HISTORY_S = 600
MAINT_FILE = os.path.join(STATE_DIR, "maint.json")


def load_maint():
    try:
        with open(MAINT_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_maint(d):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(MAINT_FILE + ".tmp", "w") as f:
        json.dump(d, f, indent=1)
    os.replace(MAINT_FILE + ".tmp", MAINT_FILE)


def record_health(st):
    import maint
    d = load_maint()
    score, why = maint.health(st, d)
    d["health"] = dict(score=score, why=why, t=time.time())
    hh = [h for h in d.get("health_hist", []) if time.time() - h[0] < 7 * 86400]
    if not hh or time.time() - hh[-1][0] >= 3600:
        hh.append([round(time.time()), score])
    d["health_hist"] = hh
    save_maint(d)
    return score, why


# ---------- config / state / log ----------
def size(s):
    s = str(s).strip().upper().rstrip("B")
    mult = {"K": 1 << 10, "M": 1 << 20, "G": 1 << 30}.get(s[-1:], 1)
    return int(float(s[:-1] if s[-1:] in "KMG" else s) * mult)


def globs(s):
    return [g.strip() for g in re.split(r"[,\n]", s or "") if g.strip()]


def load_conf():
    c = configparser.ConfigParser(interpolation=None)
    c.optionxform = str  # keep case of budget keys
    c.read(CONF)
    return c


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(st):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, indent=1)
    os.replace(tmp, STATE_FILE)


_conf = None


def log(msg):
    os.makedirs(LOG_DIR, exist_ok=True)
    max_b = int((_conf.getfloat("general", "log_max_mb", fallback=5) if _conf else 5) * (1 << 20))
    keep = _conf.getint("general", "log_keep", fallback=3) if _conf else 3
    try:
        if os.path.getsize(LOG_FILE) > max_b:
            for i in range(keep - 1, 0, -1):
                if os.path.exists(f"{LOG_FILE}.{i}"):
                    os.replace(f"{LOG_FILE}.{i}", f"{LOG_FILE}.{i + 1}")
            os.replace(LOG_FILE, LOG_FILE + ".1")
    except OSError:
        pass
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + msg
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")
    if sys.stdout.isatty() or os.environ.get("MEMGUARD_ECHO"):
        print(line)


def notify(title, msg):
    if _conf and not _conf.getboolean("general", "notify", fallback=True):
        return
    esc = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')
    subprocess.run(["osascript", "-e",
                    f'display notification "{esc(msg)}" with title "memguard" subtitle "{esc(title)}"'],
                   capture_output=True, timeout=10)


def sh(*args):
    return subprocess.run(args, capture_output=True, text=True, timeout=30).stdout


# ---------- metrics ----------
def metrics():
    m = {}
    sw = sh("sysctl", "-n", "vm.swapusage")
    used = re.search(r"used = ([\d.]+)M", sw)
    m["swap"] = int(float(used.group(1)) * (1 << 20)) if used else 0
    try:
        m["pressure"] = int(sh("sysctl", "-n", "kern.memorystatus_vm_pressure_level").strip())
    except ValueError:
        m["pressure"] = 1
    vs = sh("vm_stat")

    def pg(k):
        r = re.search(k + r":\s+(\d+)", vs)
        return int(r.group(1)) if r else 0
    m["free"] = (pg("Pages free") + pg("Pages speculative")) * PAGE
    # what could be handed out without swapping: free + clean/inactive + purgeable
    m["avail"] = m["free"] + (pg("Pages inactive") + pg("Pages purgeable")) * PAGE
    m["swapouts"] = pg("Swapouts")
    # pool segments (Activity Monitor's definitions): app = anonymous - purgeable, cached = file-backed + purgeable
    m["wired"] = pg("Pages wired down") * PAGE
    m["compressed"] = pg("Pages occupied by compressor") * PAGE
    m["app"] = max(0, pg("Anonymous pages") - pg("Pages purgeable")) * PAGE
    m["cached"] = (pg("File-backed pages") + pg("Pages purgeable")) * PAGE
    return m


def proc_list():
    """Own-uid processes: pid -> {name, rss, mem(phys footprint), nice, stat}."""
    out = sh("ps", "-axo", "pid=,ppid=,uid=,rss=,nice=,%cpu=,stat=,comm=")
    procs = {}
    for ln in out.splitlines():
        p = ln.split(None, 7)
        if len(p) < 8 or int(p[2]) != MY_UID:
            continue
        pid = int(p[0])
        procs[pid] = dict(pid=pid, ppid=int(p[1]), rss=int(p[3]) * 1024, nice=int(p[4]), cpu=float(p[5]),
                          stat=p[6], name=os.path.basename(p[7]), path=p[7])
    for p in procs.values():
        p["mem"] = p["rss"]
    return procs


def add_footprint(procs):
    """top's MEM column is the phys footprint, which includes compressed/swapped memory -
    much better than RSS for finding the real hogs. It is ~70% of a tick's cost, so
    only called when something might need acting on."""
    for ln in sh("top", "-l", "1", "-o", "mem", "-n", "120", "-stats", "pid,mem").splitlines():
        mt = re.match(r"\s*(\d+)\s+([\d.]+)([BKMGT])", ln)
        if mt and int(mt.group(1)) in procs:
            procs[int(mt.group(1))]["mem"] = int(float(mt.group(2)) * {"B": 1, "K": 1 << 10, "M": 1 << 20,
                                                                         "G": 1 << 30, "T": 1 << 40}[mt.group(3)])
    for p in procs.values():
        p["mem"] = max(p.get("mem", 0), p["rss"])
    return procs


def cmdline(pid):
    return sh("ps", "-o", "args=", "-p", str(pid)).strip()


def start_time(pid):
    return sh("ps", "-o", "lstart=", "-p", str(pid)).strip()


def start_epoch(pid):
    try:
        return time.mktime(time.strptime(start_time(pid), "%a %b %d %H:%M:%S %Y"))
    except ValueError:
        return 0


def gpu_full():
    """The IOAccelerator with the most VRAM in use: {used, util, sys}. `sys` is inUseSysMemoryBytes, the system RAM
    the GPU has mapped over PCIe (GART) because VRAM is full: the GPU spilling into the 16 GB. None if unavailable."""
    out = sh("ioreg", "-r", "-d1", "-c", "IOAccelerator")
    best = None
    for blk in re.findall(r'"PerformanceStatistics" = \{(.*?)\}', out):
        g = lambda k: int(re.search(f'"{k}"=(\\d+)', blk).group(1)) if re.search(f'"{k}"=(\\d+)', blk) else None
        used = g("inUseVidMemoryBytes")
        if used is not None and (best is None or used > best["used"]):
            best = dict(used=used, util=g("Device Utilization %"), sys=g("inUseSysMemoryBytes") or 0)
    return best


def gpu_stats():
    """(vram_used_bytes, gpu_util_pct) of the busiest IOAccelerator, or (None, None)."""
    g = gpu_full()
    return (g["used"], g["util"]) if g else (None, None)


def disk_probe():
    """(free bytes on the swap volume, ms for a 4 KB write + fsync). Swap lives on this disk, so a slow or
    nearly full disk makes every swap-out expensive."""
    import shutil
    free = shutil.disk_usage("/System/Volumes/VM" if os.path.isdir("/System/Volumes/VM") else "/").free
    p = os.path.join(STATE_DIR, ".probe")
    t = time.perf_counter()
    try:
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.write(fd, os.urandom(4096)); os.fsync(fd); os.close(fd)
    except OSError:
        return free, None
    return free, round((time.perf_counter() - t) * 1000, 1)


def front_pid():
    asn = sh("lsappinfo", "front").strip()
    mt = re.search(r'"pid"\s*=\s*(\d+)', sh("lsappinfo", "info", "-only", "pid", asn)) if asn else None
    return int(mt.group(1)) if mt else None


SHELLS = {"zsh", "bash", "sh", "fish", "login", "tmux", "screen"}


def tree(procs, roots):
    """pids of roots plus their descendants, not descending through shells (a build started in the
    foreground Terminal is a build, not the foreground app)."""
    kids = {}
    for p in procs.values():
        kids.setdefault(p["ppid"], []).append(p)
    out, todo = set(), [r for r in roots if r in procs]
    while todo:
        pid = todo.pop()
        if pid in out:
            continue
        out.add(pid)
        todo += [k["pid"] for k in kids.get(pid, []) if k["name"] not in SHELLS]
    return out


def thermal():
    """CPU_Speed_Limit / CPU_Scheduler_Limit from pmset (100 = not throttled)."""
    out = sh("pmset", "-g", "therm")
    g = lambda k: int(re.search(k + r"\s*=\s*(\d+)", out).group(1)) if re.search(k + r"\s*=\s*(\d+)", out) else 100
    return g("CPU_Speed_Limit"), g("CPU_Scheduler_Limit")


def loadavg():
    return [float(x) for x in sh("sysctl", "-n", "vm.loadavg").strip(" {}\n").split()[:3]]


# ---------- CPU duty-cycle throttler (cpulimit-style) ----------
def throttle_loop(pid, duty):
    """Runs detached: SIGSTOP/SIGCONT the pid so it gets `duty` of each 200 ms slice.
    Always leaves the process running (SIGCONT) when it exits."""
    period, on = 0.2, 0.2 * duty
    stop = []
    signal.signal(signal.SIGTERM, lambda *a: stop.append(1))
    try:
        while not stop:
            os.kill(pid, signal.SIGCONT); time.sleep(on)
            if stop:
                break
            os.kill(pid, signal.SIGSTOP); time.sleep(period - on)
    except OSError:
        pass
    finally:
        try:
            os.kill(pid, signal.SIGCONT)
        except OSError:
            pass


def start_throttle(p, st, duty, why):
    if str(p["pid"]) in st.setdefault("throttled", {}):
        return
    tp = subprocess.Popen([sys.executable, os.path.realpath(__file__), "throttle-loop", str(p["pid"]), str(duty)],
                          stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                          start_new_session=True)
    st["throttled"][str(p["pid"])] = dict(name=p["name"], t=time.time(), loop=tp.pid, duty=duty, why=why,
                                         start=start_time(p["pid"]))
    log(f"THROTTLE pid={p['pid']} {p['name']} cpu={p['cpu']:.0f}% -> {int(duty * 100)}% duty ({why})")


def stop_throttle(pid, st, why):
    info = st.get("throttled", {}).pop(str(pid), None)
    if not info:
        return
    try:
        os.kill(info["loop"], signal.SIGTERM)
    except OSError:
        pass
    time.sleep(0.3)
    try:
        if start_time(pid) == info.get("start"):
            os.kill(pid, signal.SIGCONT)
    except OSError:
        pass
    log(f"UNTHROTTLE pid={pid} {info['name']} ({why})")


# ---------- helpers ----------
def matches(name, patterns):
    n = name.lower()
    return any(fnmatch.fnmatch(n, g.lower()) for g in patterns)


def protected_set(conf):
    return globs(conf.get("protected", "names", fallback="")) + globs(conf.get("protected", "user", fallback=""))


def eligible(procs, conf, st, include_paused=False):
    prot = protected_set(conf)
    rx = conf.get("test", "target_regex", fallback="").strip()
    me = {os.getpid(), os.getppid()}
    paused = {int(k) for k in st.get("paused", {})}
    out = []
    for p in procs.values():
        if p["pid"] in me or "Z" in p["stat"] or matches(p["name"], prot):
            continue
        if p["pid"] in paused and not include_paused:
            continue
        if rx and not re.search(rx, cmdline(p["pid"])):
            continue
        cool = st.get("cooldown", {}).get(str(p["pid"]))
        if cool and cool > time.time():
            continue
        out.append(p)
    return out


def renice(p, dry, why):
    if p["nice"] >= 10:
        return False
    if not dry:
        subprocess.run(["renice", "10", "-p", str(p["pid"])], capture_output=True)
    p["nice"] = 10
    log(f"RENICE +10 pid={p['pid']} {p['name']} mem={p['mem'] >> 20}M ({why}){' [dry-run]' if dry else ''}")
    return True


def background(p, st, dry, why):
    """taskpolicy -b: background QoS (throttles CPU, disk I/O and GPU priority)."""
    if str(p["pid"]) in st.setdefault("bg", {}):
        return False
    if not dry:
        r = subprocess.run(["taskpolicy", "-b", "-p", str(p["pid"])], capture_output=True)
        if r.returncode != 0:
            return False
        st["bg"][str(p["pid"])] = dict(name=p["name"], t=time.time(), why=why, start=start_time(p["pid"]))
    log(f"BACKGROUND pid={p['pid']} {p['name']} mem={p['mem'] >> 20}M ({why}){' [dry-run]' if dry else ''}")
    return True


def unbackground(pid, st, why, dry=False):
    if dry:
        if str(pid) in st.get("bg", {}):
            log(f"FOREGROUND pid={pid} ({why}) [dry-run]")
        return False
    info = st.get("bg", {}).pop(str(pid), None)
    if info is None:
        return False
    if start_time(pid) == info.get("start"):
        subprocess.run(["taskpolicy", "-B", "-p", str(pid)], capture_output=True)
        log(f"FOREGROUND pid={pid} {info['name']} ({why})")
    return True


def pause(p, st, dry, why, kind="pressure"):
    if not dry:
        try:
            os.kill(p["pid"], signal.SIGSTOP)
        except OSError as e:
            log(f"pause failed pid={p['pid']} {p['name']}: {e}")
            return False
        st.setdefault("paused", {})[str(p["pid"])] = dict(name=p["name"], t=time.time(), why=why, kind=kind,
                                                           start=start_time(p["pid"]), mem=p["mem"])
    log(f"PAUSE (SIGSTOP) pid={p['pid']} {p['name']} mem={p['mem'] >> 20}M ({why}){' [dry-run]' if dry else ''}")
    return True


def resume(pid, st, why, cooldown_s=0, dry=False):
    if dry:
        if str(pid) in st.get("paused", {}):
            log(f"RESUME pid={pid} ({why}) [dry-run]")
        return False
    info = st.get("paused", {}).pop(str(pid), None)
    if info is None:
        return False
    if start_time(pid) == info.get("start"):  # guard against pid reuse
        try:
            os.kill(pid, signal.SIGCONT)
        except OSError:
            pass
        log(f"RESUME (SIGCONT) pid={pid} {info['name']} after {int(time.time() - info['t'])}s ({why})")
        if cooldown_s:
            st.setdefault("cooldown", {})[str(pid)] = time.time() + cooldown_s
    else:
        log(f"forget pid={pid} {info['name']} (process gone)")
    return True


def resume_all(st, why):
    for pid in [int(k) for k in list(st.get("paused", {}))]:
        resume(pid, st, why)


# ---------- modes ----------
def detect_mode(conf, procs, st):
    # one user-facing mode ("Max"); DAW/build detection only sets internal priorities
    names = [p["name"] for p in procs.values()]
    if any(matches(n, globs(conf.get("modes", "studio_apps", fallback=""))) for n in names):
        return "studio"
    build_tools = globs(conf.get("modes", "build_tools", fallback=""))
    compilers = globs(conf.get("build_guard", "compilers", fallback=""))
    if any(matches(n, build_tools) for n in names) and sum(matches(n, compilers) for n in names) >= 1:
        return "build"
    return "normal"


def mode_cfg(conf, mode, key, default):
    return conf.get(f"mode.{mode}", key, fallback=default)


# ---------- the 20 GB pool ----------
def rate(hist, now, idx, win):
    """bytes/s growth of column idx over the last win seconds."""
    r = [h for h in hist if now - h[0] <= win + 1 and len(h) > idx]
    if len(r) < 2 or r[-1][0] <= r[0][0]:
        return 0.0
    return (r[-1][idx] - r[0][idx]) / (r[-1][0] - r[0][0])


def pool_state(conf, st, m, g, now, swapping_mb):
    """One state for RAM + VRAM: green/yellow/orange/red plus what's driving it."""
    P = conf["pool"] if conf.has_section("pool") else {}
    L = conf["levels"]
    ram = int(sh("sysctl", "-n", "hw.memsize").strip() or 16 << 30)
    vtot = conf.getint("vram", "total_mb", fallback=4096) << 20
    vused = g["used"] if g else 0
    spill = g["sys"] if g else 0
    disk_free, lat = disk_probe()
    lats = (st.get("disk_lat") or [])[-5:] + ([lat] if lat is not None else [])
    st["disk_lat"] = lats
    lat_med = sorted(lats)[len(lats) // 2] if lats else 0
    tight = disk_free < size(P.get("disk_min_free", "40G")) or lat_med >= float(P.get("disk_slow_ms", 50))

    swap_active = swapping_mb >= conf.getfloat("general", "swap_active_mb", fallback=8) or m["pressure"] > 1
    eff_swap = m["swap"] if swap_active else 0
    why = []
    if m["pressure"] >= 4 or eff_swap >= size(L["red_swap"]):
        level = "red"; why.append("critical pressure" if m["pressure"] >= 4 else f"swapping with {m['swap'] >> 20}M swap")
    elif eff_swap >= size(L["orange_swap"]) or (swap_active and m["free"] < size(L["orange_free"])):
        level = "orange"; why.append(f"swapping {swapping_mb:.0f}MB/tick, free {m['free'] >> 20}M")
    elif eff_swap >= size(L["yellow_swap"]) or m["pressure"] >= 2:
        level = "yellow"; why.append("pressure warn" if m["pressure"] >= 2 else f"swap {m['swap'] >> 20}M active")
    else:
        level = "green"
    # storage-aware: swap on a nearly full or slow disk is what freezes the Mac, so tighten earlier
    if tight and level == "green" and (swap_active or m["avail"] < size(P.get("tight_avail", "4G"))):
        level = "yellow"; why.append(f"disk {'slow' if lat_med >= float(P.get('disk_slow_ms', 50)) else 'low'} and RAM tight")

    # predictive: time to exhaustion from the last window of available memory + swap-out rate
    PR = conf["predict"] if conf.has_section("predict") else {}
    hist = st["hist"]
    win = float(PR.get("window_s", 30))
    recent = [h for h in hist if now - h[0] <= win + 1]
    tte = None
    if len(recent) >= 2 and recent[-1][0] > recent[0][0]:
        drop = -rate(hist, now, 1, win)
        r = drop + sum(h[3] for h in recent[1:]) / max(1, len(recent) - 1) * (1 << 20)
        if r >= size(PR.get("min_rate", "50M")):
            tte = m["avail"] / r
    st["tte"] = round(tte, 1) if tte is not None else None
    if tte is not None and tte < float(PR.get("tte_s", 60)) and LEVELS.index(level) < 2 \
            and m["avail"] < size(PR.get("avail_below", "3G")):
        log(f"PREDICT exhaustion in {tte:.0f}s (avail {m['avail'] >> 20}M) -> orange early")
        level = "orange"; why.append(f"exhaustion predicted in {tte:.0f}s")

    spill_rate = rate(hist, now, 5, float(P.get("spill_window_s", 30)))  # bytes/s of GPU memory moving into RAM
    spilling = spill_rate >= size(P.get("spill_rate", "8M"))
    spd, sched = thermal()
    load = loadavg()
    seg = dict(app=m["app"], wired=max(0, m["wired"] - spill), compressed=m["compressed"], spill=spill,
               vram=vused, cached=m["cached"],
               free=max(0, ram - m["app"] - m["wired"] - m["compressed"] - m["cached"]) + max(0, vtot - vused))
    return dict(state=level, why=why, total=ram + vtot, ram=ram, vram_total=vtot, seg=seg, swap=m["swap"],
                swap_rate=round(swapping_mb / max(1, now - (hist[-2][0] if len(hist) > 1 else now - 10)), 1),
                spill_rate=round(spill_rate / (1 << 20), 1), spilling=spilling, swap_active=swap_active,
                vram_pct=round(100 * vused / vtot, 1), thermal=spd, sched=sched, load=load,
                cpu_load=round(100 * load[0] / (os.cpu_count() or 16)), disk_free=disk_free, disk_lat=lat_med,
                tight=tight, avail=m["avail"])


def set_xcode_jobs(n, st, dry, why):
    """Xcode/xcodebuild read this when a build starts, so it sets the parallelism of the next build."""
    if st.get("xcode_jobs") == n:
        return
    if not dry:
        subprocess.run(["defaults", "write", "com.apple.dt.Xcode", "IDEBuildOperationMaxNumberOfConcurrentCompileTasks",
                        "-int", str(n)], capture_output=True)
        st["xcode_jobs"] = n
    log(f"XCODE jobs -> {n} ({why}){' [dry-run]' if dry else ''}")


AUTO_BG = ("pool", "level", "budget", "spill", "thermal", "mode build")


# ---------- the tick ----------
def tick(dry=False, force=None):
    conf = load_conf()
    st = load_state()
    dry = dry or conf.getboolean("general", "dry_run", fallback=False)
    if os.path.exists(OFF_FILE):
        return
    now = time.time()
    m = metrics()
    g = gpu_full()
    last_out = st.get("swapouts", m["swapouts"])
    swapping_mb = max(0, m["swapouts"] - last_out) * PAGE / (1 << 20)
    st["swapouts"] = m["swapouts"]

    # trend history (last 10 min): t, avail, swap, swap-out MB/s, vram used, gpu spill
    hist = [h for h in st.get("hist", []) if now - h[0] <= HISTORY_S]
    dt_last = now - hist[-1][0] if hist else 0
    hist.append([round(now, 1), m["avail"], m["swap"], round(swapping_mb / dt_last, 1) if dt_last else 0,
                 g["used"] if g else 0, g["sys"] if g else 0])
    st["hist"] = hist

    pool = pool_state(conf, st, m, g, now, swapping_mb)
    hist[-1] += [LEVELS.index(pool["state"]), pool["thermal"]]  # for the health score's 10-minute view
    level = force or pool["state"]
    prev = st.get("level", "green") if st.get("level") in LEVELS else "green"
    if not force and LEVELS.index(level) < LEVELS.index(prev) - 1:
        level = LEVELS[LEVELS.index(prev) - 1]  # step down one level per tick: no orange/green flapping
    pool["state"] = level
    lvl = LEVELS.index(level)
    L, P = conf["levels"], (conf["pool"] if conf.has_section("pool") else {})

    procs = proc_list()
    compilers = globs(conf.get("build_guard", "compilers", fallback=""))
    build_tools = globs(conf.get("modes", "build_tools", fallback=""))
    near = lvl >= 1 or pool["swap_active"] or st.get("paused") or st.get("throttled") or pool["tight"] \
        or any(matches(p["name"], compilers) for p in procs.values())
    if near:
        add_footprint(procs)
    mode = detect_mode(conf, procs, st)
    if mode != st.get("mode"):
        log(f"MODE {st.get('mode', '?')} -> {mode}")
        for k, info in list(st.get("bg", {}).items()):
            if info.get("why", "").startswith("mode "):
                unbackground(int(k), st, f"left mode {st.get('mode')}", dry=dry)
    st["mode"] = mode

    # priority: foreground app / DAW (never touched) > builds > background
    fpid = front_pid()
    daws = [p["pid"] for p in procs.values() if matches(p["name"], globs(conf.get("modes", "studio_apps", fallback="")))]
    fg = tree(procs, ([fpid] if fpid else []) + daws)
    st["foreground"] = procs[fpid]["name"] if fpid in procs else None
    st.setdefault("front_track_t", now)
    if fpid in procs:
        st.setdefault("front_seen", {})[procs[fpid]["name"]] = now
    is_build = lambda p: matches(p["name"], compilers) or matches(p["name"], build_tools)

    summary = (f"pool={level} mode={mode} swap={m['swap'] >> 20}M free={m['free'] >> 20}M avail={m['avail'] >> 20}M "
               f"pressure={m['pressure']} swapout={swapping_mb:.0f}MB/tick vram={pool['vram_pct']:.0f}% "
               f"spill={pool['spill_rate']}MB/s speed={pool['thermal']}% disk={pool['disk_free'] >> 30}G/"
               f"{pool['disk_lat']}ms paused={len(st.get('paused', {}))}")
    if level != st.get("level"):
        log(f"POOL {st.get('level', '?')} -> {level}: {summary}" + (f" ({'; '.join(pool['why'])})" if pool["why"] else ""))
        if lvl >= 2:
            notify(f"Memory pool {level.upper()}", f"swap {m['swap'] >> 20} MB, free {m['free'] >> 20} MB")
        st["heartbeat"] = now
        st["level_since"] = now
    elif now - st.get("heartbeat", 0) > conf.getfloat("general", "heartbeat_min", fallback=5) * 60:
        log("heartbeat " + summary)
        st["heartbeat"] = now
    st["level"] = level

    for k in list(st.get("paused", {})):
        if int(k) not in procs:
            st["paused"].pop(k)
            log(f"forget pid={k} (exited)")
    for k in list(st.get("bg", {})):
        if int(k) not in procs:
            st["bg"].pop(k)
    st["cooldown"] = {k: v for k, v in st.get("cooldown", {}).items() if v > now}

    # max-pause safety net: nothing stays frozen forever
    max_s = conf.getfloat("resume", "max_pause_min", fallback=15) * 60
    bmax_s = conf.getfloat("build_guard", "max_pause_min", fallback=60) * 60
    cool_s = conf.getfloat("resume", "repause_cooldown_min", fallback=10) * 60
    for k, info in list(st.get("paused", {}).items()):
        limit = bmax_s if info.get("kind") == "build" else max_s
        if now - info["t"] >= limit:
            resume(int(k), st, f"max pause {limit / 60:g} min reached", cooldown_s=cool_s, dry=dry)
            notify("Resumed after timeout", f"{info['name']} (pid {k}) was paused {int((now - info['t']) / 60)} min")

    elig = [p for p in eligible(procs, conf, st) if p["pid"] not in fg]
    bgc = [p for p in elig if not is_build(p)]          # the background class
    gpu_heavy = globs(conf.get("vram", "gpu_heavy", fallback=""))
    acts = []

    # 1. GPU spilling into RAM while RAM is under pressure: background the GPU-heavy background apps
    #    first; compilers get a grace period before the build guard tightens
    if pool["spilling"] and lvl >= 1:
        for p in bgc:
            if matches(p["name"], gpu_heavy) and background(p, st, dry, f"spill {pool['spill_rate']}MB/s at {level}"):
                acts.append("spill")
        st["spill_t"] = now
    spill_grace = now - st.get("spill_t", 0) < float(P.get("spill_grace_s", 20))

    # 2. thermal throttling: cut compiler parallelism first, then background the UI apps if it persists
    spd = pool["thermal"]
    if spd < 100:
        if st.get("thermal_ok", True):
            log(f"THERMAL throttling: CPU_Speed_Limit={spd} scheduler={pool['sched']}")
            notify("CPU thermal throttling", f"speed limit {spd}%")
            st["thermal_since"] = now
        st["thermal_ok"] = False
        if now - st.get("thermal_since", now) >= float(P.get("thermal_ui_after_s", 60)):
            for p in bgc:
                if matches(p["name"], gpu_heavy + globs(mode_cfg(conf, "build", "background", ""))):
                    background(p, st, dry, f"thermal {spd}%")
    elif not st.get("thermal_ok", True):
        log(f"THERMAL ok again (speed limit {spd})")
        st["thermal_ok"] = True

    # Xcode parallelism for the next build follows the pool: 8 healthy, 4 yellow, 2 orange+/throttling
    J = lambda k, d: int(P.get(k, d))
    want = J("jobs_pressure", 2) if (lvl >= 2 or spd < 100) else J("jobs_normal", 4) if lvl == 1 \
        else J("jobs_healthy", 8)
    cur = st.get("xcode_jobs")
    if cur is None or want <= cur:
        st["jobs_low_t"] = now
    elif now - st.get("jobs_low_t", 0) < 60:
        want = cur  # raise only once the higher value has been wanted for 60 s straight
    set_xcode_jobs(want, st, dry, f"pool {level}, speed {spd}%")

    # mode: studio always keeps the DAW's competition backgrounded; build mode only when the pool needs it
    if mode == "studio" or (mode == "build" and lvl >= 1):
        for p in bgc:
            if matches(p["name"], globs(mode_cfg(conf, mode, "background", ""))):
                background(p, st, dry, f"mode {mode}")

    # 3. soft per-app budgets: act only when the pool is yellow+ (or the disk is tight)
    scale = float(mode_cfg(conf, mode, "budget_scale", "1"))
    for pat, lim in conf.items("budgets"):
        limit = int(size(lim) * scale)
        group = [p for p in bgc if matches(p["name"], [pat])]
        total = sum(p["mem"] for p in group)
        key = "budget:" + pat
        if total > limit and (lvl >= 1 or pool["tight"]):
            if not st.setdefault("over", {}).get(key):
                log(f"BUDGET {pat} over: {total >> 20}M > {limit >> 20}M at pool {level}")
                st["over"][key] = now
                for p in group:
                    renice(p, dry, f"budget {pat}")
                    background(p, st, dry, f"budget {pat}")
            elif lvl >= 2 and group:
                top = max(group, key=lambda p: p["mem"])
                if pause(top, st, dry, f"budget {pat} {total >> 20}M > {limit >> 20}M"):
                    bgc = [p for p in bgc if p["pid"] != top["pid"]]
        else:
            st.setdefault("over", {}).pop(key, None)

    # 4. pressure on the background class (largest first), never the foreground app or the build
    if lvl >= 1:
        heavy = globs(conf.get("heavy", "names", fallback=""))
        for p in bgc:
            if matches(p["name"], heavy) or p["mem"] >= size(L["red_proc_min"]):
                renice(p, dry, f"level {level}")
                background(p, st, dry, f"level {level}")
    if lvl == 2 and bgc:
        top = max(bgc, key=lambda p: p["mem"])
        if top["mem"] >= size(P.get("pause_min", "200M")):
            pause(top, st, dry, "pool orange, largest background process")
    if lvl == 3:
        for p in sorted(bgc, key=lambda p: -p["mem"]):
            if p["mem"] >= size(L["red_proc_min"]):
                pause(p, st, dry, f"pool red, >= {L['red_proc_min']}")

    # compilers paused for pressure (by older versions, or by hand-off) belong to the build guard
    for k, info in st.get("paused", {}).items():
        if info.get("kind") != "build" and matches(info["name"], compilers) and info.get("why") != "manual":
            info["kind"] = "build"

    # 5. build guard: summed compiler memory capped; the cap tightens with the pool (not during the spill grace),
    #    and a job-count cap applies while the CPU is throttling. The oldest job always runs.
    if conf.getboolean("build_guard", "enabled", fallback=True):
        budget = size(mode_cfg(conf, mode, "compiler_budget", conf.get("build_guard", "budget", fallback="6G")))
        if not spill_grace:
            budget = int(budget * {0: 1, 1: 1, 2: 0.5, 3: 0.25}[lvl])
        max_jobs = J("thermal_jobs", 4) if spd < 100 else 10 ** 6
        running = [p for p in elig if matches(p["name"], compilers) and "T" not in p["stat"]]
        for p in running:
            p["started"] = start_epoch(p["pid"])
        running.sort(key=lambda p: (p["started"], p["pid"]))
        total = n = 0
        for p in running:
            total += p["mem"]; n += 1
            if (total > budget or n > max_jobs) and p is not running[0]:
                why = f"build guard {total >> 20}M > {budget >> 20}M" if total > budget else \
                    f"build guard: {max_jobs} jobs while CPU at {spd}%"
                if pause(p, st, dry, why, kind="build"):
                    total -= p["mem"]; n -= 1
        held = sorted(((k, i) for k, i in st.get("paused", {}).items() if i.get("kind") == "build"),
                      key=lambda kv: kv[1]["t"])
        for k, info in held:
            if (total + info.get("mem", 0) <= budget and n < max_jobs) or total == 0:
                resume(int(k), st, f"build guard: room under {budget >> 20}M", dry=dry)
                total += info.get("mem", 0); n += 1
            else:
                break
        st["compilers_mem"], st["compilers_budget"] = total, budget

    # 6. healthy pool: release everything (resume one per tick, smallest first; un-background after 60 s calm)
    R = conf["resume"]
    calm = m["pressure"] == 1 and swapping_mb < 1 and m["avail"] >= size(R["resume_free"])
    if force:
        calm = lvl == 0
    if lvl == 0 and calm:
        pp = [(k, i) for k, i in st.get("paused", {}).items() if i.get("kind") != "build"]
        if pp:
            k, info = min(pp, key=lambda kv: kv[1].get("mem", 0))
            resume(int(k), st, "pool healthy", dry=dry)
        elif now - st.get("level_since", now) >= conf.getfloat("resume", "unbackground_after_s", fallback=60):
            for k, info in list(st.get("bg", {}).items()):
                w = info.get("why", "")
                if w.startswith(AUTO_BG) and not (w.startswith("thermal") and spd < 100):
                    unbackground(int(k), st, "pool healthy", dry=dry)
            st["over"] = {}

    # CPU governor: sustained hot background processes -> background QoS, then duty-cycle; release when calm
    C = conf["cpu"] if conf.has_section("cpu") else {}
    if C.get("enabled", "true").lower() == "true":
        limit, sustain = float(C.get("limit_pct", 300)), float(C.get("sustain_s", 60))
        duty = float(C.get("duty", 0.5))
        hot = st.setdefault("cpu_hot", {})
        watch = globs(C.get("watch", ""))
        for p in bgc:
            k = str(p["pid"])
            if p["cpu"] >= limit:
                first = hot.setdefault(k, now)
                if now - first >= sustain:
                    if k not in st.get("bg", {}):
                        background(p, st, dry, f"cpu {p['cpu']:.0f}% > {limit:.0f}% for {sustain:.0f}s")
                    elif now - first >= 2 * sustain and k not in st.get("throttled", {}) and not dry:
                        start_throttle(p, st, duty, f"cpu {p['cpu']:.0f}% still > {limit:.0f}%")
            else:
                hot.pop(k, None)
            if matches(p["name"], watch) and p["cpu"] >= float(C.get("watch_pct", 100)) \
                    and now - st.setdefault("cpu_noted", {}).get(p["name"], 0) > 600:
                st["cpu_noted"][p["name"]] = now
                log(f"CPU note: {p['name']} pid={p['pid']} at {p['cpu']:.0f}% (informational)")
        for k in list(hot):
            if int(k) not in procs or int(k) in fg:
                hot.pop(k)
        release = float(C.get("release_pct", limit / 2))
        for k, info in list(st.get("throttled", {}).items()):
            pr = procs.get(int(k))
            # while duty-cycled, ps %cpu reads ~duty*real, so compare against release*duty
            if pr is None or pr["cpu"] < release * info.get("duty", 1) or int(k) in fg:
                stop_throttle(int(k), st, "cpu calmed down" if pr and int(k) not in fg else
                              "now in the foreground" if pr else "exited")
                hot.pop(k, None)
                if pr and k in st.get("bg", {}) and st["bg"][k]["why"].startswith("cpu"):
                    unbackground(int(k), st, "cpu calmed down", dry=dry)
        for k, info in list(st.get("bg", {}).items()):
            pr = procs.get(int(k))
            if info["why"].startswith("cpu") and k not in st.get("throttled", {}) and k not in hot \
                    and pr and pr["cpu"] < release:
                unbackground(int(k), st, "cpu calmed down", dry=dry)

    # the app the user switched to is never left backgrounded by an automatic action
    for k, info in list(st.get("bg", {}).items()):
        if int(k) in fg and info.get("why", "").startswith(AUTO_BG + ("boost",)):
            unbackground(int(k), st, "now in the foreground", dry=dry)

    st["cpu_speed_limit"], st["cpu_sched_limit"], st["load"] = spd, pool["sched"], pool["load"]
    st["gpu_util"] = g["util"] if g else None
    if g:
        st["vram_used"], st["vram_total"], st["vram_pct"] = g["used"], pool["vram_total"], pool["vram_pct"]
    st["gpu_hist"] = [x for x in st.get("gpu_hist", []) if now - x[0] <= 60] + \
        ([[round(now, 1), g["util"]]] if g and g["util"] is not None else [])
    pool["actions"] = acts
    st["pool"] = pool
    st["updated"] = now
    if now - st.get("health_t", 0) >= 300:
        st["health_t"] = now
        try:
            track_agents(st)
            track_ssd()
        except Exception as e:
            log(f"tracking error: {e}")
        try:
            record_health(st)
        except Exception as e:  # health is informational; never break the tick
            log(f"health error: {e}")
    save_state(st)


# ---------- junk report (read-only) ----------
JUNK = [
    ("Xcode DerivedData", "~/Library/Developer/Xcode/DerivedData", "rm -rf ~/Library/Developer/Xcode/DerivedData/*"),
    ("Xcode Archives", "~/Library/Developer/Xcode/Archives", "open ~/Library/Developer/Xcode/Archives  # delete old ones in Finder"),
    ("iOS DeviceSupport", "~/Library/Developer/Xcode/iOS DeviceSupport", "rm -rf ~/Library/Developer/Xcode/iOS\\ DeviceSupport/*"),
    ("macOS DeviceSupport", "~/Library/Developer/Xcode/macOS DeviceSupport", "rm -rf ~/Library/Developer/Xcode/macOS\\ DeviceSupport/*"),
    ("Simulator caches", "~/Library/Developer/CoreSimulator/Caches", "xcrun simctl delete unavailable && rm -rf ~/Library/Developer/CoreSimulator/Caches/*"),
    ("Simulator devices", "~/Library/Developer/CoreSimulator/Devices", "xcrun simctl delete unavailable"),
    ("Xcode module cache", "~/Library/Caches/com.apple.dt.Xcode", "rm -rf ~/Library/Caches/com.apple.dt.Xcode"),
    ("User caches (all)", "~/Library/Caches", "open ~/Library/Caches  # pick per app; apps rebuild them"),
    ("Homebrew cache", "~/Library/Caches/Homebrew", "brew cleanup --prune=all"),
    ("npm cache", "~/.npm/_cacache", "npm cache clean --force"),
    ("pip cache", "~/Library/Caches/pip", "python3 -m pip cache purge"),
    ("Trash", "~/.Trash", "Finder > Empty Trash  (permanent)"),
]


def junk():
    total = 0
    print("Read-only report: nothing is deleted. Run a command yourself if you want to free that space.\n")
    for label, path, cmd in JUNK:
        p = os.path.expanduser(path)
        if not os.path.exists(p):
            continue
        out = sh("du", "-sk", p).split()
        kb = int(out[0]) if out and out[0].isdigit() else 0
        if label != "User caches (all)":
            total += kb
        print(f"{kb / 1048576:8.2f} GB  {label:<20} {path}\n            delete: {cmd}")
    print(f"\n~{total / 1048576:.1f} GB in the specific items above (User caches total listed separately).")


# ---------- bench ----------
def bench(seconds=60, step=2):
    os.makedirs(os.path.join(HOME, "bench"), exist_ok=True)
    path = os.path.join(HOME, "bench", time.strftime("bench-%Y%m%d-%H%M%S.csv"))
    last = metrics()["swapouts"]
    with open(path, "w") as f:
        f.write("time,cpu_speed_limit,sched_limit,load1,swap_mb,swapout_mb_s,free_mb,vram_mb,gpu_pct\n")
        end = time.time() + seconds
        while time.time() < end:
            t0 = time.time()
            m, (spd, sched), (v, u) = metrics(), thermal(), gpu_stats()
            rate = max(0, m["swapouts"] - last) * PAGE / (1 << 20) / step
            last = m["swapouts"]
            row = [time.strftime("%H:%M:%S"), spd, sched, loadavg()[0], m["swap"] >> 20, round(rate, 1),
                   m["free"] >> 20, (v or 0) >> 20, u if u is not None else ""]
            f.write(",".join(map(str, row)) + "\n"); f.flush()
            time.sleep(max(0, step - (time.time() - t0)))
    print(path)
    print(open(path).read())
    return 0


def bench_run(label, cmd, guard_swap=None, guard_avail=None, step=2, logfile=None):
    """Runs cmd (shell) as its own process group and samples the machine every `step` s until it exits.
    guard_*: abort (SIGINT, then SIGTERM; never SIGKILL) if swap grows by more than guard_swap over the start,
    or available memory (free + inactive + purgeable) drops below guard_avail. Writes CSV + JSON in bench/."""
    os.makedirs(os.path.join(HOME, "bench"), exist_ok=True)
    base = os.path.join(HOME, "bench", time.strftime("run-%Y%m%d-%H%M%S-") + label)
    m0 = metrics()
    w0 = ssd_counters()[0]
    r = dict(label=label, cmd=cmd, memguard="off" if os.path.exists(OFF_FILE) else "on", start=time.time(),
             swap_start=m0["swap"], peak_swap=m0["swap"], min_free=m0["free"], min_avail=m0["avail"],
             min_speed=100, max_pressure=m0["pressure"], peak_compressed=m0["compressed"], max_load=0,
             peak_vram=0, peak_spill=0, aborted=None, xcode_jobs=subprocess.run(
                 ["defaults", "read", "com.apple.dt.Xcode", "IDEBuildOperationMaxNumberOfConcurrentCompileTasks"],
                 capture_output=True, text=True).stdout.strip() or "default (= CPU count)")
    out = open(logfile or base + ".log", "w")
    proc = subprocess.Popen(cmd, shell=True, stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
    last, t_abort = m0["swapouts"], None
    with open(base + ".csv", "w") as f:
        f.write("t,swap_mb,swapout_mb_s,free_mb,avail_mb,compressed_mb,pressure,speed_limit,load1,vram_mb,spill_mb,paused\n")
        while proc.poll() is None:
            t0 = time.time()
            m, (spd, _), g, ld = metrics(), thermal(), gpu_full() or dict(used=0, sys=0), loadavg()[0]
            rate_ = max(0, m["swapouts"] - last) * PAGE / (1 << 20) / step
            last = m["swapouts"]
            r["peak_swap"] = max(r["peak_swap"], m["swap"]); r["min_free"] = min(r["min_free"], m["free"])
            r["min_avail"] = min(r["min_avail"], m["avail"]); r["min_speed"] = min(r["min_speed"], spd)
            r["max_pressure"] = max(r["max_pressure"], m["pressure"]); r["max_load"] = max(r["max_load"], ld)
            r["peak_compressed"] = max(r["peak_compressed"], m["compressed"])
            r["peak_vram"] = max(r["peak_vram"], g["used"]); r["peak_spill"] = max(r["peak_spill"], g["sys"])
            f.write(",".join(map(str, [round(t0 - r["start"]), m["swap"] >> 20, round(rate_, 1), m["free"] >> 20,
                                       m["avail"] >> 20, m["compressed"] >> 20, m["pressure"], spd, ld,
                                       g["used"] >> 20, g["sys"] >> 20, len(load_state().get("paused", {}))])) + "\n")
            f.flush()
            if not r["aborted"] and ((guard_swap and m["swap"] - m0["swap"] > guard_swap) or
                                     (guard_avail and m["avail"] < guard_avail)):
                r["aborted"] = (f"swap +{(m['swap'] - m0['swap']) >> 20} MB" if guard_swap and m["swap"] - m0["swap"] > guard_swap
                                else f"available memory {m['avail'] >> 20} MB") + f" at {round(t0 - r['start'])} s"
                log(f"BENCH {label}: safety abort ({r['aborted']})")
                os.killpg(proc.pid, signal.SIGCONT); os.killpg(proc.pid, signal.SIGINT); t_abort = time.time()
            if t_abort and time.time() - t_abort > 30:
                os.killpg(proc.pid, signal.SIGTERM); t_abort = None
            time.sleep(max(0, step - (time.time() - t0)))
    m1 = metrics()
    r.update(end=time.time(), seconds=round(time.time() - r["start"]), rc=proc.returncode,
             swapout_mb=round((m1["swapouts"] - m0["swapouts"]) * PAGE / (1 << 20)), swap_end=m1["swap"],
             ssd_written_mb=round((ssd_counters()[0] - w0) / (1 << 20)))
    with open(base + ".json", "w") as f:
        json.dump(r, f, indent=1)
    print(json.dumps(r, indent=1))
    return r


# ---------- SSD writes (swap churn is SSD wear) ----------
def ssd_counters():
    """(bytes written, bytes read) on the internal disk since boot, from IOKit (IOBlockStorageDriver)."""
    out = sh("ioreg", "-r", "-c", "IOBlockStorageDriver", "-w0")
    best = (0, 0)
    for blk in re.findall(r'"Statistics" = \{([^}]*)\}', out):
        w = re.search(r'"Bytes \(Write\)"=(\d+)', blk); r = re.search(r'"Bytes \(Read\)"=(\d+)', blk)
        if w and r and int(w.group(1)) + int(r.group(1)) > sum(best):
            best = (int(w.group(1)), int(r.group(1)))
    return best


def track_ssd():
    """Per-day written bytes in maint.json (survives reboots: a counter that went down means a reboot)."""
    d = load_maint()
    w, r = ssd_counters()
    t = d.setdefault("ssd", {})
    day = time.strftime("%Y-%m-%d")
    last = t.get("last_w")
    if last is None:  # first record: count since boot only if the Mac booted today
        bt = int((re.search(r"sec = (\d+)", sh("sysctl", "-n", "kern.boottime")) or [0, 0])[1])
        inc = w if time.strftime("%Y-%m-%d", time.localtime(bt)) == day else 0
    else:
        inc = w - last if w >= last else w  # counter went down: rebooted
    t["last_w"] = w
    days = t.setdefault("days", {})
    days[day] = days.get(day, 0) + inc
    t["days"] = dict(sorted(days.items())[-30:])
    t["boot_w"], t["boot_r"], t["t"] = w, r, time.time()
    save_maint(d)


def track_agents(st):
    """First-seen RSS of each running third-party user LaunchAgent pid, to spot leaks for `boost`."""
    seen = st.setdefault("agent_rss", {})
    rss = {int(l.split()[0]): int(l.split()[1]) * 1024 for l in sh("ps", "-axo", "pid=,rss=").splitlines() if l.split()}
    for ln in sh("launchctl", "list").splitlines()[1:]:
        f = ln.split("\t")
        if len(f) == 3 and f[0].isdigit() and not f[2].startswith(("com.apple.", "application.")):
            pid = int(f[0])
            if seen.get(f[2], {}).get("pid") != pid and pid in rss:
                seen[f[2]] = dict(pid=pid, rss=rss[pid], t=time.time())


SAFE_QUIT_DEFAULT = "Music, TV, Podcasts, News, Stocks, Weather, Maps, App Store, System Settings, Photos, Preview, " \
                    "Calculator, Dictionary, Font Book, Books, Home, Freeform"
NEVER_QUIT = "Finder, Xcode*, Claude*, Terminal, iTerm2, MemGuard, Dock, loginwindow, SystemUIServer"


def gui_apps():
    """Running regular apps: [{name, pid, bundle}]"""
    out = []
    for blk in sh("lsappinfo", "list").split("\n\n"):
        if 'type="Foreground"' not in blk:
            continue
        n = re.search(r'^\s*\d+\) "([^"]+)"', blk, re.M); p = re.search(r"pid = (\d+)", blk)
        b = re.search(r'bundleID="([^"]+)"', blk)
        if n and p:
            out.append(dict(name=n.group(1), pid=int(p.group(1)), bundle=b.group(1) if b else ""))
    return out


def snapshot():
    m = metrics()
    return dict(free=m["free"], avail=m["avail"], app=m["app"], compressed=m["compressed"], swap=m["swap"],
                pressure=m["pressure"], load=loadavg()[0], t=time.time())


def boost(quit_names=None, as_json=False):
    """Speed Up: only real actions, then the measured before/after. Never purge, never SIGKILL."""
    conf, st = load_conf(), load_state()
    before = snapshot()
    procs = add_footprint(proc_list())
    acts, now = [], time.time()
    compilers = globs(conf.get("build_guard", "compilers", fallback=""))
    daws = globs(conf.get("modes", "studio_apps", fallback=""))
    fpid = front_pid()
    fg = tree(procs, ([fpid] if fpid else []) + [p["pid"] for p in procs.values() if matches(p["name"], daws)])

    if quit_names:  # graceful quit through the app itself (it asks to save unsaved documents)
        for name in quit_names:
            if matches(name, globs(NEVER_QUIT) + daws):
                acts.append(f"skipped {name} (never quit by boost)"); continue
            r = subprocess.run(["osascript", "-e", f'tell application "{name}" to quit'], capture_output=True, text=True,
                               timeout=30)
            acts.append(f"asked {name} to quit" + (f" ({r.stderr.strip()[:80]})" if r.returncode else ""))
            log(f"BOOST quit {name} (graceful)")
        time.sleep(3)
    else:
        # 1. release what is no longer needed: everything paused (non-build) when the pool isn't orange/red
        if LEVELS.index(st.get("level", "green")) < 2:
            for k, info in list(st.get("paused", {}).items()):
                if info.get("kind") != "build" and resume(int(k), st, "boost"):
                    acts.append(f"resumed {info['name']} ({k})")
        # 2. every non-foreground heavy app to background QoS (released when you switch to it)
        prot = protected_set(conf)
        for p in procs.values():
            if p["pid"] in fg or matches(p["name"], prot + compilers) or "Z" in p["stat"] or p["pid"] == os.getpid():
                continue
            if p["mem"] >= 300 << 20 and background(p, st, False, "boost"):
                acts.append(f"background QoS: {p['name']} ({p['pid']}, {p['mem'] >> 20} MB)")
        # 3. orphaned compile jobs (their build is gone: parent is launchd)
        for p in procs.values():
            if matches(p["name"], compilers) and p["ppid"] == 1:
                try:
                    os.kill(p["pid"], signal.SIGCONT); os.kill(p["pid"], signal.SIGTERM)
                    acts.append(f"ended orphaned {p['name']} ({p['pid']}, {p['mem'] >> 20} MB)")
                    log(f"BOOST SIGTERM orphaned {p['name']} pid={p['pid']}")
                except OSError:
                    pass
        # 4. leaky third-party LaunchAgents that restart themselves (KeepAlive): RSS > 2x what memguard first saw
        import plistlib
        for label, info in st.get("agent_rss", {}).items():
            pr = procs.get(info["pid"])
            pl = os.path.expanduser(f"~/Library/LaunchAgents/{label}.plist")
            if not pr or label.startswith(("com.user.memguard", "com.mydaw.")) or not os.path.exists(pl):
                continue
            try:
                with open(pl, "rb") as f:
                    keep = bool(plistlib.load(f).get("KeepAlive"))
            except Exception:
                keep = False
            if keep and pr["rss"] > 2 * info["rss"] and pr["rss"] > 100 << 20:
                subprocess.run(["launchctl", "kickstart", "-k", f"gui/{MY_UID}/{label}"], capture_output=True)
                acts.append(f"restarted leaky agent {label} ({info['rss'] >> 20} -> {pr['rss'] >> 20} MB)")
                log(f"BOOST restarted {label}")
        # 5. idle Homebrew services
        brew = "/usr/local/bin/brew"
        if os.path.exists(brew):
            for ln in sh(brew, "services", "list").splitlines()[1:]:
                f = ln.split()
                if len(f) > 1 and f[1] == "started":
                    pr = [p for p in procs.values() if f[0] in p["path"]]
                    if pr and sum(p["cpu"] for p in pr) < 1:
                        subprocess.run([brew, "services", "stop", f[0]], capture_output=True)
                        acts.append(f"stopped idle brew service {f[0]} (brew services start {f[0]} to undo)")
        # 6. browsers: tab discarding is the browser's own (Brave Memory Saver policy is on)
        if subprocess.run(["defaults", "read", "com.brave.Browser", "HighEfficiencyModeEnabled"],
                          capture_output=True, text=True).stdout.strip() == "1":
            acts.append("Brave Memory Saver is on: Brave discards inactive tabs itself (no outside switch exists)")
        save_state(st)
        time.sleep(5)
    # idle apps: not frontmost for 30+ min, > 300 MB (memguard has watched since it started tracking)
    seen, since = st.get("front_seen", {}), st.get("front_track_t", now)
    safe = globs(conf.get("boost", "safe_quit", fallback=SAFE_QUIT_DEFAULT))
    idle = []
    for a in gui_apps():
        if a["pid"] == fpid or a["name"] in ("MemGuard",):
            continue
        mem = sum(procs[p]["mem"] for p in tree(procs, [a["pid"]]) if p in procs)
        idle_s = now - seen.get(a["name"], since)
        if idle_s >= 1800 and mem >= 300 << 20:
            idle.append(dict(name=a["name"], pid=a["pid"], mem=mem, idle_min=int(idle_s / 60),
                             safe=matches(a["name"], safe) and not matches(a["name"], globs(NEVER_QUIT) + daws),
                             never=matches(a["name"], globs(NEVER_QUIT) + daws)))
    after = snapshot()
    r = dict(before=before, after=after, actions=acts, idle=sorted(idle, key=lambda x: -x["mem"]), t=now)
    st = load_state(); st["boost"] = r; save_state(st)
    log(f"BOOST {len(acts)} actions; avail {before['avail'] >> 20} -> {after['avail'] >> 20} MB, "
        f"swap {before['swap'] >> 20} -> {after['swap'] >> 20} MB")
    if as_json:
        print(json.dumps(r))
    else:
        G = lambda b: f"{b / (1 << 30):.2f} GB"
        print("Speed Up (real actions only):")
        for a in acts or ["nothing needed doing"]:
            print("  - " + a)
        print(f"\n{'':<22}{'before':>12}{'after':>12}")
        for k, lab in (("avail", "available memory"), ("free", "free memory"), ("app", "app memory"),
                       ("compressed", "compressed"), ("swap", "swap used")):
            print(f"{lab:<22}{G(before[k]):>12}{G(after[k]):>12}")
        print(f"{'pressure':<22}{before['pressure']:>12}{after['pressure']:>12}\n{'load (1 min)':<22}"
              f"{before['load']:>12}{after['load']:>12}")
        if idle:
            print("\nIdle apps (not used for 30+ min, > 300 MB). Quit with: memguard boost --quit \"App, App\"")
            for i in r["idle"]:
                print(f"  [{'x' if i['safe'] else ' '}] {i['name']:<28} {i['mem'] >> 20:>6} MB  idle {i['idle_min']} min"
                      + ("  (never quit by boost)" if i["never"] else ""))
    return 0


# ---------- maintain ----------
def maintain(args):
    sys.path.insert(0, HOME)
    import maint
    sub = args[0] if args else "all"
    d = load_maint()
    H = maint.human
    if sub in ("scan", "all"):
        d["junk"] = maint.scan_junk(); d["junk_t"] = time.time()
        total = sum(c["size"] for c in d["junk"] if c["id"] not in ("trash", "optional"))
        print(f"== Junk: {H(total)} reclaimable (plus Trash {H(next((c['size'] for c in d['junk'] if c['id'] == 'trash'), 0))})")
        for c in d["junk"]:
            print(f"  {H(c['size']):>10}  {c['title']}  [{c['id']}] ({len(c['items'])} items)"
                  + (f"  -> run: {c['command']}" if c.get("command") else ""))
            for i in sorted(c["items"], key=lambda i: -i["size"])[:5]:
                print(f"      {H(i['size']):>10}  {i['path'].replace(os.path.expanduser('~'), '~')}  {i.get('note', '')}")
    if sub == "clean":
        cats = (args[1] if len(args) > 1 else "").split(",")
        freed = maint.clean(cats, log, d.get("junk"))
        print(f"moved {H(freed)} to the Trash (recoverable until you empty it)")
        d["junk"] = maint.scan_junk(); d["junk_t"] = time.time()
    if sub in ("startup", "all"):
        d["startup"] = maint.startup()
        print("== Startup items (scope | loaded | cpu | ram | label -> program)")
        for s_ in d["startup"]:
            print(f"  {s_['scope']:<16} {'on ' if s_.get('loaded') else 'off'} {s_.get('cpu', 0):>5}% "
                  f"{H(s_.get('rss', 0)):>9}  {s_['label']}  -> {s_['program']}")
    if sub in ("disable", "enable") and len(args) > 1:
        maint.set_agent(args[1], sub == "enable", log)
        print(f"{sub}d {args[1]}")
    if sub in ("security", "all"):
        d["security"] = maint.security()
        sec = d["security"]
        print(f"== Security: XProtect {sec['xprotect']} | Remediator {sec['xprotect_remediator']} | {sec['gatekeeper']} | "
              f"SIP {sec['sip']} | {sec['filevault']} | {sec['firewall']} | Spotlight: {sec['spotlight']}")
        for a in sec["persistence"]:
            mark = "FLAG" if a["flag"] else ("yours" if a["yours"] else "ok")
            print(f"  [{mark:^5}] {a['status']:<8} {a['label']}  {a['detail'][:70]}")
        if sec["broken_prefs"]:
            print("  broken preference files:", *sec["broken_prefs"], sep="\n    ")
    if sub in ("updates", "all"):
        print("== Updates (checking Apple + Homebrew, can take a minute)...", flush=True)
        d["updates"] = maint.updates()
        u = d["updates"]
        for m in u["macos"]:
            print(f"  macOS: {m['title']} {m['version']}{' (restart)' if m['restart'] else ''}  -> softwareupdate -i '{m['label']}'")
        if not u["macos"]:
            print("  macOS: " + (u["macos_raw"].splitlines()[-1] if u["macos_raw"] else "no updates"))
        for b in u["brew"]:
            print(f"  brew{' cask' if b.get('cask') else ''}: {b['name']} {b['installed']} -> {b['current']}")
        print(f"  {len(u['brew'])} Homebrew updates" + (" -> brew upgrade" if u["brew"] else ""))
    if sub == "large":
        d["large"] = maint.large_old()
        for r in d["large"][:40]:
            print(f"  {H(r['size']):>10}  last used {r['last_used']}  {r['path']}")
    if sub == "uninstall" and len(args) > 1:
        vend = args[args.index("--vendor") + 1] if "--vendor" in args else None
        r = maint.uninstall(args[1], log, vend, apply="--apply" in args)
        print(f"== {r['name']} ({r['bundle_id']}) app: {r['app']} {H(r.get('app_size', 0))}")
        for i in r["items"]:
            print(f"  {H(i['size']):>10}  {i['path']}")
        print(("moved to Trash: " + H(r.get("freed", 0))) if "--apply" in args else "preview only; add --apply to move these to the Trash")
    if sub in ("storage", "all"):
        print("== Storage" + (" (full: speed test, map, duplicates; a few minutes)" if sub == "storage" else ""), flush=True)
        d["storage"] = maint.storage(full=sub == "storage") if sub == "storage" else \
            dict(d.get("storage") or {}, **maint.storage(full=False))
        S = d["storage"]
        dr = S["drive"]
        print(f"  {dr['kind']} {dr['model']} ({dr['protocol']}), SMART {dr['smart']}, TRIM {dr['trim']}; "
              f"{H(dr['free'])} free + {H(dr['purgeable'])} purgeable of {H(dr['size'])}")
        if S.get("speed"):
            print(f"  speed: write {S['speed']['write_mb_s']} MB/s, read {S['speed']['read_mb_s']} MB/s (2 GB file)")
        print(f"  local snapshots: {len(S['snapshots'])}" + "".join(f"\n    {x['volume']}: {x['name']}" for x in S["snapshots"]))
        for x in S["special"]:
            print(f"  {x['label']:<22} {H(x['size']) if x['size'] is not None else '?':>10}  {x['note']}")
        for x in S.get("map", []):
            print(f"  {H(x['size']):>10}  {x['path'].replace(os.path.expanduser('~'), '~')}")
        if S.get("duplicates") is not None and sub == "storage":
            print(f"  duplicates over 50 MB: {len(S['duplicates'])} groups, {H(sum(g['wasted'] for g in S['duplicates']))} "
                  "reclaimable (preview only; the oldest copy is listed first)")
            for g in S["duplicates"][:15]:
                print(f"    {H(g['size'])} x{len(g['paths'])}: " + " | ".join(p.replace(os.path.expanduser('~'), '~') for p in g["paths"]))
    if sub == "trash" and len(args) > 1:  # one file, e.g. a duplicate from the Storage view
        print(f"moved {H(maint.move_to_trash(os.path.realpath(args[1]), log))} to the Trash")
    if sub == "needs":
        print(json.dumps(maint.needs(d, load_state())))
    if sub == "fix" and len(args) > 1:
        ok = maint.fix(args[1], log)
        print("done" if ok else "not done")
        if args[1] == "caches":
            d["junk"] = maint.scan_junk(); d["junk_t"] = time.time()
    if sub == "optimize":
        d["optimize"] = maint.optimize(d.get("storage"), apply="--apply" in args, log=log)
        print_optimize(d["optimize"])
    save_maint(d)
    if sub in ("health", "all"):
        score, why = record_health(load_state())
        print(f"== Health score: {score}/100" + ("" if not why else "  (" + "; ".join(why) + ")"))
    return 0


def print_optimize(o):
    print(f"{'area':<10} {'status':<9} {'item':<48} measured / action")
    for i in o["items"]:
        print(f"{i['area']:<10} {i['status']:<9} {i['title'][:48]:<48} {i['measured']}")
        if i["action"] and i["status"] not in ("ok", "applied", "n/a"):
            print(f"{'':<69} -> {i['action']}")
        if i.get("gain"):
            print(f"{'':<69} gain: {i['gain']}")


# ---------- CLI ----------
POOL_SEGS = [("app", "A", "app RAM"), ("wired", "W", "wired"), ("compressed", "C", "compressed"),
             ("spill", "S", "GPU spill"), ("vram", "V", "VRAM"), ("cached", "c", "cached"), ("free", ".", "free")]


def pool_bar(pool, width=60):
    """One text bar for the 20 GB pool; swap is drawn as overflow past the end."""
    tot = pool["total"]
    out, acc = "", 0.0
    for k, ch, _ in POOL_SEGS:
        acc += pool["seg"].get(k, 0) * width / tot
        out += ch * (round(acc) - len(out))
    over = round(pool["swap"] * width / tot)
    return "[" + out[:width].ljust(width, ".") + "]" + ("#" * over + " swap" if over else "")


def status():
    st, m = load_state(), metrics()
    pool = st.get("pool")
    pri = {"studio": "DAW first", "build": "build running"}.get(st.get("mode"), "")
    print(f"memguard: {'OFF' if os.path.exists(OFF_FILE) else 'ON (Max)'}  pool={st.get('level', '?')}  "
          f"foreground={st.get('foreground') or '?'}{'  priority: ' + pri if pri else ''}")
    if pool:
        G = lambda b: f"{b / (1 << 30):.1f}"
        print(f"\n20 GB pool ({G(pool['ram'])} RAM + {G(pool['vram_total'])} VRAM)  {pool_bar(pool)}")
        print("  " + "  ".join(f"{ch}={label} {G(pool['seg'].get(k, 0))}" for k, ch, label in POOL_SEGS))
        print(f"  swap {G(pool['swap'])} GB (overflow, #)  swap-out {pool['swap_rate']} MB/s  "
              f"VRAM {pool['vram_pct']:.0f}%  GPU spill {pool['spill_rate']:+} MB/s{' SPILLING' if pool['spilling'] else ''}")
        print(f"  CPU load {pool['cpu_load']}%  speed limit {pool['thermal']}%  disk free {G(pool['disk_free'])} GB, "
              f"write+fsync {pool['disk_lat']} ms{'  (TIGHT: budgets apply early)' if pool['tight'] else ''}")
        if pool.get("why"):
            print("  why: " + "; ".join(pool["why"]))
        print()
    print(f"swap used {m['swap'] >> 20} MB | free {m['free'] >> 20} MB | avail {m['avail'] >> 20} MB | pressure "
          + {1: "normal", 2: "warn", 4: "critical"}.get(m["pressure"], str(m["pressure"]))
          + (f" | exhaustion in ~{st['tte']:.0f}s" if st.get("tte") is not None else ""))
    print(f"compilers: {st.get('compilers_mem', 0) >> 20} MB running (budget {st.get('compilers_budget', 0) >> 20} MB) | "
          f"Xcode jobs for the next build: {st.get('xcode_jobs', '?')}")
    spd, sched = thermal()
    print(f"CPU speed limit {spd}% | scheduler {sched}% | load {' '.join(map(str, loadavg()))}")
    for title, key in (("paused", "paused"), ("backgrounded", "bg"), ("cpu-throttled", "throttled")):
        d = st.get(key, {})
        print(f"{title}: {len(d)}")
        for k, i in d.items():
            print(f"  pid {k:>6} {i['name']:<28} {int((time.time() - i['t']) / 60)} min  ({i['why']})")
    print(f"config: {CONF}\nlog:    {LOG_FILE}")


def pid_action(cmd, pid):
    st, conf = load_state(), load_conf()
    procs = proc_list()
    p = procs.get(pid)
    if p is None and cmd != "resume-pid":
        print(f"pid {pid} not found (or not yours)")
        return 1
    if p and cmd in ("pause-pid", "quit-pid", "bg-pid", "throttle-pid") and matches(p["name"], protected_set(conf)):
        print(f"{p['name']} is protected")
        return 1
    if cmd == "pause-pid":
        pause(p, st, False, "manual")
    elif cmd == "resume-pid":
        resume(pid, st, "manual") or os.kill(pid, signal.SIGCONT)
    elif cmd == "bg-pid":
        background(p, st, False, "manual")
    elif cmd == "fg-pid":
        unbackground(pid, st, "manual")
    elif cmd == "throttle-pid":
        start_throttle(p, st, float(load_conf().get("cpu", "duty", fallback="0.5")), "manual")
    elif cmd == "unthrottle-pid":
        stop_throttle(pid, st, "manual")
    elif cmd == "quit-pid":  # polite quit only: SIGCONT first (in case paused), then SIGTERM
        resume(pid, st, "quit")
        os.kill(pid, signal.SIGCONT)
        os.kill(pid, signal.SIGTERM)
        log(f"QUIT (SIGTERM) pid={pid} {p['name']} (manual)")
    save_state(st)
    return 0


def main(argv):
    global _conf
    _conf = load_conf()
    cmd = argv[1] if len(argv) > 1 else "status"
    if cmd == "tick":
        force = argv[argv.index("--force-level") + 1] if "--force-level" in argv else None
        tick(dry="--dry-run" in argv, force=force)
    elif cmd == "status":
        status()
    elif cmd == "json":
        print(json.dumps(load_state()))
    elif cmd == "junk":
        junk()
    elif cmd == "maintain":
        return maintain(argv[2:])
    elif cmd == "boost":
        q = argv[argv.index("--quit") + 1] if "--quit" in argv else None
        return boost([x.strip() for x in q.split(",") if x.strip()] if q else None, as_json="--json" in argv)
    elif cmd == "optimize":
        return maintain(["optimize"] + argv[2:])
    elif cmd == "bench" and len(argv) > 3 and argv[2] == "run":
        # memguard bench run <label> [--guard-swap 3G] [--guard-avail 500M] -- <shell command>
        a = argv[3:]
        cmd_ = " ".join(a[a.index("--") + 1:]) if "--" in a else ""
        opt = lambda k: size(a[a.index(k) + 1]) if k in a else None
        r = bench_run(a[0], cmd_, opt("--guard-swap"), opt("--guard-avail"))
        return 0 if r["rc"] == 0 else 1
    elif cmd == "bench":
        return bench(int(argv[2]) if len(argv) > 2 else 60)
    elif cmd == "throttle-loop":
        throttle_loop(int(argv[2]), float(argv[3]))
    elif cmd in ("pause-pid", "resume-pid", "bg-pid", "fg-pid", "quit-pid", "throttle-pid", "unthrottle-pid") and len(argv) > 2:
        return pid_action(cmd, int(argv[2]))
    elif cmd == "mode":
        st = load_state()
        st.pop("mode_override", None)
        save_state(st)
        print("memguard has one mode, Max: it adapts by itself (a DAW or a build running only changes priorities)")
    elif cmd == "pause" and len(argv) > 2:
        st, name = load_state(), " ".join(argv[2:])
        hits = [p for p in proc_list().values() if matches(p["name"], [name])
                and p["pid"] not in (os.getpid(), os.getppid())]
        if not hits:
            print(f"no process matching '{name}'")
        for p in hits:
            if matches(p["name"], protected_set(_conf)):
                print(f"skip protected {p['name']} ({p['pid']})")
            else:
                pause(p, st, False, "manual")
                print(f"paused {p['name']} ({p['pid']})")
        save_state(st)
    elif cmd == "resume" and len(argv) > 2:
        st = load_state()
        if argv[2] == "all":
            resume_all(st, "manual resume all")
        else:
            for k, i in list(st.get("paused", {}).items()):
                if matches(i["name"], [" ".join(argv[2:])]):
                    resume(int(k), st, "manual")
        save_state(st)
        print("resumed")
    elif cmd == "off":
        os.makedirs(STATE_DIR, exist_ok=True)
        open(OFF_FILE, "w").close()
        st = load_state()
        resume_all(st, "memguard turned off")
        for k in list(st.get("bg", {})):
            unbackground(int(k), st, "memguard turned off")
        for k in list(st.get("throttled", {})):
            stop_throttle(int(k), st, "memguard turned off")
        # the Xcode job cap is memguard's: off = Xcode's own default (one compile per CPU thread)
        subprocess.run(["defaults", "delete", "com.apple.dt.Xcode", "IDEBuildOperationMaxNumberOfConcurrentCompileTasks"],
                       capture_output=True)
        st.pop("xcode_jobs", None)
        save_state(st)
        log("memguard OFF")
        print("memguard off (all paused processes resumed, background QoS removed)")
    elif cmd == "on":
        if os.path.exists(OFF_FILE):
            os.remove(OFF_FILE)
        log("memguard ON")
        print("memguard on")
    elif cmd == "log":
        n = argv[2] if len(argv) > 2 else "40"
        os.execvp("tail", ["tail", "-n", n, LOG_FILE])
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
