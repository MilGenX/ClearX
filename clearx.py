#!/usr/bin/env python3
"""ClearX - keeps a 16 GB Intel Mac fast and out of swap death.

Lowers priority (renice + taskpolicy background QoS), pauses (SIGSTOP) memory hogs,
caps parallel compiler memory, predicts exhaustion from the trend, and resumes
(SIGCONT) when pressure drops. Never sends SIGKILL. Stdlib only.

Usage:
  clearx status | log [N] | junk | json      (status shows the 20 GB RAM+VRAM pool)
  clearx tick [--dry-run] [--force-level L]
  clearx pause <name> | resume all | resume <name>
  clearx pause-pid|resume-pid|bg-pid|fg-pid|quit-pid|throttle-pid|unthrottle-pid <pid>
  clearx bench [seconds]   (CSV snapshot: speed limit, load, swap rate, VRAM, GPU%)
  clearx bench run <label> [--guard-swap 3G] [--guard-avail 500M] [--max-seconds N] -- <command>   (workload benchmark)
  clearx off | on
  clearx maintain [all|scan|clean <cat,...>|large|startup|disable <label>|enable <label>
                     |uninstall <App> [--vendor com.x] [--apply]|updates|security|health|storage|optimize]
  clearx run "<label>" [--slots N] <command...>   heavy-job queue: one (or N) at a time, low priority
  clearx wake [all|<app>]   unfreeze apps frozen by the App Freezer
  clearx boost [--json] [--quit "App, App"]   Speed Up: real actions only, measured before/after
  clearx optimize [--apply]   measure every limit of this Mac; --apply sets the safe user-level items
"""
import configparser, fnmatch, json, os, re, signal, subprocess, sys, time, types

