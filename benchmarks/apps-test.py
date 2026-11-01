#!/usr/bin/env python3
"""20-app test (clearx ON): open ~20 of your own apps + Brave tabs, let it settle, measure memory, CPU and
how fast each app comes to the front. Pass 2 repeats while a clean CMake build runs (--build-dir).
Quits only the apps this test opened (politely; they were freshly opened with no documents).
  python3 benchmarks/apps-test.py [--build-dir <cmake build dir>] [--settle 300]
"""
import json, os, re, signal, subprocess, sys, time

D = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, os.path.join(D, "src"))
import clearx as mg  # noqa: E402

APPS = ["Brave Browser", "Xcode", "Music", "Mail", "Messages", "Notes", "Calendar", "Reminders",
        "Photos", "Maps", "Preview", "TextEdit", "System Settings", "Activity Monitor", "Terminal", "Claude", "ChatGPT",
        "Safari", "Finder"]
TABS = ["https://www.youtube.com", "https://www.google.com/maps", "https://github.com/juce-framework/JUCE",
        "https://en.wikipedia.org/wiki/Digital_audio_workstation", "https://www.reddit.com/r/audioengineering/",
        "https://developer.apple.com/documentation/metal", "https://www.bbc.com/news", "https://www.nytimes.com",
        "https://stackoverflow.com/questions", "https://www.apple.com/imac/", "https://music.youtube.com",
        "https://www.amazon.com", "https://docs.python.org/3/", "https://news.ycombinator.com",
        "https://www.figma.com/community"]


def sh(*a, timeout=60):
    return subprocess.run(a, capture_output=True, text=True, timeout=timeout).stdout


def running_apps():
    return {a["name"] for a in mg.gui_apps()}


def front_name():
    asn = sh("lsappinfo", "front").strip()
    mt = re.search(r'"LSDisplayName"="([^"]+)"', sh("lsappinfo", "info", "-only", "name", asn)) if asn else None
    return mt.group(1) if mt else ""


def switch_times(apps):
    """Seconds from asking for each app until macOS reports it frontmost (LaunchServices, no AppleScript)."""
    out = {}
    for a in apps:
        disp = "Xcode" if a.startswith("Xcode") else a
        t = time.time()
        subprocess.run(["open", "-a", a], capture_output=True)
        while time.time() - t < 15 and front_name() != disp:
            time.sleep(0.02)
        out[a] = round(time.time() - t, 2)
        time.sleep(0.5)
    return out


def sample(seconds, step=5):
    rows, last = [], mg.metrics()["swapouts"]
    end = time.time() + seconds
    while time.time() < end:
        t0 = time.time()
        m, (spd, _) = mg.metrics(), mg.thermal()
        lvl = mg.sysctl_level() if hasattr(mg, "sysctl_level") else None
        rows.append(dict(avail=m["avail"], free=m["free"], cached=m["cached"], compressed=m["compressed"], swap=m["swap"],
                         swapout_mb_s=round(max(0, m["swapouts"] - last) * 4096 / (1 << 20) / step, 1),
                         pressure_pct=100 - int(sh("sysctl", "-n", "kern.memorystatus_level").strip() or 100),
                         speed=spd, load=mg.loadavg()[0]))
        last = m["swapouts"]
        time.sleep(max(0, step - (time.time() - t0)))
    return rows


def summarize(rows, sw):
    G = lambda b: round(b / (1 << 30), 2)
    t = sorted(sw.values())
    return dict(pressure_pct_max=max(r["pressure_pct"] for r in rows),
                pressure_pct_avg=round(sum(r["pressure_pct"] for r in rows) / len(rows)),
                ready_gb_min=G(min(r["free"] + r["cached"] for r in rows)),
                avail_gb_min=G(min(r["avail"] for r in rows)),
                compressed_gb_max=G(max(r["compressed"] for r in rows)),
                swap_gb_max=G(max(r["swap"] for r in rows)),
                swapout_mb_s_avg=round(sum(r["swapout_mb_s"] for r in rows) / len(rows), 1),
                swapout_mb_s_max=max(r["swapout_mb_s"] for r in rows),
                speed_min=min(r["speed"] for r in rows), load_max=max(r["load"] for r in rows),
                switch_median_s=t[len(t) // 2], switch_worst_s=t[-1],
                switch_worst_app=max(sw, key=sw.get), switch=sw)


def main():
    a = sys.argv
    settle = int(a[a.index("--settle") + 1]) if "--settle" in a else 300
    bdir = a[a.index("--build-dir") + 1] if "--build-dir" in a else None
    apps = [x for x in APPS if os.path.exists(f"/Applications/{x}.app") or os.path.exists(f"/System/Applications/{x}.app")
            or os.path.exists(f"/System/Applications/Utilities/{x}.app") or x == "Finder"]
    before = running_apps()
    res = dict(start=time.time(), apps=apps, already_open=sorted(before), clearx="on")
    print(f"opening {len(apps)} apps + {len(TABS)} Brave tabs", flush=True)
    w0 = mg.ssd_counters()[0]
    for x in apps:
        subprocess.run(["open", "-g", "-a", x], capture_output=True)
        time.sleep(2)
    for u in TABS:
        subprocess.run(["open", "-g", "-a", "Brave Browser", u], capture_output=True)
        time.sleep(1)
    opened = sorted(running_apps() - before)
    res["opened_by_test"] = opened
    print(f"settling {settle}s", flush=True)
    rows = sample(settle)
    res["pass1"] = summarize(rows, switch_times(apps))
    res["pass1"]["ssd_written_mb"] = round((mg.ssd_counters()[0] - w0) / (1 << 20))
    print(json.dumps(res["pass1"], indent=1), flush=True)

    if bdir:
        print("pass 2: same apps + clean build", flush=True)
        subprocess.run(["cmake", "--build", bdir, "--target", "clean", "--config", "Release"], capture_output=True)
        w1, t = mg.ssd_counters()[0], time.time()
        b = subprocess.Popen(["cmake", "--build", bdir, "--config", "Release", "--", "-quiet", "ONLY_ACTIVE_ARCH=NO",
                              f"MODULE_CACHE_DIR={bdir}/ModuleCache2"], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        rows = sample(120)
        sw = switch_times(apps)
        while b.poll() is None:
            rows += sample(30)
        res["pass2"] = summarize(rows, sw)
        res["pass2"].update(build_s=round(time.time() - t), build_rc=b.returncode,
                            ssd_written_mb=round((mg.ssd_counters()[0] - w1) / (1 << 20)))
        print(json.dumps({k: v for k, v in res["pass2"].items() if k != "switch"}, indent=1), flush=True)

    # quit only what this test opened (fresh, no documents): politely, SIGTERM as a fallback
    for name in opened:
        if name in ("Finder", "Claude", "ClearX"):
            continue
        r = subprocess.run(["osascript", "-e", f'tell application "{name}" to quit'], capture_output=True, timeout=30)
        if r.returncode != 0:
            for p in mg.gui_apps():
                if p["name"] == name:
                    os.kill(p["pid"], signal.SIGTERM)
    res["end"] = time.time()
    path = os.path.join(D, "bench", time.strftime("apps-%Y%m%d-%H%M%S.json"))
    with open(path, "w") as f:
        json.dump(res, f, indent=1)
    print(path)


if __name__ == "__main__":
    main()
