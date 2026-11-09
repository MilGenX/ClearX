"""clearx health - what's wrong with the Mac, found by itself once a day (with the disk keeper) and written down.

X 2026-11-08: "go through the whole computer, find everything wrong with it". The full check-up found what nothing was
watching: no backup at all, 121 GB gone in four days, macOS's update service stuck for four days, a caffeinate left
holding the Mac awake after its app quit, crash bursts, a staged `_to_delete` with 18 GB in it. Each of those is now a
check here. Every run writes ~/Documents/ClearX Health.md (the whole picture, newest first) and notifies only what is
new and serious, once.

One thing it fixes itself: a `caffeinate` whose app is gone (its parent is launchd) holding the Mac awake for more than
`caffeinate_h` hours is ended. Everything else is said, never done.
"""
import glob, json, os, re, subprocess, time

import maint
from maint import human

DAY = 86400


def run(*a, timeout=120):
    try:
        r = subprocess.run(a, capture_output=True, timeout=timeout)
        return (r.stdout + r.stderr).decode("utf-8", "replace")
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"(failed: {e})"


def cfg(key, default):
    return maint.setting("health", key, default)


def etime_s(e):
    """ps's etime ([[dd-]hh:]mm:ss) in seconds."""
    d, _, rest = e.strip().rpartition("-")
    parts = [int(x) for x in rest.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    return (int(d) if d else 0) * DAY + parts[0] * 3600 + parts[1] * 60 + parts[2]


def procs():
    """[(pid, ppid, cpu, seconds running, command)]"""
    out = []
    for ln in run("ps", "-Ao", "pid=,ppid=,pcpu=,etime=,command=").splitlines():
        f = ln.split(None, 4)
        if len(f) == 5:
            try:
                out.append((int(f[0]), int(f[1]), float(f[2]), etime_s(f[3]), f[4]))
            except ValueError:
                pass
    return out


# ---------- the checks: each returns [(level, key, text)], level "serious" | "fix" | "note" ----------
def backup():
    if "No destinations configured" in run("tmutil", "destinationinfo"):
        return [("serious", "no-backup", "Nothing backs this Mac up: Time Machine has no backup disk. An external drive "
                 "(or a network one) in System Settings › General › Time Machine fixes it.")]
    latest = run("tmutil", "latestbackup").strip()
    m = re.search(r"(\d{4}-\d{2}-\d{2})-(\d{6})", latest)
    if m and time.time() - time.mktime(time.strptime(m.group(1), "%Y-%m-%d")) > 7 * DAY:
        return [("serious", "backup-old", f"The last Time Machine backup is from {m.group(1)}.")]
    return []


def disk(history):
    out = []
    free = maint.available() if hasattr(maint, "available") else 0
    try:
        st = os.statvfs(os.path.expanduser("~"))
        free = free or st.f_bavail * st.f_frsize
        total = st.f_blocks * st.f_frsize
    except OSError:
        return out
    if total and free / total < float(cfg("free_min_share", "0.2")):
        out.append(("serious", "low-disk", f"Only {human(free)} free of {human(total)}."))
    if history:
        past = [h for h in history if time.time() - h["t"] >= 3 * DAY]
        if past and past[-1]["free"] - free > 30 * 10 ** 9:
            out.append(("serious", "disk-falling", f"{human(past[-1]['free'] - free)} of free space gone since "
                        f"{time.strftime('%b %d', time.localtime(past[-1]['t']))}."))
    history.append(dict(t=time.time(), free=free))
    del history[:-60]
    return out


def stuck(ps):
    out = []
    for pid, ppid, cpu, secs, cmd in ps:
        if "softwareupdated" in cmd and cpu > 10 and secs > DAY:
            out.append(("fix", "softwareupdated", f"macOS's update service has used {cpu:.0f}% CPU for {secs // DAY} days: "
                        "a restart clears it."))
    return out


def awake(ps, log, fix=True):
    """caffeinate left behind (parent gone) for more than caffeinate_h hours: ended."""
    out, limit = [], float(cfg("caffeinate_h", "12")) * 3600
    for pid, ppid, cpu, secs, cmd in ps:
        if os.path.basename(cmd.split()[0]) == "caffeinate" and ppid == 1 and secs > limit:
            if fix:
                try:
                    os.kill(pid, 15)
                    log(f"HEALTH ended caffeinate pid {pid} ({cmd}): its app was gone, it held the Mac awake {secs // 3600} h")
                    out.append(("note", f"caffeinate-{pid}", f"Ended a leftover `{cmd}` that held the Mac awake for {secs // 3600} h."))
                    continue
                except OSError:
                    pass
            out.append(("fix", f"caffeinate-{pid}", f"`{cmd}` (pid {pid}) has held the Mac awake for {secs // 3600} h."))
    return out


def uptime(ps):
    up = next((secs for pid, ppid, cpu, secs, cmd in ps if pid == 1), 0)
    if up > int(cfg("restart_days", "14")) * DAY:
        return [("fix", "uptime", f"Running {up // DAY} days without a restart: memory and stuck services build up.")]
    return []


def memory():
    m = re.search(r"used = ([\d.]+)M", run("sysctl", "vm.swapusage"))
    if m and float(m.group(1)) > float(cfg("swap_mb", "6000")):
        return [("fix", "swap", f"{float(m.group(1)) / 1024:.1f} GB of memory is on the disk (swap): the Mac is short of "
                 "memory; close what isn't needed, or restart.")]
    return []


def crashes(since_h=24):
    """The same program crashing 3+ times in the last day."""
    count, cut = {}, time.time() - since_h * 3600
    for d in (os.path.expanduser("~/Library/Logs/DiagnosticReports"), "/Library/Logs/DiagnosticReports"):
        for f in glob.glob(d + "/*.ips") + glob.glob(d + "/*.crash"):
            try:
                if os.path.getmtime(f) < cut:
                    continue
            except OSError:
                continue
            name = re.sub(r"[-_]\d{4}-\d{2}-\d{2}.*$", "", os.path.basename(f))
            count[name] = count.get(name, 0) + 1
    return [("fix", f"crash-{n}", f"{n} crashed {c} times in the last day.") for n, c in sorted(count.items(), key=lambda x: -x[1]) if c >= 3]


def leftovers(root=None, days=3):
    """Folders staged for deletion (_to_delete, *.old, ...) in ~/Developer and the home folder, never finished."""
    out = []
    for base in (root or os.path.join(os.path.expanduser("~"), "Developer"), os.path.expanduser("~")):
        for p in glob.glob(base + "/*/_to_delete") + glob.glob(base + "/_to_delete") + glob.glob(base + "/*/*.clearx-old.app"):
            try:
                if time.time() - os.path.getmtime(p) > days * DAY:
                    out.append(("fix", f"leftover-{p}", f"{p.replace(os.path.expanduser('~'), '~')} ({human(maint.du(p))}) was "
                                "staged for deletion and never finished."))
            except OSError:
                pass
    return out


def clutter():
    """Things lying on the Desktop or loose in Downloads for more than a week."""
    out, home = [], os.path.expanduser("~")
    for d in ("Desktop", "Downloads"):
        old = [n for n in os.listdir(os.path.join(home, d)) if not n.startswith(".")
               and time.time() - os.path.getmtime(os.path.join(home, d, n)) > 7 * DAY] if os.path.isdir(os.path.join(home, d)) else []
        if len(old) >= 5:
            out.append(("note", f"clutter-{d}", f"{len(old)} things have been sitting in {d} for over a week."))
    return out


def homebrew():
    for b in ("/usr/local/bin/brew", "/opt/homebrew/bin/brew"):
        if os.path.exists(b):
            txt = run(b, "doctor", timeout=300)
            warns = [w for w in re.findall(r"^Warning: (.+)$", txt, re.M) if "Intel x86_64" not in w and "outdated" not in w]
            return [("note", "brew", "Homebrew: " + "; ".join(warns))] if warns else []
    return []


CHECKS = ("backup", "disk", "stuck", "awake", "uptime", "memory", "crashes", "leftovers", "clutter", "homebrew")


def check(log=print, state=None, fix=True):
    """All checks. Returns (findings, new serious ones since the last run)."""
    state = state if state is not None else {}
    ps = procs()
    found = []
    for name in CHECKS:
        if cfg(name, "true").lower() == "false":
            continue
        try:
            f = {"disk": lambda: disk(state.setdefault("free", [])), "stuck": lambda: stuck(ps),
                 "awake": lambda: awake(ps, log, fix), "uptime": lambda: uptime(ps)}.get(name, globals()[name])
            found += f()
        except Exception as e:  # one check's trouble never stops the others
            log(f"HEALTH {name} failed: {e}")
    seen = set(state.get("seen", []))
    new = [f for f in found if f[0] == "serious" and f[1] not in seen]
    state["seen"] = sorted({f[1] for f in found})
    return found, new


def write_report(found, path=None):
    path = path or os.path.join(os.path.expanduser("~"), "Documents", "ClearX Health.md")
    icon = {"serious": "🔴", "fix": "🟠", "note": "🟡"}
    lines = [f"# Mac health — {time.strftime('%a %b %d %Y, %H:%M')}", ""]
    if not found:
        lines.append("Nothing wrong found. 🟢")
    for lvl in ("serious", "fix", "note"):
        for f in [f for f in found if f[0] == lvl]:
            lines.append(f"- {icon[lvl]} {f[2]}")
    lines += ["", "_Written by ClearX each day (src/health.py). `clearx maintain health` checks now._", ""]
    try:
        with open(path) as fh:
            old = fh.read()
    except OSError:
        old = ""
    text = "\n".join(lines) + ("\n---\n\n" + old if old else "")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(text[:200000])  # (the newest days first; old ones fall off)
    return path