HOME = os.path.dirname(os.path.realpath(__file__))
CONF = os.environ.get("CLEARX_CONF", os.path.join(HOME, "clearx.conf"))
STATE_DIR = os.environ.get("CLEARX_STATE", os.path.join(HOME, "state"))
LOG_DIR = os.environ.get("CLEARX_LOGS", os.path.join(HOME, "logs"))
STATE_FILE = os.path.join(STATE_DIR, "state.json")
OFF_FILE = os.path.join(STATE_DIR, "disabled")
LOG_FILE = os.path.join(LOG_DIR, "clearx.log")
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
    """Merge into the file under a lock: the daemon, the CLI and the app's calls write it concurrently, and a
    long `maintain` run must not drop keys (junk, storage, ssd...) that others wrote meanwhile."""
    import fcntl
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(MAINT_FILE + ".lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        cur = load_maint()
        cur.update(d)
        tmp = f"{MAINT_FILE}.{os.getpid()}.tmp"
        with open(tmp, "w") as f:
            json.dump(cur, f, indent=1)
        os.replace(tmp, MAINT_FILE)


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


def state_lock():
    """One writer of state.json at a time. The tick (every 10 s) and the app's calls (wake, boost, pause...) each load,
    change and save it; without this one save drops the other's changes, e.g. a pause record (the process would
    stay stopped with nothing left to resume it). Held until the process exits (or os.close)."""
    import fcntl
    os.makedirs(STATE_DIR, exist_ok=True)
    fd = os.open(STATE_FILE + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


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
    if sys.stdout.isatty() or os.environ.get("CLEARX_ECHO"):
        print(line)


def notify(title, msg):
    if _conf and not _conf.getboolean("general", "notify", fallback=True):
        return
    if subprocess.run(["pgrep", "-x", "ClearX"], capture_output=True).returncode == 0:
        # ClearX.app posts it (its own name and icon; osascript notifications show Script Editor's)
        with open(os.path.join(STATE_DIR, "notify.json"), "w") as f:
            json.dump(dict(title=title, msg=msg, t=time.time()), f)
        return
    esc = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')
    subprocess.run(["osascript", "-e",
                    f'display notification "{esc(msg)}" with title "ClearX" subtitle "{esc(title)}"'],
                   capture_output=True, timeout=10)


def sh(*args):
    # errors="replace": some tools print non-UTF-8 bytes (pmset lists Bluetooth device names verbatim)
    return subprocess.run(args, capture_output=True, timeout=30).stdout.decode("utf-8", errors="replace")


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
    # [protected] apps: the app and everything it starts (MyApp: its node server, voice ID, tunnel)
    apps = globs(conf.get("protected", "apps", fallback=""))
    ptree = tree(procs, [p["pid"] for p in procs.values() if matches(p["name"], apps)]) if apps else set()
    # build tests (MyDAW's audio timing tests): a paused test fails, and a failed test keeps the update out
    ptree |= tree(procs, [p["pid"] for p in procs.values()
                          if matches(p["name"], globs(conf.get("interactive", "tests", fallback="ctest, *_tests")))])
    rx = conf.get("test", "target_regex", fallback="").strip()
    me = {os.getpid(), os.getppid()}
    paused = {int(k) for k in st.get("paused", {})}
    out = []
    for p in procs.values():
        if p["pid"] in me or "Z" in p["stat"] or matches(p["name"], prot) or p["pid"] in ptree:
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
    if p["name"].startswith("com.apple.WebKit.") and why != "manual":
        return False  # XPC helpers of MyApp/Safari: freezing them blanks pages (renice/QoS still fine)
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


# ---------- build queue: one project builds/installs at a time ----------
def queue_skipped(procs, conf):
    """Builds that never wait in line ([queue] skip: X 2026-10-28, "and the queue": MyDAW's updates go in right away):
    each matching script and everything under it, shells included (its cmake, xcodebuild, ctest)."""
    Q = conf["queue"] if conf.has_section("queue") else {}
    skip = [x.strip() for x in Q.get("skip", "").split(",") if x.strip()]
    if not skip:
        return set()
    kids = {}
    for p in procs.values():
        kids.setdefault(p["ppid"], []).append(p["pid"])
    todo = [p["pid"] for p in procs.values() if (p["name"] in SHELLS or p["name"].lower().startswith("python"))
            and any(x in cmdline(p["pid"]) for x in skip)]
    out = set()
    while todo:
        pid = todo.pop()
        if pid not in out:
            out.add(pid); todo += kids.get(pid, [])
    return out


def build_roots(procs, conf):
    """Top-level build jobs, one per project run: MyDAW's auto-update script, `myapp.py build|install`, and any
    xcodebuild / swift build / cmake / cargo / ninja / make not started by another of these. `zsh -c` and `python -c`
    only carry text: one that merely mentions a script isn't running it, and a real run shows up as its own process."""
    Q = conf["queue"] if conf.has_section("queue") else {}
    tools = globs(Q.get("tools", "xcodebuild, swift-build, cmake, cargo, ninja, make"))
    scripts = [x.strip() for x in Q.get("scripts", "mydaw-auto-update, myapp.py build, myapp.py install").split(",") if x.strip()]
    cand = set()
    for p in procs.values():
        n = p["name"]
        if matches(n, tools):
            cand.add(p["pid"])
        elif n in SHELLS or n.lower().startswith("python"):
            c = cmdline(p["pid"])
            if " -c " not in c and any(x in c for x in scripts):
                cand.add(p["pid"])
    cand -= queue_skipped(procs, conf)
    roots = []
    for pid in cand:  # keep only the outermost: a root whose ancestor is also a candidate belongs to that one
        a, top = procs[pid]["ppid"], True
        while a in procs and a > 1:
            if a in cand:
                top = False; break
            a = procs[a]["ppid"]
        if top:
            roots.append(pid)
    return roots


def queue_tick(conf, st, procs, fg, dry, strained=True):
    Q = conf["queue"] if conf.has_section("queue") else {}
    if str(Q.get("enabled", "true")).lower() != "true":
        for k, i in list(st.get("paused", {}).items()):
            if i.get("kind") == "queue":
                resume(int(k), st, "build queue off", dry=dry)
        return
    roots = build_roots(procs, conf)
    for pid in sorted(queue_skipped(procs, conf)):  # (one held in line before it was let skip it)
        if st.get("paused", {}).get(str(pid), {}).get("kind") == "queue":
            resume(pid, st, "build queue: skips the line", dry=dry)
    known = st.setdefault("queue_t", {})
    for r in roots:  # first-seen time = place in line (start time from ps, cached)
        if str(r) not in known:
            known[str(r)] = start_epoch(r) or time.time()
    for k in list(known):
        if int(k) not in roots:
            known.pop(k)
    order = sorted(roots, key=lambda r: known[str(r)])
    groups = {r: tree(procs, [r]) for r in order}
    tests = tree(procs, [p["pid"] for p in procs.values() if matches(p["name"], globs(conf.get("interactive", "tests", fallback="ctest, *_tests")))])
    # the head of the line runs; so does anything you run in the app in front (Xcode, Terminal you're watching)
    active = [r for r in order if groups[r] & fg] or []
    active = set(active + order[:1])
    if not strained:  # memory to spare: builds run side by side, as they would without ClearX
        active = set(order)
    # MyDAW's updater testing (its ctest, under a build that skips the line): its timing tests miss beside another
    # build (X 2026-10-28: MyApp's tests next to MyDAW's player test failed it twice), so every other build waits meanwhile.
    skipped = queue_skipped(procs, conf)
    if any(procs[p]["name"] == "ctest" for p in skipped if p in procs):
        active = set()
    # nothing waits in line forever: whatever has waited max_wait_min runs alongside the rest
    active |= {r for r in order if time.time() - known[str(r)] >= float(Q.get("max_wait_min", 180)) * 60}
    st["build_queue"] = [dict(pid=r, name=procs[r]["name"], what=cmdline(r)[:120], running=r in active) for r in order]
    held_by_q = {int(k) for k, i in st.get("paused", {}).items() if i.get("kind") == "queue"}
    run_pids = set().union(*[groups[r] for r in active]) if active else set()
    for pid in sorted(held_by_q & run_pids, reverse=True):  # its turn: children first, then the parent
        resume(pid, st, "build queue: its turn", dry=dry)
    for pid in sorted(held_by_q - set(procs)):
        st["paused"].pop(str(pid), None)
    # a project's own "stuck run" timer must not count time spent waiting in line (MyDAW kills runs > 30 min)
    if any(r not in active for r in order):
        for f in [x.strip() for x in Q.get("keep_fresh", "").split(",") if x.strip()]:
            try:
                os.utime(f)
            except OSError:
                pass
    for r in order:
        if r in active:
            continue
        for pid in sorted(groups[r]):  # parents first so nothing new starts
            p = procs[pid]
            if pid in fg or pid in tests or "T" in p["stat"] or "Z" in p["stat"] or str(pid) in st.get("paused", {}):
                continue
            if " --test" in cmdline(pid):  # (a test run already going finishes: MyApp's node --test)
                continue
            pause(p, st, dry, f"build queue: waits for {procs[order[0]]['name']} (pid {order[0]})", kind="queue")


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


def swap_out_rate(hist, now, win):
    """MB/s swapped out, averaged over the last win seconds; time with no history counts as no swapping, so one
    busy tick right after a restart or wake-up doesn't read as a sustained rate."""
    r = [h for h in hist if now - h[0] <= win + 1]
    return sum(b[3] * (b[0] - a[0]) for a, b in zip(r, r[1:])) / win


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
    # Levels come from what is happening now: macOS's pressure level, the swap-out rate over the last minute and the
    # memory left. Allocated swap says nothing about now (macOS keeps it long after pressure ends), and neither does
    # "free" (macOS keeps it low on purpose); together they had the pool red ~1000 times a day at normal pressure.
    swap_rate = swap_out_rate(st.get("hist", []), now, float(L.get("swap_window_s", 60)))
    why = []
    if m["pressure"] >= 4 or (swap_rate >= float(L.get("red_swap_rate", 50)) and m["avail"] < size(L.get("red_avail", "2G"))):
        level = "red"; why.append("critical pressure" if m["pressure"] >= 4 else f"swapping {swap_rate:.0f}MB/s, available {m['avail'] >> 20}M")
    elif (m["pressure"] >= 2 or swap_rate >= float(L.get("orange_swap_rate", 25))) and m["avail"] < size(L.get("orange_avail", "4G")):
        level = "orange"; why.append(f"{'pressure warn' if m['pressure'] >= 2 else f'swapping {swap_rate:.0f}MB/s'}, available {m['avail'] >> 20}M")
    elif m["pressure"] >= 2 or swap_rate >= float(L.get("yellow_swap_rate", 8)):
        level = "yellow"; why.append("pressure warn" if m["pressure"] >= 2 else f"swapping {swap_rate:.0f}MB/s")
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
        # cmake --build (incl. MyDAW's auto-update) and make started from launchd/Finder/Terminal get the same cap
        subprocess.run(["launchctl", "setenv", "CMAKE_BUILD_PARALLEL_LEVEL", str(n)], capture_output=True)
        subprocess.run(["launchctl", "setenv", "MAKEFLAGS", f"-j{n}"], capture_output=True)
        st["xcode_jobs"] = n
    log(f"XCODE jobs -> {n} ({why}){' [dry-run]' if dry else ''}")


AUTO_BG = ("pool", "level", "budget", "spill", "thermal", "mode build", "interactive")
ACTIVATIONS = os.path.join(STATE_DIR, "activations.json")  # written by ClearX.app on every app switch


def hid_idle_s():
    """Seconds since the last keyboard/mouse/trackpad input."""
    mt = re.search(r'"HIDIdleTime" = (\d+)', sh("ioreg", "-c", "IOHIDSystem", "-d", "4"))
    return int(mt.group(1)) / 1e9 if mt else 0


def assertion_pids():
    """pids holding any power assertion (audio playback, downloads, calls, timers): never frozen."""
    return {int(x) for x in re.findall(r"pid (\d+)\(", sh("pmset", "-g", "assertions"))}


def net_active(st, now, min_bytes=1 << 20, window=30):
    """pids that moved >= min_bytes over the network in the last ~window s (nettop cumulative counters)."""
    cur = {}
    for ln in sh("nettop", "-P", "-L", "1", "-n", "-J", "bytes_in,bytes_out").splitlines()[1:]:
        f = ln.split(",")
        mt = re.match(r".*\.(\d+)$", f[0]) if len(f) >= 3 else None
        if mt and f[1].isdigit() and f[2].isdigit():
            cur[mt.group(1)] = int(f[1]) + int(f[2])
    prev = st.get("net", {})
    act = {int(k) for k, v in cur.items() if k in prev and v - prev[k][1] >= min_bytes and now - prev[k][0] <= window * 3}
    st["net"] = {k: (prev[k][0], prev[k][1]) if k in prev and now - prev[k][0] < window else (now, v) for k, v in cur.items()}
    return act


def freeze_tick(conf, st, procs, lvl, fpid, daws, compilers, now, dry, reserve_stage=0):
    """App Freezer: hidden, idle, big apps are stopped (SIGSTOP) while the pool is yellow+; ClearX.app wakes
    them (SIGCONT) the instant they're activated. Everything is woken when the pool is green, the freezer is off,
    or ClearX.app isn't running (it's the one that wakes them)."""
    F = conf["freezer"] if conf.has_section("freezer") else {}
    frozen = {k: i for k, i in st.get("paused", {}).items() if i.get("kind") == "freeze"}
    for k, i in list(frozen.items()):  # woken by the app (or by hand): forget
        p = procs.get(int(k))
        if p is None or "T" not in p["stat"]:
            st["paused"].pop(k, None); frozen.pop(k)
            if p is not None and i.get("root"):
                log(f"WAKE pid={k} {i['name']} ({i.get('app')}) after {int(now - i['t'])}s")
    try:
        with open(ACTIVATIONS) as f:
            for name, t in json.load(f).items():
                if t > st.setdefault("front_seen", {}).get(name, 0):
                    st["front_seen"][name] = t
    except (OSError, ValueError):
        pass
    on = str(F.get("enabled", "false")).lower() == "true" or reserve_stage > 0
    app_alive = any(p["name"] in ("ClearX",) for p in procs.values())
    healthy = lvl == 0 and reserve_stage < 2
    if frozen and (not on or healthy or not app_alive):
        why = "freezer off" if not on else "pool healthy and reserve met" if healthy else "ClearX.app not running"
        for k in list(frozen):
            resume(int(k), st, why, dry=dry)
        log(f"UNFREEZE all ({why})")
        return 0
    if not on or healthy or not app_alive:
        return 0
    idle_s = float(F.get("idle_min", 5)) * 60 if reserve_stage < 4 else float(F.get("recent_min", 1)) * 60
    min_mem = size(F.get("min_mem", "300M"))
    # [protected] apps (MyApp: its server, voice ID and phone tunnel must keep answering) are never frozen either
    never = globs(F.get("never", "")) + protected_set(conf) + daws + compilers + globs(conf.get("protected", "apps", fallback=""))
    renderer_only = globs(F.get("renderer_only", "Brave Browser, Google Chrome"))
    held = assertion_pids()
    busy_net = net_active(st, now)
    since = st.get("front_track_t", now)
    frozen_roots = {i.get("root") for i in frozen.values()} | {i.get("app_pid") for i in frozen.values()}
    # prefer compression: freezing helps while the compressor can absorb what goes cold; when it's nearly full,
    # whatever gets frozen goes straight to swap (SSD writes), so stop adding to it
    m_ = metrics()
    if m_["compressed"] >= size(F.get("compressor_max", "4G")):
        return 0
    n = 0
    for a in gui_apps():
        if a["pid"] == fpid or a["pid"] not in procs or a["pid"] in frozen_roots or matches(a["name"], never):
            continue
        if now - st.get("front_seen", {}).get(a["name"], since) < idle_s:
            continue
        if st.get("cooldown", {}).get(str(a["pid"]), 0) > now:
            continue  # just resumed by the max-freeze safety net: not straight back in
        # a protected helper inside an app (a local AI server, a tunnel) is never frozen with it
        pids = {x for x in tree(procs, [a["pid"]]) if x == a["pid"] or not matches(procs[x]["name"], never)}
        if pids & (held | busy_net):
            continue  # playing audio, holding a wake assertion, or downloading/uploading
        mem = sum(procs[x]["mem"] for x in pids)
        if mem < min_mem:
            continue
        if matches(a["name"], renderer_only):  # browsers: only their tab renderers, never the main/GPU process
            pids = {x for x in pids if "(Renderer)" in procs[x]["name"]}
        new = 0
        for x in sorted(pids):
            p = procs[x]
            if "T" in p["stat"]:
                continue
            if pause(p, st, dry, f"frozen: {a['name']} hidden & idle {int((now - st.get('front_seen', {}).get(a['name'], since)) / 60)} min",
                     kind="freeze"):
                new += 1
                if not dry:
                    st["paused"][str(x)].update(app=a["name"], root=a["pid"] if x == a["pid"] else None, app_pid=a["pid"])
        if new:  # (all already stopped: nothing new, no log line every tick)
            n += 1
            log(f"FREEZE {a['name']} ({new} processes, {mem >> 20} MB)")
    return n


# ---------- the tick ----------
def _read_pool(c):
    """Reads the machine and works out this tick's pool level."""
    conf, st, now, force = c.conf, c.st, c.now, c.force
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
    c.m = m; c.g = g; c.swapping_mb = swapping_mb; c.pool = pool; c.level = level; c.lvl = lvl; c.L = L; c.P = P


def _read_procs(c):
    """Process list, mode, and who's in front (never touched)."""
    conf, st, now, dry, pool, lvl, P = c.conf, c.st, c.now, c.dry, c.pool, c.lvl, c.P
    procs = proc_list()
    compilers = globs(conf.get("build_guard", "compilers", fallback=""))
    build_tools = globs(conf.get("modes", "build_tools", fallback=""))
    # top's footprint is ~70% of a tick's CPU. Footprint-based choices (pause, budgets) only happen when the pool is
    # yellow+, swapping or tight; held processes and a build on a green pool don't need it (compiler RSS is its
    # footprint), so then refresh it at most every footprint_s instead of every tick.
    near = lvl >= 1 or pool["swap_active"] or st.get("throttled") or pool["tight"]
    calm_near = st.get("paused") or any(matches(p["name"], compilers) for p in procs.values())
    if near or (calm_near and now - st.get("footprint_t", 0) >= float(P.get("footprint_s", 60))):
        st["footprint_t"] = now
        add_footprint(procs)
        st["footprint"] = {str(p["pid"]): p["mem"] for p in procs.values() if p["mem"] > p["rss"] + (32 << 20)}
    else:  # between refreshes: the last footprint where it exceeded RSS (compressed/swapped memory)
        fp = st.get("footprint", {})
        for p in procs.values():
            p["mem"] = max(p["mem"], fp.get(str(p["pid"]), 0))
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
    c.procs = procs; c.compilers = compilers; c.mode = mode; c.fpid = fpid; c.fg = fg; c.is_build = is_build


def _note_level(c):
    """Logs level changes and the periodic heartbeat."""
    conf, st, now, m, g, swapping_mb, pool, level, lvl, mode = c.conf, c.st, c.now, c.m, c.g, c.swapping_mb, c.pool, c.level, c.lvl, c.mode
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


def _drop_exited(c):
    """Forgets processes that exited; WebKit helpers are never left paused."""
    st, now, procs = c.st, c.now, c.procs
    for k in list(st.get("paused", {})):
        if int(k) not in procs:
            st["paused"].pop(k)
            log(f"forget pid={k} (exited)")
    for k, info in list(st.get("paused", {}).items()):  # (freeze_tick handles the frozen ones)
        if info.get("kind") != "freeze" and "T" not in procs[int(k)]["stat"]:
            st["paused"].pop(k)  # woken from outside: a stale record would keep it from ever being paused again
            log(f"forget pid={k} {info['name']} (woken from outside)")
    for k, info in list(st.get("paused", {}).items()):
        if info["name"].startswith("com.apple.WebKit.") and info.get("why") != "manual":
            resume(int(k), st, "WebKit helpers are never paused")
    for k in list(st.get("bg", {})):
        if int(k) not in procs:
            st["bg"].pop(k)
    st["cooldown"] = {k: v for k, v in st.get("cooldown", {}).items() if v > now}


def _release_overdue(c):
    """Time limits on every kind of pause."""
    conf, st, now, dry = c.conf, c.st, c.now, c.dry
    # max-pause safety net: nothing stays frozen forever
    max_s = conf.getfloat("resume", "max_pause_min", fallback=15) * 60
    bmax_s = conf.getfloat("build_guard", "max_pause_min", fallback=60) * 60
    cool_s = conf.getfloat("resume", "repause_cooldown_min", fallback=10) * 60
    for k, info in list(st.get("paused", {}).items()):
        if info.get("kind") == "queue":  # safety net: nothing waits in line forever
            if now - info["t"] >= conf.getfloat("queue", "max_wait_min", fallback=180) * 60:
                resume(int(k), st, "build queue: waited too long", dry=dry)
            continue
        if info.get("kind") == "hold":
            hmax = conf.getfloat("interactive", "hold_max_min", fallback=120) * 60
            if now - info["t"] >= hmax:  # active for hours: let builders catch up for a while
                st["hold_off_until"] = now + conf.getfloat("interactive", "hold_break_min", fallback=20) * 60
                for k2, i2 in list(st.get("paused", {}).items()):
                    if i2.get("kind") == "hold":
                        resume(int(k2), st, f"held {hmax / 60:g} min: builders get a turn", dry=dry)
            continue
        limit = bmax_s if info.get("kind") == "build" else \
            conf.getfloat("freezer", "max_min", fallback=60) * 60 if info.get("kind") == "freeze" else max_s
        if now - info["t"] >= limit:
            resume(int(k), st, f"max pause {limit / 60:g} min reached", cooldown_s=cool_s, dry=dry)
            if info.get("kind") == "freeze":  # the app's own pid gets the cooldown the freezer checks
                st.setdefault("cooldown", {})[str(info.get("app_pid", k))] = now + cool_s
            else:  # (frozen apps: one notification per renderer would be a pile of them)
                notify("Resumed after timeout", f"{info['name']} (pid {k}) was paused {int((now - info['t']) / 60)} min")


def _reserve_and_freeze(c):
    """Works out the reserve stage, then runs the App Freezer."""
    conf, st, now, dry, m, procs, compilers, fg, is_build, lvl, fpid = c.conf, c.st, c.now, c.dry, c.m, c.procs, c.compilers, c.fg, c.is_build, c.lvl, c.fpid
    hist = st["hist"]
    # RAM reserve: keep `target` GB ready to use (free + file cache). Below it, one more step per tick:
    # 1 freeze hidden browser tabs' renderers + idle hidden apps (oldest/largest), 2 more of those even when the
    # pool is green, 3 builds to 2 jobs at background QoS, 4 also freeze hidden apps idle >= recent_min.
    RS = conf["reserve"] if conf.has_section("reserve") else {}
    stage = 0
    if str(RS.get("enabled", "false")).lower() == "true":
        target = size(RS.get("target", "3G"))
        ready = m["free"] + m["cached"]
        need = sum(procs[x]["mem"] for x in fg) + sum(p["mem"] for p in procs.values()
                                                       if matches(p["name"], compilers) and p["pid"] not in fg)
        ceiling = int(sh("sysctl", "-n", "hw.memsize").strip()) - m["wired"] - m["compressed"] - need
        # which build holds the reserve down (the menu says so calmly instead of a red cross)
        builds = [p for p in procs.values() if is_build(p)]
        build = "" if not builds else "Building MyDAW" if any("/MyDAW/" in p["path"] or "/MyDAW/" in cmdline(p["pid"])
                                                           for p in builds[:6]) \
            else "Building MyApp" if any("/MYAPP/" in p["path"] or "/MYAPP/" in cmdline(p["pid"]) for p in builds[:6]) \
            else "A build is running"
        st["reserve"] = dict(target=target, ready=ready, need=need, ceiling=ceiling, ok=ready >= target,
                             reachable=ceiling >= target, build=build)
        stage = st.get("reserve_stage", 0)
        if ready < target:
            # unreachable (what you're using + the build need more): freeze idle hidden apps only, don't throttle the
            # build to chase a number it can't reach; the menu says so
            stage = min(stage + 1, 4 if ceiling >= target else 2)
        elif ready >= target + size(RS.get("hysteresis", "1G")):
            stage = max(stage - 1, 0)
        if stage != st.get("reserve_stage", 0):
            log(f"RESERVE stage {st.get('reserve_stage', 0)} -> {stage}: ready {ready >> 20}M, target {target >> 20}M, "
                f"foreground+builds need {need >> 20}M" + ("" if ceiling >= target else " (reserve can't be held without pausing the build)"))
        st["reserve_stage"] = stage
        hist[-1] += [ready]
    else:
        st.pop("reserve", None); st["reserve_stage"] = 0
    st["frozen_new"] = freeze_tick(conf, st, procs, lvl, fpid, globs(conf.get("modes", "studio_apps", fallback="")),
                                   compilers, now, dry, stage)
    c.stage = stage


def _classify(c):
    """Sorts processes into who may be lowered and who may be paused."""
    conf, st, procs, fg, is_build = c.conf, c.st, c.procs, c.fg, c.is_build
    elig = [p for p in eligible(procs, conf, st) if p["pid"] not in fg]
    bgc = [p for p in elig if not is_build(p)]          # the background class
    # apps you can see (and their helpers: tab renderers, WebKit/GPU processes) are never paused for pressure: a
    # paused helper is a frozen or blank window, and only the App Freezer knows to wake it when you switch to it
    gui = tree(procs, [a["pid"] for a in gui_apps() if a["pid"] in procs])
    # macOS's own services (/System, /usr) are lowered in priority but never paused: apps wait on them, so a paused
    # system service turns into a beachball in whatever app asked it something
    pausable = [p for p in bgc if p["pid"] not in gui and not p["path"].startswith(("/System/", "/usr/", "/Library/Apple/"))]
    gpu_heavy = globs(conf.get("vram", "gpu_heavy", fallback=""))
    acts = []
    c.elig = elig; c.bgc = bgc; c.gui = gui; c.pausable = pausable; c.gpu_heavy = gpu_heavy; c.acts = acts


def _gpu_spill(c):
    """GPU memory spilling into RAM under pressure."""
    st, now, dry, pool, level, lvl, P, bgc, gpu_heavy, acts = c.st, c.now, c.dry, c.pool, c.level, c.lvl, c.P, c.bgc, c.gpu_heavy, c.acts
    # 1. GPU spilling into RAM while RAM is under pressure: background the GPU-heavy background apps
    #    first; compilers get a grace period before the build guard tightens
    if pool["spilling"] and lvl >= 1:
        for p in bgc:
            if matches(p["name"], gpu_heavy) and background(p, st, dry, f"spill {pool['spill_rate']}MB/s at {level}"):
                acts.append("spill")
        st["spill_t"] = now
    spill_grace = now - st.get("spill_t", 0) < float(P.get("spill_grace_s", 20))
    c.spill_grace = spill_grace


def _thermal(c):
    """macOS is lowering the CPU speed limit."""
    conf, st, now, dry, pool, P, bgc, gpu_heavy = c.conf, c.st, c.now, c.dry, c.pool, c.P, c.bgc, c.gpu_heavy
    # 2. thermal throttling: cut compiler parallelism first, then background the UI apps if it persists
    spd = pool["thermal"]
    if spd < 100:
        if st.get("thermal_ok", True):
            log(f"THERMAL throttling: CPU_Speed_Limit={spd} scheduler={pool['sched']}")
            st["thermal_since"] = now
        st["thermal_ok"] = False
        if now - st.get("thermal_since", now) >= float(P.get("thermal_ui_after_s", 60)):
            for p in bgc:
                if matches(p["name"], gpu_heavy + globs(mode_cfg(conf, "build", "background", ""))):
                    background(p, st, dry, f"thermal {spd}%")
    elif not st.get("thermal_ok", True):
        log(f"THERMAL ok again (speed limit {spd})")
        st["thermal_ok"] = True
    # brief dips are normal under load; notify only when throttling is sustained (every sample below 100 and
    # the average below notify_below for notify_after_s), at most once per notify_every_s
    win = float(P.get("thermal_notify_after_s", 120))
    sh_ = [x for x in st.get("spd_hist", []) if now - x[0] <= win + 15] + [[round(now, 1), spd]]
    st["spd_hist"] = sh_
    st["thermal_sustained"] = now - sh_[0][0] >= win and all(s < 100 for _, s in sh_) \
        and sum(s for _, s in sh_) / len(sh_) < float(P.get("thermal_notify_below", 90))
    if st["thermal_sustained"] and now - st.get("thermal_notified", 0) >= float(P.get("thermal_notify_every_s", 3600)):
        st["thermal_notified"] = now
        notify("Your Mac got warm", "ClearX eased off background work (fewer build jobs, background apps "
                                    "at low priority) until it cools down.")
    c.spd = spd
    # ClearX only slows something down when the Mac is short of memory or macOS is already lowering the CPU speed;
    # otherwise builds, AI servers and busy apps run exactly as they would without it
    c.strained = c.lvl >= 1 or c.stage >= 1 or spd < 100


def _heat(c):
    """Sustained CPU load, before macOS starts throttling."""
    conf, st, now, dry, pool, spd, bgc, elig = c.conf, c.st, c.now, c.dry, c.pool, c.spd, c.bgc, c.elig
    # 2b. heat: the i9 runs hot under sustained all-core load long before macOS lowers CPU_Speed_Limit, so act
    #     on sustained load: fewer compile jobs for the next build, background QoS for known background hogs.
    #     Between cool_pct and hot_pct neither timer runs (hysteresis); released after cool_s below cool_pct.
    Hc = conf["heat"] if conf.has_section("heat") else {}
    if str(Hc.get("enabled", "true")).lower() == "true":
        cpu = pool["cpu_load"]
        if cpu >= float(Hc.get("hot_pct", 70)):
            st.setdefault("heat_since", now); st.pop("cool_since", None)
        elif cpu < float(Hc.get("cool_pct", 45)):
            st.setdefault("cool_since", now); st.pop("heat_since", None)
        else:
            st.pop("heat_since", None); st.pop("cool_since", None)
        if spd < 100:  # macOS is already slowing the CPU: heat mode now, and no release until it stops
            st.setdefault("heat_since", now - float(Hc.get("sustain_s", 30))); st.pop("cool_since", None)
        if not st.get("heat_on") and now - st.get("heat_since", now) >= float(Hc.get("sustain_s", 30)):
            st["heat_on"] = True
            log(f"HEAT: CPU load {cpu}% sustained -> {Hc.get('jobs', 6)} compile jobs, background hogs")
        elif st.get("heat_on") and now - st.get("cool_since", now) >= float(Hc.get("cool_s", 120)):
            st["heat_on"] = False
            log(f"HEAT over: CPU load {cpu}%")
            for k, info in list(st.get("bg", {}).items()):
                if info.get("why", "").startswith("heat"):
                    unbackground(int(k), st, "cooled down", dry=dry)
        if st.get("heat_on"):
            for p in bgc:
                if matches(p["name"], globs(Hc.get("background", "photoanalysisd, mediaanalysisd, Google Drive*, cloudd"))):
                    background(p, st, dry, f"heat: CPU load {cpu}%")
            # automatic builders/test runners (e.g. a node --test run spawning one worker per core): their workers
            # live seconds, so lower the long-lived parents too; nice and background QoS carry over to new children
            bp = globs(conf.get("interactive", "build_paths", fallback="/auto-src/, /.worktrees/builder/"))
            for p in elig if spd < 100 else []:
                if any(x in p["path"] for x in bp) and "T" not in p["stat"]:
                    if p["nice"] < 10:
                        renice(p, dry, f"heat: auto-builder, CPU load {cpu}%")
                    background(p, st, dry, f"heat: auto-builder, CPU load {cpu}%")


def _interactive(c, want):
    """While you're using the Mac, builds and local AI step back. Returns the next build's job cap."""
    conf, st, now, dry, procs, compilers, fg, fpid, stage = c.conf, c.st, c.now, c.dry, c.procs, c.compilers, c.fg, c.fpid, c.stage
    I = conf["interactive"] if conf.has_section("interactive") else {}
    interactive = str(I.get("enabled", "false")).lower() == "true"
    if interactive:
        idle = hid_idle_s()
        st["hid_idle"] = round(idle)
        if idle < float(I.get("active_s", 60)) and c.strained:  # you're using a strained Mac: builds yield to you
            want = min(want, int(I.get("active_jobs", 4)))
            if stage >= 1:  # reserve short while you work: the next build step gets 1 job (never SIGSTOP: a
                want = 1    # stopped compiler keeps its RAM and it gets swapped out, i.e. SSD writes)
            bp = globs(I.get("build_paths", "/auto-src/, /.worktrees/builder/"))
            auto = [p["pid"] for p in procs.values() if "mydaw-auto-update" in p["path"] or any(x in p["path"] for x in bp) or
                    (p["name"] in ("zsh", "bash", "sh") and "mydaw-auto-update" in cmdline(p["pid"]))] \
                if any(p["name"] in ("zsh", "bash", "sh") for p in procs.values()) else []
            updater = tree(procs, auto)  # MyDAW's auto-update build, from the moment it starts
            # its tests are timing tests (audio): at background QoS they run 3x slower and fail, and a failed test
            # keeps the update out. Shells neither: what they start later (the tests) would inherit background QoS.
            tests = tree(procs, [p["pid"] for p in procs.values() if matches(p["name"], globs(I.get("tests", "ctest, *_tests")))])
            for p in procs.values():
                if p["pid"] in tests or p["name"] in SHELLS:
                    if str(p["pid"]) in st.get("bg", {}) and st["bg"][str(p["pid"])].get("why", "").startswith("interactive"):
                        unbackground(p["pid"], st, "tests run at full speed", dry=dry)
                    continue
                if (matches(p["name"], compilers) or p["pid"] in updater) and p["pid"] not in fg and "T" not in p["stat"]:
                    background(p, st, dry, "interactive: you're using the Mac" + (" (auto-builder)" if p["pid"] in updater else ""))
            # HOLD: automatic builders don't run at all while you use the Mac (backgrounding alone still let
            # hundreds of short-lived node/clang workers load all 16 threads). Whole tree SIGSTOPped, parents
            # first so nothing new spawns; tests already running finish (timing tests). Released once you're away.
            hold_front = fpid in procs and matches(procs[fpid]["name"], globs(conf.get("protected", "apps", fallback="")))
            if str(I.get("hold_auto", "true")).lower() == "true" and now >= st.get("hold_off_until", 0) and not hold_front:
                held = st.setdefault("paused", {})
                for pid in sorted(updater - tests):
                    p = procs[pid]
                    if pid in fg or "T" in p["stat"] or str(pid) in held or p["name"] in SHELLS and pid not in auto:
                        continue
                    pause(p, st, dry, "hold: auto-builder while you use the Mac", kind="hold")
            # local AI servers (MyApp's llama-server): never paused, but while you work in another app they run at
            # background QoS so the app in front stays snappy; full speed when their app is in front or you're away
            ai = [p for p in procs.values() if matches(p["name"], globs(I.get("ai_servers", "llama-server, ollama*")))]
            owner_front = fpid in procs and matches(procs[fpid]["name"], globs(conf.get("protected", "apps", fallback="")))
            for p in ai:
                if owner_front:
                    if st.get("bg", {}).get(str(p["pid"]), {}).get("why", "").startswith("interactive: AI"):
                        unbackground(p["pid"], st, "its app is in front", dry=dry)
                else:
                    background(p, st, dry, "interactive: AI server while you use another app")
        elif idle >= float(I.get("idle_s", 120)) or not c.strained:
            for k, info in sorted(st.get("paused", {}).items(), key=lambda kv: -int(kv[0])):  # children first
                if info.get("kind") == "hold":
                    resume(int(k), st, "you're away: auto-builder runs", dry=dry)
            for k, info in list(st.get("bg", {}).items()):  # (the reserve's own holds stay until the reserve is met)
                if info.get("why", "").startswith("interactive") and not (stage >= 3 and "RAM reserve" in info["why"]):
                    unbackground(int(k), st, "you're away: full speed", dry=dry)
    c.interactive = interactive
    return want


def _build_jobs(c):
    """How many compile jobs the next build gets; also runs the build queue."""
    conf, st, now, dry, procs, compilers, fg, level, lvl, P, spd, stage = c.conf, c.st, c.now, c.dry, c.procs, c.compilers, c.fg, c.level, c.lvl, c.P, c.spd, c.stage
    Hc = conf["heat"] if conf.has_section("heat") else {}
    # Xcode parallelism for the next build follows the pool: 8 healthy, 4 yellow, 2 orange+/throttling
    J = lambda k, d: int(P.get(k, d))
    want = J("jobs_pressure", 2) if (lvl >= 2 or spd < 100) else J("jobs_normal", 4) if lvl == 1 \
        else J("jobs_healthy", 8)
    if st.get("heat_on"):
        want = min(want, int(Hc.get("jobs", 6)))
    if stage >= 3:  # reserve: builds give way (2 jobs, background QoS)
        want = min(want, 2)
        for p in procs.values():
            if matches(p["name"], compilers) and p["pid"] not in fg and "T" not in p["stat"]:
                background(p, st, dry, "interactive: RAM reserve")
    queue_tick(conf, st, procs, fg, dry, c.strained)
    want = _interactive(c, want)
    cur = st.get("xcode_jobs")
    if cur is None or want <= cur:
        st["jobs_low_t"] = now
    elif now - st.get("jobs_low_t", 0) < 60:
        want = cur  # raise only once the higher value has been wanted for 60 s straight
    set_xcode_jobs(want, st, dry, f"pool {level}, speed {spd}%")


def _mode_priorities(c):
    """Studio and build modes lower the apps competing with them."""
    conf, st, dry, mode, lvl, bgc = c.conf, c.st, c.dry, c.mode, c.lvl, c.bgc
    # mode: studio always keeps the DAW's competition backgrounded; build mode only when the pool needs it
    if mode == "studio" or (mode == "build" and lvl >= 1):
        for p in bgc:
            if matches(p["name"], globs(mode_cfg(conf, mode, "background", ""))):
                background(p, st, dry, f"mode {mode}")


def _budgets(c):
    """Soft per-app memory budgets."""
    conf, st, now, dry, mode, pool, level, lvl, bgc, gui = c.conf, c.st, c.now, c.dry, c.mode, c.pool, c.level, c.lvl, c.bgc, c.gui
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
            elif lvl >= 2 and [p for p in group if p["pid"] not in gui]:
                top = max((p for p in group if p["pid"] not in gui), key=lambda p: p["mem"])
                if pause(top, st, dry, f"budget {pat} {total >> 20}M > {limit >> 20}M"):
                    bgc = [p for p in bgc if p["pid"] != top["pid"]]
        else:
            st.setdefault("over", {}).pop(key, None)
    c.bgc = bgc


def _pressure(c):
    """Lowers and pauses background processes according to the level."""
    conf, st, dry, procs, level, lvl, L, P, bgc, gui, pausable = c.conf, c.st, c.dry, c.procs, c.level, c.lvl, c.L, c.P, c.bgc, c.gui, c.pausable
    # 4. pressure on the background class (largest first), never the foreground app or the build
    if lvl >= 1:
        heavy = globs(conf.get("heavy", "names", fallback=""))
        for p in bgc:
            if matches(p["name"], heavy) or p["mem"] >= size(L["red_proc_min"]):
                renice(p, dry, f"level {level}")
                background(p, st, dry, f"level {level}")
    pausable = [p for p in pausable if str(p["pid"]) not in st.get("paused", {})]
    if lvl == 2 and pausable:
        top = max(pausable, key=lambda p: p["mem"])
        if top["mem"] >= size(P.get("pause_min", "200M")):
            pause(top, st, dry, "pool orange, largest background process")
    if lvl == 3:
        for p in sorted(pausable, key=lambda p: -p["mem"]):
            if p["mem"] >= size(L["red_proc_min"]):
                pause(p, st, dry, f"pool red, >= {L['red_proc_min']}")

    for k, info in list(st.get("paused", {}).items()):
        if info.get("kind") == "pressure" and (int(k) in gui or (procs.get(int(k)) or {}).get("path", "").startswith(
                ("/System/", "/usr/", "/Library/Apple/"))):
            resume(int(k), st, "app on screen or macOS service: never paused for pressure", dry=dry)


def _build_guard(c):
    """Caps total compiler memory and job count. The oldest job always runs."""
    conf, st, dry, compilers, mode, lvl, P, elig, spd, spill_grace, stage = c.conf, c.st, c.dry, c.compilers, c.mode, c.lvl, c.P, c.elig, c.spd, c.spill_grace, c.stage
    # compilers paused for pressure (by older versions, or by hand-off) belong to the build guard; ones the queue or
    # the hold keeps back aren't its to release (it resumed queued jobs as soon as there was room)
    for k, info in st.get("paused", {}).items():
        if info.get("kind") in (None, "pressure") and matches(info["name"], compilers) and info.get("why") != "manual":
            info["kind"] = "build"

    # 5. build guard: summed compiler memory capped; the cap tightens with the pool (not during the spill grace),
    #    and a job-count cap applies while the CPU is throttling. The oldest job always runs.
    if conf.getboolean("build_guard", "enabled", fallback=True):
        budget = size(mode_cfg(conf, mode, "compiler_budget", conf.get("build_guard", "budget", fallback="6G")))
        if not spill_grace:
            budget = int(budget * {0: 1, 1: 1, 2: 0.5, 3: 0.25}[lvl])
        max_jobs = int(P.get("thermal_jobs", 4)) if spd < 100 else 10 ** 6
        if stage >= 3:
            max_jobs = min(max_jobs, 2)
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


def _release_healthy(c):
    """Resumes and un-backgrounds once the pool has been steadily healthy."""
    conf, st, now, dry, force, m, swapping_mb, lvl, spd, interactive = c.conf, c.st, c.now, c.dry, c.force, c.m, c.swapping_mb, c.lvl, c.spd, c.interactive
    # 6. healthy pool: release everything (resume one per tick, smallest first; un-background after 60 s calm)
    R = conf["resume"]
    calm = m["pressure"] == 1 and swapping_mb < 1 and m["avail"] >= size(R["resume_free"])
    if force:
        calm = lvl == 0
    # resume only after a steady green (a paused process's memory gets swapped out; resuming it right before the
    # next orange swaps it back in: pause/resume flapping is pure SSD churn), then don't re-pause it for a while
    steady = now - st.get("level_since", now) >= conf.getfloat("resume", "steady_s", fallback=60)
    if lvl == 0 and calm and steady:
        pp = [(k, i) for k, i in st.get("paused", {}).items() if i.get("kind") not in ("build", "freeze", "hold", "queue")]
        if pp:
            k, info = min(pp, key=lambda kv: kv[1].get("mem", 0))
            resume(int(k), st, "pool healthy", dry=dry, cooldown_s=conf.getfloat("resume", "repause_cooldown_min", fallback=10) * 60)
        elif now - st.get("level_since", now) >= conf.getfloat("resume", "unbackground_after_s", fallback=60):
            for k, info in list(st.get("bg", {}).items()):
                w = info.get("why", "")
                # interactive ones are the interactive block's: it releases them once you're away (releasing them
                # here re-backgrounded them on the next tick: ~15 processes flipped every 10 s)
                if w.startswith(AUTO_BG) and not (w.startswith("thermal") and spd < 100) \
                        and not (interactive and w.startswith("interactive")):
                    unbackground(int(k), st, "pool healthy", dry=dry)
            st["over"] = {}


def _cpu_governor(c):
    """Sustained hot background processes: background QoS, then duty-cycling."""
    conf, st, now, dry, procs, fg, bgc = c.conf, c.st, c.now, c.dry, c.procs, c.fg, c.bgc
    # CPU governor: sustained hot background processes -> background QoS, then duty-cycle; release when calm
    C = conf["cpu"] if conf.has_section("cpu") else {}
    if C.get("enabled", "true").lower() == "true":
        limit, sustain = float(C.get("limit_pct", 300)), float(C.get("sustain_s", 60))
        duty = float(C.get("duty", 0.5))
        hot = st.setdefault("cpu_hot", {})
        watch = globs(C.get("watch", ""))
        for p in bgc:
            k = str(p["pid"])
            if p["cpu"] >= limit and c.strained:
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
            if pr is None or pr["cpu"] < release * info.get("duty", 1) or int(k) in fg or not c.strained:
                stop_throttle(int(k), st, "cpu calmed down" if pr and int(k) not in fg else
                              "now in the foreground" if pr else "exited")
                hot.pop(k, None)
                if pr and k in st.get("bg", {}) and st["bg"][k]["why"].startswith("cpu"):
                    unbackground(int(k), st, "cpu calmed down", dry=dry)
        for k, info in list(st.get("bg", {}).items()):
            pr = procs.get(int(k))
            if info["why"].startswith("cpu") and k not in st.get("throttled", {}) and k not in hot \
                    and pr and (pr["cpu"] < release or not c.strained):
                unbackground(int(k), st, "cpu calmed down", dry=dry)


def _finish(c):
    """Final bookkeeping for the app, the health score and state.json."""
    st, now, dry, fg, g, pool, spd, acts = c.st, c.now, c.dry, c.fg, c.g, c.pool, c.spd, c.acts
    # the app the user switched to is never left backgrounded by an automatic action
    for k, info in list(st.get("bg", {}).items()):
        if int(k) in fg and info.get("why", "") != "manual":
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


def tick(dry=False, force=None):
    conf = load_conf()
    st = load_state()
    dry = dry or conf.getboolean("general", "dry_run", fallback=False)
    if os.path.exists(OFF_FILE):
        return
    c = types.SimpleNamespace(conf=conf, st=st, dry=dry, force=force, now=time.time())
    for step in (_read_pool, _read_procs, _note_level, _drop_exited, _release_overdue, _reserve_and_freeze, _classify,
                 _gpu_spill, _thermal, _heat, _build_jobs, _mode_priorities, _budgets, _pressure, _build_guard,
                 _release_healthy, _cpu_governor, _finish):  # order matters: each step sees what the last one did
        step(c)


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


def bench_run(label, cmd, guard_swap=None, guard_avail=None, step=2, logfile=None, max_s=None):
    """Runs cmd (shell) as its own process group and samples the machine every `step` s until it exits.
    guard_*: abort (SIGINT, then SIGTERM; never SIGKILL) if swap grows by more than guard_swap over the start,
    or available memory (free + inactive + purgeable) drops below guard_avail; max_s: the same after that long.
    Each sample also times what you'd feel: starting a tiny program, waking from a short sleep and a small synced
    disk write (spawn_ms, wake_ms, fsync_ms). Writes CSV + JSON in bench/."""
    os.makedirs(os.path.join(HOME, "bench"), exist_ok=True)
    base = os.path.join(HOME, "bench", time.strftime("run-%Y%m%d-%H%M%S-") + label)
    m0 = metrics()
    w0 = ssd_counters()[0]
    r = dict(label=label, cmd=cmd, clearx="off" if os.path.exists(OFF_FILE) else "on", start=time.time(),
             swap_start=m0["swap"], peak_swap=m0["swap"], min_free=m0["free"], min_avail=m0["avail"],
             min_speed=100, max_pressure=m0["pressure"], peak_compressed=m0["compressed"], max_load=0,
             load_start=loadavg()[1], peak_paused=0,
             peak_vram=0, peak_spill=0, aborted=None, xcode_jobs=subprocess.run(
                 ["defaults", "read", "com.apple.dt.Xcode", "IDEBuildOperationMaxNumberOfConcurrentCompileTasks"],
                 capture_output=True, text=True).stdout.strip() or "default (= CPU count)")
    out = open(logfile or base + ".log", "w")
    proc = subprocess.Popen(cmd, shell=True, stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
    last, t_abort = m0["swapouts"], None
    feel = dict(spawn_ms=[], wake_ms=[], fsync_ms=[])
    with open(base + ".csv", "w") as f:
        f.write("t,swap_mb,swapout_mb_s,free_mb,avail_mb,compressed_mb,pressure,speed_limit,load1,vram_mb,spill_mb,paused,"
                "spawn_ms,wake_ms,fsync_ms\n")
        while proc.poll() is None:
            t0 = time.time()
            subprocess.run(["/usr/bin/true"])
            spawn = (time.time() - t0) * 1000
            t1 = time.time()
            time.sleep(0.05)
            wake = max(0, (time.time() - t1 - 0.05) * 1000)
            sync = disk_probe()[1] or 0
            feel["spawn_ms"].append(spawn); feel["wake_ms"].append(wake); feel["fsync_ms"].append(sync)
            paused = len(load_state().get("paused", {}))
            r["peak_paused"] = max(r["peak_paused"], paused)
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
                                       g["used"] >> 20, g["sys"] >> 20, paused, round(spawn, 1), round(wake, 1), round(sync, 1)])) + "\n")
            f.flush()
            over_time = max_s and t0 - r["start"] > max_s
            if not r["aborted"] and ((guard_swap and m["swap"] - m0["swap"] > guard_swap) or
                                     (guard_avail and m["avail"] < guard_avail) or over_time):
                r["aborted"] = ("time limit" if over_time else
                                f"swap +{(m['swap'] - m0['swap']) >> 20} MB" if guard_swap and m["swap"] - m0["swap"] > guard_swap
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
    for k, v in feel.items():
        v = sorted(v) or [0]
        r[k] = dict(median=round(v[len(v) // 2], 1), p95=round(v[int(len(v) * .95)], 1), max=round(v[-1], 1))
    with open(base + ".json", "w") as f:
        json.dump(r, f, indent=1)
    print(json.dumps(r, indent=1))
    return r


# ---------- heavy-job queue: big jobs line up instead of colliding ----------
def run_queued(label, cmd, slots=1, timeout_h=6):
    """clearx run "<label>" <command...>: waits until fewer than `slots` queued jobs are running and the pool is
    green or yellow, then runs the command at background QoS (taskpolicy -b) and returns its exit code.
    Queue state is a directory of lock files, so jobs from any Terminal or script share it."""
    import fcntl
    qdir = os.path.join(STATE_DIR, "queue")
    os.makedirs(qdir, exist_ok=True)
    t0, said = time.time(), False
    while True:
        for i in range(slots):
            fd = os.open(os.path.join(qdir, f"slot{i}.lock"), os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(fd); continue
            if load_state().get("level", "green") in ("green", "yellow"):
                os.ftruncate(fd, 0); os.write(fd, f"{os.getpid()} {label}\n".encode())
                log(f"QUEUE start {label} (waited {int(time.time() - t0)}s): {' '.join(cmd)[:120]}")
                rc = subprocess.call(["taskpolicy", "-b"] + cmd)
                log(f"QUEUE done {label} rc={rc} after {int(time.time() - t0)}s")
                os.close(fd)  # releases the slot
                return rc
            os.close(fd)
        if not said:
            print(f"clearx: '{label}' is queued (another heavy job is running or memory is tight)...", file=sys.stderr)
            said = True
        if time.time() - t0 > timeout_h * 3600:
            print("clearx: gave up waiting", file=sys.stderr); return 75
        time.sleep(5)


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
    so = int((re.search(r"Swapouts:\s+(\d+)", sh("vm_stat")) or [0, 0])[1]) * PAGE
    lso = t.get("last_so")
    sinc = (so if last is None else 0) if lso is None else (so - lso if so >= lso else so)
    t["last_so"] = so
    sw = t.setdefault("swap_days", {})
    sw[day] = sw.get(day, 0) + sinc
    t["swap_days"] = dict(sorted(sw.items())[-30:])
    t["boot_w"], t["boot_r"], t["boot_swap"], t["t"] = w, r, so, time.time()
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
NEVER_QUIT = "Finder, Xcode*, Claude*, Terminal, iTerm2, ClearX, Dock, loginwindow, SystemUIServer"


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
    lk = state_lock()
    conf, st = load_conf(), load_state()
    before = snapshot()
    procs = add_footprint(proc_list())
    acts, now = [], time.time()
    compilers = globs(conf.get("build_guard", "compilers", fallback=""))
    daws = globs(conf.get("modes", "studio_apps", fallback=""))
    fpid = front_pid()
    fg = tree(procs, ([fpid] if fpid else []) + [p["pid"] for p in procs.values() if matches(p["name"], daws)])

    if quit_names:  # graceful quit through the app itself (it asks to save unsaved documents)
        os.close(lk)
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
                if info.get("kind") not in ("build", "queue", "hold") and resume(int(k), st, "boost"):
                    acts.append(f"resumed {info['name']} ({k})")
        # 2. every non-foreground heavy app to background QoS (released when you switch to it)
        prot = protected_set(conf)
        for p in procs.values():
            if p["pid"] in fg or matches(p["name"], prot + compilers) or "Z" in p["stat"] or p["pid"] == os.getpid():
                continue
            if p["mem"] >= 300 << 20 and background(p, st, False, "boost"):
                acts.append(f"background QoS: {p['name']} ({p['pid']}, {p['mem'] >> 20} MB)")
        save_state(st)
        os.close(lk)
        # 3. orphaned compile jobs (their build is gone: parent is launchd)
        for p in procs.values():
            if matches(p["name"], compilers) and p["ppid"] == 1:
                try:
                    os.kill(p["pid"], signal.SIGCONT); os.kill(p["pid"], signal.SIGTERM)
                    acts.append(f"ended orphaned {p['name']} ({p['pid']}, {p['mem'] >> 20} MB)")
                    log(f"BOOST SIGTERM orphaned {p['name']} pid={p['pid']}")
                except OSError:
                    pass
        # 4. leaky third-party LaunchAgents that restart themselves (KeepAlive): RSS > 2x what clearx first saw
        import plistlib
        for label, info in st.get("agent_rss", {}).items():
            pr = procs.get(info["pid"])
            pl = os.path.expanduser(f"~/Library/LaunchAgents/{label}.plist")
            if not pr or label.startswith(("com.user.clearx", "com.mydaw.")) or not os.path.exists(pl):
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
        # 6. booted iOS simulators, when no build or test is running (each is a whole iOS in RAM)
        busy = any(matches(p["name"], compilers + ["xcodebuild", "ctest", "XCBBuildService", "SWBBuildService"])
                   for p in procs.values())
        booted = re.findall(r"\(([0-9A-F-]{36})\) \(Booted\)", sh("xcrun", "simctl", "list", "devices", "booted"))
        if booted and not busy:
            subprocess.run(["xcrun", "simctl", "shutdown", "all"], capture_output=True, timeout=60)
            acts.append(f"shut down {len(booted)} iOS simulator(s) (Xcode starts them again when needed)")
        # 7. browsers: tab discarding is the browser's own (Brave Memory Saver policy is on)
        if subprocess.run(["defaults", "read", "com.brave.Browser", "HighEfficiencyModeEnabled"],
                          capture_output=True, text=True).stdout.strip() == "1":
            acts.append("Brave Memory Saver is on: Brave discards inactive tabs itself (no outside switch exists)")
        time.sleep(5)
    # idle apps: not frontmost for 30+ min, > 300 MB (clearx has watched since it started tracking)
    seen, since = st.get("front_seen", {}), st.get("front_track_t", now)
    safe = globs(conf.get("boost", "safe_quit", fallback=SAFE_QUIT_DEFAULT))
    idle = []
    for a in gui_apps():
        if a["pid"] == fpid or a["name"] in ("ClearX",):
            continue
        mem = sum(procs[p]["mem"] for p in tree(procs, [a["pid"]]) if p in procs)
        idle_s = now - seen.get(a["name"], since)
        if idle_s >= 1800 and mem >= 300 << 20:
            idle.append(dict(name=a["name"], pid=a["pid"], mem=mem, idle_min=int(idle_s / 60),
                             safe=matches(a["name"], safe) and not matches(a["name"], globs(NEVER_QUIT) + daws),
                             never=matches(a["name"], globs(NEVER_QUIT) + daws)))
    after = snapshot()
    r = dict(before=before, after=after, actions=acts, idle=sorted(idle, key=lambda x: -x["mem"]), t=now)
    lk = state_lock()
    st = load_state(); st["boost"] = r; save_state(st)
    os.close(lk)
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
            print("\nIdle apps (not used for 30+ min, > 300 MB). Quit with: clearx boost --quit \"App, App\"")
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
        if dr.get("wear_pct") is not None:
            print(f"  wear: {dr['wear_pct']}% of its life used; {H(dr['lifetime_written'])} written, "
                  f"{H(dr['lifetime_read'])} read over {dr['power_on_hours']} powered-on hours (endurance ~{H(dr.get('endurance', 0))})")
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
    if sub == "plan":
        d["plan"] = maint.storage_plan(d, int(args[1]) if len(args) > 1 else 250)
        P = d["plan"]
        print(f"Goal {H(P['goal'])} free: now {H(P['free'])}, gap {H(P['gap'])}; candidates {H(P['candidates'])} -> "
              + ("reachable" if P["reachable"] else f"NOT reachable (short {H(P['gap'] - P['candidates'])})"))
        for i in P["items"]:
            print(f"  {H(i['size']):>10}  {i['title']}\n{'':14}{i['how']}" + (f"\n{'':14}note: {i['note']}" if i["note"] else ""))
    if sub == "needs":
        print(json.dumps(maint.needs(d, load_state())))
    if sub == "fix" and len(args) > 1:
        ok = maint.fix(args[1], log)
        if args[1] == "iosdev":
            print(f"Freed {ok[0] / 1e9:.1f} GB now; {ok[1] / 1e9:.1f} GB of iPhone debug symbols went to the Trash "
                  "(empty it to free that too).")
            d["junk"] = maint.scan_junk(); d["junk_t"] = time.time()
            d["plan"] = maint.storage_plan(d, round(d.get("plan", {}).get("goal", 250 * 10 ** 9) / 10 ** 9))
        else:
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
    print(f"ClearX: {'OFF' if os.path.exists(OFF_FILE) else 'ON (Max)'}  pool={st.get('level', '?')}  "
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
    if cmd in ("tick", "wake", "mode", "pause", "resume", "off"):
        state_lock()  # released when this process exits
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
    elif cmd == "run" and len(argv) > 3:  # clearx run "<label>" [--slots N] <command...>
        a = argv[3:]
        slots = 1
        if a[0] == "--slots":
            slots, a = int(a[1]), a[2:]
        return run_queued(argv[2], a, slots)
    elif cmd == "wake":  # clearx wake [all|<app name>]: unfreeze
        st, name = load_state(), " ".join(argv[2:]) or "all"
        for k, i in list(st.get("paused", {}).items()):
            if i.get("kind") == "freeze" and (name == "all" or matches(i.get("app", ""), [name])):
                resume(int(k), st, "woken by hand")
        save_state(st)
        print("woken")
    elif cmd == "boost":
        q = argv[argv.index("--quit") + 1] if "--quit" in argv else None
        return boost([x.strip() for x in q.split(",") if x.strip()] if q else None, as_json="--json" in argv)
    elif cmd == "optimize":
        return maintain(["optimize"] + argv[2:])
    elif cmd == "bench" and len(argv) > 3 and argv[2] == "run":
        # clearx bench run <label> [--guard-swap 3G] [--guard-avail 500M] -- <shell command>
        a = argv[3:]
        cmd_ = " ".join(a[a.index("--") + 1:]) if "--" in a else ""
        opt = lambda k: size(a[a.index(k) + 1]) if k in a else None
        r = bench_run(a[0], cmd_, opt("--guard-swap"), opt("--guard-avail"), max_s=int(dict(zip(a, a[1:])).get("--max-seconds", 0)) or None)
        return 0 if r["rc"] == 0 else 1
    elif cmd == "bench":
        return bench(int(argv[2]) if len(argv) > 2 else 60)
    elif cmd == "throttle-loop":
        throttle_loop(int(argv[2]), float(argv[3]))
    elif cmd in ("pause-pid", "resume-pid", "bg-pid", "fg-pid", "quit-pid", "throttle-pid", "unthrottle-pid") and len(argv) > 2:
        state_lock()
        return pid_action(cmd, int(argv[2]))
    elif cmd == "mode":
        st = load_state()
        st.pop("mode_override", None)
        save_state(st)
        print("ClearX has one mode, Max: it adapts by itself (a DAW or a build running only changes priorities)")
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
        resume_all(st, "ClearX turned off")
        for k in list(st.get("bg", {})):
            unbackground(int(k), st, "ClearX turned off")
        for k in list(st.get("throttled", {})):
            stop_throttle(int(k), st, "ClearX turned off")
        # the Xcode job cap is clearx's: off = Xcode's own default (one compile per CPU thread)
        subprocess.run(["defaults", "delete", "com.apple.dt.Xcode", "IDEBuildOperationMaxNumberOfConcurrentCompileTasks"],
                       capture_output=True)
        for v in ("CMAKE_BUILD_PARALLEL_LEVEL", "MAKEFLAGS"):
            subprocess.run(["launchctl", "unsetenv", v], capture_output=True)
        st.pop("xcode_jobs", None)
        save_state(st)
        log("ClearX OFF")
        print("ClearX off (all paused processes resumed, background QoS removed)")
    elif cmd == "on":
        if os.path.exists(OFF_FILE):
            os.remove(OFF_FILE)
        log("ClearX ON")
        print("ClearX on")
    elif cmd == "log":
        n = argv[2] if len(argv) > 2 else "40"
        os.execvp("tail", ["tail", "-n", n, LOG_FILE])
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
