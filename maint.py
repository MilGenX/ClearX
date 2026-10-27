"""memguard maintain - transparent CleanMyMac replacement.

Every action previews first with sizes. Removal = move to ~/.Trash (recoverable), logged.
Nothing here empties the Trash, uses sudo, or touches system files.
Results are cached in state/maint.json for the menu-bar app.
"""
import glob, json, os, plistlib, re, shutil, subprocess, time

HOME = os.path.expanduser("~")
TRASH = os.path.join(HOME, ".Trash")
DAY = 86400
# folders under ~/Library/Caches that a category below already counts, or that must not be moved
CACHE_SKIP = {"Homebrew", "pip", "Yarn", "com.apple.dt.Xcode", "CloudKit", "com.apple.bird",
              "com.apple.HomeKit", "FamilyCircle", "com.apple.containermanagerd", "com.apple.ap.adprivacyd"}


def sh(*a, timeout=120):
    try:
        return subprocess.run(a, capture_output=True, timeout=timeout).stdout.decode("utf-8", errors="replace")
    except (subprocess.TimeoutExpired, OSError):
        return ""


def du(path):
    out = sh("du", "-sk", path, timeout=300).split()
    return int(out[0]) * 1024 if out and out[0].isdigit() else 0


def human(b):
    for u in ("B", "KB", "MB", "GB", "TB"):
        if b < 1024 or u == "TB":
            return f"{b:.1f} {u}" if u not in ("B", "KB") else f"{b:.0f} {u}"
        b /= 1024


def running_ids():
    """Bundle ids and names of running apps, to skip their caches."""
    ids = set(re.findall(r'bundleID="([^"]+)"', sh("lsappinfo", "list")))
    names = {os.path.basename(l.strip()).lower() for l in sh("ps", "-axo", "comm=").splitlines()}
    return {i.lower() for i in ids}, names


def item(path, size=None, note=""):
    return {"path": path, "size": du(path) if size is None else size, "note": note}


# ---------- junk scan ----------
def scan_junk():
    ids, names = running_ids()
    cats = []

    caches = []
    croot = os.path.join(HOME, "Library/Caches")
    for d in sorted(os.listdir(croot)) if os.path.isdir(croot) else []:
        p = os.path.join(croot, d)
        if d in CACHE_SKIP or d.startswith(".") or not os.path.isdir(p):
            continue
        low = d.lower()
        if low in ids or any(low.endswith("." + n) or low == n for n in names if len(n) > 3):
            continue  # app is running: its cache is in use
        it = item(p)
        if it["size"] > 1 << 20:
            caches.append(it)
    cats.append(dict(id="caches", title="App caches (apps not running)", items=caches, action="trash",
                     note="Apps rebuild these on demand."))

    logs = []
    for root in (os.path.join(HOME, "Library/Logs"),):
        for dp, dn, fn in os.walk(root):
            for f in fn:
                p = os.path.join(dp, f)
                try:
                    st = os.lstat(p)
                except OSError:
                    continue
                if time.time() - st.st_mtime > 7 * DAY:
                    logs.append(item(p, st.st_size))
    cats.append(dict(id="logs", title="User logs older than 7 days", items=logs, action="trash"))

    xc = []
    dev = os.path.join(HOME, "Library/Developer")
    dd = os.path.join(dev, "Xcode/DerivedData")
    for d in glob.glob(dd + "/*"):
        xc.append(item(d, note="DerivedData (rebuilt by Xcode)"))
    for d in glob.glob(dev + "/Xcode/Archives/*/*"):
        if time.time() - os.path.getmtime(d) > 90 * DAY:
            xc.append(item(d, note="archive older than 90 days"))
    for kind in ("iOS DeviceSupport", "watchOS DeviceSupport", "macOS DeviceSupport", "tvOS DeviceSupport"):
        vers = sorted(glob.glob(f"{dev}/Xcode/{kind}/*"), key=os.path.getmtime)
        for d in vers[:-1]:  # keep the newest one
            xc.append(item(d, note=f"{kind} (older version; newest kept)"))
    for d in glob.glob(dev + "/Xcode/iOS Device Logs/*"):
        xc.append(item(d, note="old device logs"))
    cats.append(dict(id="xcode", title="Xcode junk", items=xc, action="trash"))

    sims = []
    out = sh("xcrun", "simctl", "list", "devices", "unavailable", "-j", timeout=60)
    try:
        for rt, devs in json.loads(out).get("devices", {}).items():
            for d in devs:
                p = d.get("dataPath", "").rsplit("/data", 1)[0]
                if p and os.path.isdir(p):
                    sims.append(item(p, note=f"unavailable simulator {d.get('name')} ({rt.split('.')[-1]})"))
    except ValueError:
        pass
    cats.append(dict(id="simulators", title="Unavailable simulators", items=sims, action="command",
                     command="xcrun simctl delete unavailable"))

    devc = []
    brew_cache = sh("brew", "--cache").strip() if shutil.which("brew") or os.path.exists("/usr/local/bin/brew") else ""
    for p, note in ((brew_cache, "Homebrew downloads"), (HOME + "/Library/Caches/pip", "pip"),
                    (HOME + "/.npm/_cacache", "npm"), (HOME + "/Library/Caches/Yarn", "yarn")):
        if p and os.path.isdir(p):
            devc.append(item(p, note=note))
    cats.append(dict(id="devcaches", title="Developer caches (brew, pip, npm, yarn)", items=devc, action="trash"))

    dl = []
    for p in glob.glob(HOME + "/Downloads/*"):
        if p.lower().endswith((".dmg", ".pkg", ".zip.download")) and time.time() - os.path.getmtime(p) > 30 * DAY:
            dl.append(item(p, note="installer older than 30 days"))
    cats.append(dict(id="installers", title="Old installers in Downloads", items=dl, action="trash"))

    # optional: only junk if you no longer develop/test for iOS. Not counted in the safe total.
    opt = []
    for d in glob.glob(f"{dev}/Xcode/iOS DeviceSupport/*"):
        opt.append(item(d, note="symbols for your iPhone; re-copied (slowly) next time you debug on it"))
    for d in glob.glob(f"{dev}/CoreSimulator/Devices/*"):
        it = item(d)
        if it["size"] > 500 << 20:
            udid = os.path.basename(d)
            it.update(note=f"simulator data; reset with: xcrun simctl erase {udid}")
            opt.append(it)
    rt = sh("xcrun", "simctl", "runtime", "list", "-j", timeout=60)
    try:
        for rid, r in json.loads(rt).items():
            opt.append(dict(path=r.get("path") or rid, size=int(r.get("sizeBytes", 0)),
                            note=f"{r.get('platformIdentifier', '').split('.')[-1]} {r.get('version')} simulator runtime; "
                                 f"remove with: xcrun simctl runtime delete {rid}"))
    except (ValueError, AttributeError):
        pass
    cats.append(dict(id="optional", title="Optional: iOS dev data (only if you don't need it)", items=opt,
                     action="fix:iosdev", command="clearx maintain fix iosdev"))

    cats.append(dict(id="trash", title="Trash (already deleted; empty it in Finder)",
                     items=[item(TRASH)] if os.path.isdir(TRASH) else [], action="none"))
    for c in cats:
        c["size"] = sum(i["size"] for i in c["items"])
    return cats


def move_to_trash(path, log):
    if not os.path.exists(path) or not path.startswith(HOME + "/") or path.startswith(TRASH):
        return 0
    size = du(path)
    dest = os.path.join(TRASH, os.path.basename(path.rstrip("/")))
    if os.path.exists(dest):
        dest += time.strftime(" %H.%M.%S")
    try:
        shutil.move(path, dest)
    except OSError as e:
        log(f"MAINT trash failed {path}: {e}")
        return 0
    log(f"MAINT trashed {path} ({human(size)})")
    return size


def clean(cat_ids, log, junk=None):
    junk = junk or scan_junk()
    freed = 0
    for c in junk:
        if c["id"] in cat_ids and c["action"] == "trash":
            for i in c["items"]:
                freed += move_to_trash(i["path"], log)
    return freed


# ---------- large & old files ----------
def large_old(min_big="500M", old_days=365, old_min="50M"):
    out = sh("find", HOME, "-xdev", "(", "-path", TRASH, "-o", "-path", HOME + "/Library/Containers", "-o",
             "-path", HOME + "/Library/Group Containers", "-o", "-name", "node_modules", ")", "-prune", "-o",
             "-type", "f", "(", "-size", "+" + min_big, "-o", "(", "-size", "+" + old_min, "-atime", f"+{old_days}",
             ")", ")", "-print", timeout=600)
    res = []
    for p in out.splitlines():
        try:
            st = os.stat(p)
        except OSError:
            continue
        res.append(dict(path=p, size=st.st_size, last_used=time.strftime("%Y-%m-%d", time.localtime(st.st_atime))))
    return sorted(res, key=lambda r: -r["size"])[:200]


# ---------- app uninstaller ----------
def app_info(app):
    if not app.endswith(".app"):
        for base in ("/Applications", HOME + "/Applications"):
            if os.path.isdir(f"{base}/{app}.app"):
                app = f"{base}/{app}.app"
                break
    try:
        with open(os.path.join(app, "Contents/Info.plist"), "rb") as f:
            info = plistlib.load(f)
    except OSError:
        info = {}
    return app, info.get("CFBundleIdentifier", ""), info.get("CFBundleName") or os.path.basename(app)[:-4]


def leftovers(app, vendor_prefix=None):
    """Find an app's files in ~/Library by bundle id (and optional vendor prefix like com.macpaw)."""
    app, bid, name = app_info(app)
    keys = {k.lower() for k in (bid, name) if k}
    if vendor_prefix:
        keys.add(vendor_prefix.lower())
    lib = HOME + "/Library"
    places = ["Application Support", "Caches", "Preferences", "Containers", "Group Containers", "LaunchAgents",
              "Saved Application State", "HTTPStorages", "WebKit", "Logs", "Cookies", "Application Scripts",
              "Preferences/ByHost"]
    found = []
    for pl in places:
        for p in glob.glob(f"{lib}/{pl}/*"):
            b = os.path.basename(p).lower()
            if any(b == k or b.startswith(k + ".") or b.startswith(k + " ") or (len(k) > 6 and k in b) for k in keys):
                found.append(item(p))
    res = dict(app=app if os.path.isdir(app) else None, bundle_id=bid, name=name, items=found)
    if res["app"]:
        res["app_size"] = du(app)
    return res


def uninstall(app, log, vendor_prefix=None, apply=False):
    r = leftovers(app, vendor_prefix)
    if not apply:
        return r
    for p in [i["path"] for i in r["items"] if "LaunchAgents" in i["path"]]:
        label = os.path.basename(p)[:-6] if p.endswith(".plist") else None
        if label:
            subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{label}"], capture_output=True)
    r["freed"] = sum(move_to_trash(i["path"], log) for i in r["items"])
    if r["app"]:
        if os.access(os.path.dirname(r["app"]), os.W_OK):
            try:
                dest = os.path.join(TRASH, os.path.basename(r["app"]))
                shutil.move(r["app"], dest if not os.path.exists(dest) else dest + time.strftime(" %H.%M.%S"))
                log(f"MAINT trashed {r['app']}")
                r["freed"] += r.get("app_size", 0)
            except OSError as e:
                log(f"MAINT could not trash {r['app']}: {e}")
    return r


# ---------- login items & launch agents ----------
def _proc_usage():
    use = {}
    for ln in sh("ps", "-axo", "pid=,%cpu=,rss=,command=").splitlines():
        p = ln.split(None, 3)
        if len(p) == 4:
            use.setdefault(p[3].split(" -")[0].strip(), []).append((int(p[0]), float(p[1]), int(p[2]) * 1024))
    return use


def startup():
    use = _proc_usage()
    loaded = sh("launchctl", "list")
    uid = os.getuid()
    disabled = sh("launchctl", "print-disabled", f"gui/{uid}")
    res = []
    for scope, d in (("user", HOME + "/Library/LaunchAgents"), ("all-users agent", "/Library/LaunchAgents"),
                     ("system daemon", "/Library/LaunchDaemons")):
        for p in sorted(glob.glob(d + "/*.plist")):
            try:
                with open(p, "rb") as f:
                    pl = plistlib.load(f)
            except Exception:
                res.append(dict(scope=scope, plist=p, label=os.path.basename(p), program="?", error="unreadable plist"))
                continue
            label = pl.get("Label", os.path.basename(p)[:-6])
            args = pl.get("ProgramArguments") or []
            prog = pl.get("Program") or (args or ["?"])[0]
            procs = [u for k, v in use.items() if k.startswith(prog) or os.path.basename(k) == os.path.basename(prog)
                     for u in v] if prog != "?" else []
            res.append(dict(scope=scope, plist=p, label=label, program=prog,
                            loaded=(label in loaded) if scope == "user" else bool(procs), disabled=f'"{label}" => disabled' in disabled or
                            f'"{label}" => true' in disabled,
                            cpu=round(sum(u[1] for u in procs), 1), rss=sum(u[2] for u in procs), args=args[1:6],
                            interval=pl.get("StartInterval"), keepalive=bool(pl.get("KeepAlive"))))
    items = sh("osascript", "-e", 'tell application "System Events" to get the path of every login item', timeout=20)
    for path in [x.strip() for x in items.split(",") if x.strip()]:
        name = os.path.basename(path).replace(".app", "")
        procs = [u for k, v in use.items() if path in k for u in v]
        res.append(dict(scope="login item", plist=None, label=name, program=path, loaded=bool(procs),
                        cpu=round(sum(u[1] for u in procs), 1), rss=sum(u[2] for u in procs)))
    return res


def set_agent(label, enable, log):
    """User agents only (no sudo): launchctl disable/enable + bootout/bootstrap. Reversible."""
    uid = os.getuid()
    p = f"{HOME}/Library/LaunchAgents/{label}.plist"
    if enable:
        subprocess.run(["launchctl", "enable", f"gui/{uid}/{label}"])
        if os.path.exists(p):
            subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", p], capture_output=True)
    else:
        subprocess.run(["launchctl", "bootout", f"gui/{uid}/{label}"], capture_output=True)
        subprocess.run(["launchctl", "disable", f"gui/{uid}/{label}"])
    log(f"MAINT {'enabled' if enable else 'disabled'} agent {label}")


# ---------- updates ----------
def updates():
    res = {"macos": [], "brew": [], "mas": [], "checked": time.time()}
    out = sh("softwareupdate", "-l", timeout=300)
    for m in re.finditer(r"\* Label: (.+)\n\s+Title: ([^,]+), Version: ([^,]+),.*?(Action: restart)?", out):
        res["macos"].append(dict(label=m.group(1).strip(), title=m.group(2), version=m.group(3),
                                 restart="restart" in out.split(m.group(1))[1].split("\n")[1]))
    res["macos_raw"] = out.strip()[-600:]
    if os.path.exists("/usr/local/bin/brew"):
        b = sh("/usr/local/bin/brew", "outdated", "--json=v2", timeout=300)
        try:
            j = json.loads(b)
            res["brew"] = [dict(name=f["name"], installed=",".join(f.get("installed_versions", [])),
                                current=f.get("current_version")) for f in j.get("formulae", [])] + \
                          [dict(name=c["name"], installed=",".join(c.get("installed_versions", [])) if isinstance(
                              c.get("installed_versions"), list) else str(c.get("installed_versions")),
                                current=c.get("current_version"), cask=True) for c in j.get("casks", [])]
        except ValueError:
            pass
    if shutil.which("mas"):
        res["mas"] = sh("mas", "outdated").splitlines()
    return res


# ---------- security ----------
def _ver(plist):
    try:
        with open(plist, "rb") as f:
            return plistlib.load(f).get("CFBundleShortVersionString")
    except OSError:
        return None


INTERPRETERS = {"sh", "bash", "zsh", "python3", "python", "osascript", "perl", "ruby", "node", "open", "env"}
MINE = ("~/Developer/MacMemoryManager/", "~/Developer/XDAW/", "~/Developer/MYAPP/",
        "~/Applications/MemGuard.app", "~/Applications/ClearX.app", "~/Library/Application Support/MyAppResign/")


def codesign_info(path):
    if not path or path == "?" or not os.path.exists(path):
        return dict(status="missing", detail="program not found")
    try:
        with open(path, "rb") as f:
            if f.read(2) == b"#!":
                return dict(status="script", detail="shell/python script (scripts can't be signed)")
    except OSError:
        pass
    r = subprocess.run(["codesign", "-dv", "--verbose=2", path], capture_output=True, text=True)
    txt = r.stderr
    if r.returncode != 0:
        return dict(status="unsigned", detail=txt.strip().splitlines()[-1] if txt.strip() else "unsigned")
    auth = re.findall(r"Authority=(.+)", txt)
    team = re.search(r"TeamIdentifier=(.+)", txt)
    if "Signature=adhoc" in txt:
        return dict(status="adhoc", detail="ad-hoc signed (no developer identity)")
    ok = subprocess.run(["codesign", "--verify", "--deep", "--strict", path], capture_output=True).returncode == 0
    return dict(status="signed" if ok else "invalid", detail=(auth[0] if auth else "") +
                (f" (team {team.group(1)})" if team else ""))


def security():
    xp = "/Library/Apple/System/Library/CoreServices/"
    res = dict(
        xprotect=_ver(xp + "XProtect.bundle/Contents/Info.plist"),
        xprotect_remediator=_ver(xp + "XProtect.app/Contents/Info.plist"),
        gatekeeper=sh("spctl", "--status").strip(),
        sip=sh("csrutil", "status").strip().split(":")[-1].strip(" ."),
        filevault=sh("fdesetup", "status").strip().splitlines()[0] if sh("fdesetup", "status").strip() else "?",
        firewall=sh("/usr/libexec/ApplicationFirewall/socketfilterfw", "--getglobalstate").strip(),
        spotlight=sh("mdutil", "-s", "/").strip().splitlines()[-1].strip(),
        checked=time.time())
    audit = []
    for s in startup():
        if s["scope"] == "login item":
            prog = s["program"]
            exe = None
            info = os.path.join(prog, "Contents/Info.plist")
            if os.path.exists(info):
                with open(info, "rb") as f:
                    exe = os.path.join(prog, "Contents/MacOS", plistlib.load(f).get("CFBundleExecutable", ""))
            cs = codesign_info(exe or prog)
        else:
            payload = s["program"]
            if os.path.basename(payload) in INTERPRETERS:
                payload = next((a for a in s.get("args", []) if a.startswith("/") and os.path.exists(a)), payload)
                if os.path.basename(payload) in INTERPRETERS:  # inline command (-c "...")
                    payload = s["program"]
            s["program"] = payload
            cs = codesign_info(payload)
        mine = any(s["program"].startswith(m) for m in MINE)
        flag = cs["status"] in ("unsigned", "invalid", "missing") or (cs["status"] in ("adhoc", "script") and not mine)
        audit.append(dict(label=s["label"], scope=s["scope"], program=s["program"], **cs, yours=mine, flag=flag))
    res["persistence"] = audit
    res["flags"] = sum(a["flag"] for a in audit)
    broken = []
    for p in glob.glob(HOME + "/Library/Preferences/*.plist"):
        r = subprocess.run(["plutil", "-lint", p], capture_output=True, text=True)
        if r.returncode != 0 and "Operation not permitted" not in r.stdout + r.stderr:
            broken.append(p)  # privacy-protected Apple files (TCC) are unreadable, not corrupt
    res["broken_prefs"] = broken
    return res


# ---------- health score ----------
def health(st, maint):
    """0-100. Weights: memory 30, swap 15, thermal 15, storage 15, updates 10, security 15.
    Memory, swap and thermal use the last 10 minutes (the daemon's history), not one instant, so a short burst
    (one build step, one heavy page) doesn't swing the score; a problem that lasts still counts in full.
    Scans and measurements never change the score by themselves: they only refresh the facts it reads."""
    score, why = 100, []
    hist = st.get("hist") or []
    lv = [h[6] for h in hist if len(h) > 7]
    if lv:  # average memory level over 10 min: green 0, yellow 10, orange 20, red 30
        pen = round(sum({0: 0, 1: 10, 2: 20, 3: 30}[x] for x in lv) / len(lv))
    else:
        pen = {"green": 0, "yellow": 10, "orange": 20, "red": 30}.get(st.get("level", "green"), 0)
    if pen:
        score -= pen; why.append(f"memory under pressure for part of the last 10 min (-{pen})")
    rates = [h[3] for h in hist if len(h) > 3]
    rate = sum(rates) / len(rates) if rates else 0
    if rate > 1:
        p = min(15, int(rate)); score -= p; why.append(f"swapping {rate:.0f} MB/s on average (-{p})")
    sp = sorted(h[7] for h in hist if len(h) > 7) or [st.get("cpu_speed_limit", 100)]
    spd = sp[len(sp) // 2]  # median CPU speed limit over 10 min
    if spd < 100:
        p = min(15, (100 - spd) // 3); score -= p; why.append(f"CPU heat-throttled to {spd}% (-{p})")
    # storage (15): free space (swap lives here), SMART, drive type
    d = shutil.disk_usage("/System/Volumes/Data")
    free_pct = 100 * d.free / d.total
    p = 0
    if free_pct < 14:
        p += min(15, int(15 - free_pct)); why.append(f"disk {free_pct:.0f}% free")
    if d.free < 40 << 30:
        p += 5; why.append(f"only {d.free >> 30} GB free for swap (< 40 GB)")
    dr = (maint.get("storage") or {}).get("drive") or {}
    if dr.get("smart") and dr["smart"] not in ("Verified", "?"):
        p += 15; why.append(f"SMART {dr['smart']}: back up now")
    if dr.get("kind") in ("HDD", "Fusion Drive"):
        p += 5; why.append(f"boot drive is a {dr['kind']} (slow swap)")
    if p:
        p = min(15, p); score -= p; why[-1] += f" (-{p})"
    up = maint.get("updates") or {}
    n = len(up.get("macos", [])) + len(up.get("brew", []))
    if n:
        p = min(10, 2 * n + (5 if up.get("macos") else 0)); score -= p; why.append(f"{n} updates pending (-{p})")
    sec = maint.get("security") or {}
    p = 0
    if sec:
        p += 5 * ("enabled" not in sec.get("sip", "enabled")) + 5 * ("disabled" in sec.get("gatekeeper", ""))
        p += 2 * sec.get("flags", 0)
        p = min(15, p)
        if p:
            score -= p; why.append(f"security: {sec.get('flags', 0)} flagged startup items (-{p})")
    return max(0, score), why


# ---------- storage ----------
def ssd_write_read():
    out = sh("ioreg", "-r", "-c", "IOBlockStorageDriver", "-w0")
    best = (0, 0)
    for blk in re.findall(r'"Statistics" = \{([^}]*)\}', out):
        w_ = re.search(r'"Bytes \(Write\)"=(\d+)', blk); r_ = re.search(r'"Bytes \(Read\)"=(\d+)', blk)
        if w_ and r_ and int(w_.group(1)) + int(r_.group(1)) > sum(best):
            best = (int(w_.group(1)), int(r_.group(1)))
    return best


def available():
    """What Finder shows as available: free + purgeable ("available for important usage"), via Foundation (JXA)."""
    imp = sh("osascript", "-l", "JavaScript", "-e",
             "ObjC.import('Foundation');var u=$.NSURL.fileURLWithPath('/');var v=Ref();"
             "u.getResourceValueForKeyError(v,$.NSURLVolumeAvailableCapacityForImportantUsageKey,null);"
             "ObjC.unwrap(v[0]).toString()", timeout=20).strip()
    return max(int(imp) if imp.isdigit() else 0, shutil.disk_usage("/System/Volumes/Data").free)


def drive_info():
    """The boot drive: type (SSD / Fusion / HDD), protocol, SMART, TRIM, model, free and purgeable space."""
    di = sh("diskutil", "info", "/")
    g = lambda k: (re.search(rf"^\s*{re.escape(k)}:\s*(.+)$", di, re.M) or [None, ""])[1].strip()
    sp = sh("system_profiler", "SPNVMeDataType", "SPSerialATADataType", timeout=60)
    model = re.search(r"Model: (.+)", sp)
    trim = re.search(r"TRIM Support: (\w+)", sp)
    fusion = g("Fusion Drive") == "Yes"
    kind = "Fusion Drive" if fusion else ("SSD" if g("Solid State") == "Yes" else "HDD")
    d = shutil.disk_usage("/System/Volumes/Data")
    important = available()
    r = dict(kind=kind, protocol=g("Protocol"), smart=g("SMART Status") or "?", trim=trim.group(1) if trim else "?",
             model=model.group(1).strip() if model else "?", size=d.total, free=d.free,
             purgeable=max(0, important - d.free), checked=time.time())
    r.update(ssd_wear())
    return r


def ssd_wear():
    """Real wear from the drive's SMART log (needs smartmontools: brew install smartmontools). NVMe data units are
    512,000 bytes. Endurance is derived from the drive's own 'Percentage Used' instead of a guessed TBW rating."""
    sc = next((x for x in ("/usr/local/bin/smartctl", "/opt/homebrew/bin/smartctl") if os.path.exists(x)), None)
    if not sc:
        return {}
    out = sh(sc, "-a", "disk0", timeout=30)
    num = lambda k: int(re.search(rf"{k}:\s+([\d,]+)", out).group(1).replace(",", "")) if re.search(rf"{k}:\s+([\d,]+)", out) else None
    used, wr, rd, hrs = num("Percentage Used"), num("Data Units Written"), num("Data Units Read"), num("Power On Hours")
    if used is None or wr is None:
        return {}
    w = wr * 512000
    res = dict(wear_pct=used, lifetime_written=w, lifetime_read=(rd or 0) * 512000, power_on_hours=hrs)
    if used > 0:
        res["endurance"] = int(w * 100 / used)
    return res


def speed_test(mb=2048):
    """Sequential write (with fsync) and read of a temp file, then deletes it. Read may come from cache."""
    import tempfile
    path = os.path.join(tempfile.gettempdir(), "memguard-speedtest.bin")
    buf = os.urandom(8 << 20)
    try:
        t = time.time()
        with open(path, "wb") as f:
            for _ in range(mb // 8):
                f.write(buf)
            f.flush(); os.fsync(f.fileno())
        w = mb / (time.time() - t)
        t = time.time()
        with open(path, "rb", buffering=0) as f:
            while f.read(8 << 20):
                pass
        r = mb / (time.time() - t)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    return dict(write_mb_s=round(w), read_mb_s=round(r), size_mb=mb, t=time.time())


def snapshots():
    out = []
    for vol in ("/", "/System/Volumes/Data"):
        out += [dict(volume=vol, name=l.strip()) for l in sh("tmutil", "listlocalsnapshots", vol).splitlines()
                if l.strip() and not l.startswith("Snapshots for")]
    return out


def storage_map(n=25):
    """Top-n folders under ~ (one and two levels deep for Library and Developer) and /Applications."""
    roots = [HOME + "/" + x for x in os.listdir(HOME) if x not in ("Library", "Developer", ".Trash")] + \
        glob.glob(HOME + "/Library/*") + glob.glob(HOME + "/Developer/*") + glob.glob("/Applications/*")
    sizes = []
    for chunk in (roots[i:i + 40] for i in range(0, len(roots), 40)):
        for ln in sh("du", "-sk", *chunk, timeout=900).splitlines():
            k, _, p = ln.partition("\t")
            if k.isdigit():
                sizes.append(dict(path=p, size=int(k) * 1024))
    return sorted(sizes, key=lambda x: -x["size"])[:n]


def duplicates(min_size="50M"):
    """Hash-based duplicates over min_size under ~ (preview only). Same size -> sampled hash -> full hash.
    Only ~/Documents, Downloads, Desktop, Music, Movies, Pictures, and never inside bundles/libraries/code folders."""
    import hashlib
    # only your own documents and media: never app bundles, code, models, caches, cloud or build folders,
    # where an identical copy is usually load-bearing (trashing it would break an app or a project)
    roots = [HOME + "/" + d for d in ("Documents", "Downloads", "Desktop", "Music", "Movies", "Pictures")
             if os.path.isdir(HOME + "/" + d)]
    prune = [x for n in ("*.app", "*.framework", "*.bundle", "*.plugin", "*.vst3", "*.component", "*.mlmodelc",
                                  "*.mlpackage", "*.photoslibrary", "*.musiclibrary", "*.logicx", "node_modules", ".git",
                                  "build", ".build", "DerivedData", ".cache", "site-packages", "Library") for x in ("-name", n)]
    expr = []
    for i in range(0, len(prune), 2):
        expr += (["-o"] if expr else []) + prune[i:i + 2]
    out = sh("find", *roots, "-xdev", "(", *expr, ")", "-prune", "-o", "-type", "f", "-size", "+" + min_size,
             "-print", timeout=900) if roots else ""
    by_size = {}
    for p in out.splitlines():
        try:
            st = os.lstat(p)
        except OSError:
            continue
        by_size.setdefault(st.st_size, []).append(p)

    def hsh(p, sample):
        h = hashlib.blake2b(digest_size=16)
        try:
            with open(p, "rb") as f:
                if sample:
                    sz = os.fstat(f.fileno()).st_size
                    for off in (0, sz // 2, max(0, sz - (1 << 20))):
                        f.seek(off); h.update(f.read(1 << 20))
                else:
                    for b in iter(lambda: f.read(8 << 20), b""):
                        h.update(b)
        except OSError:
            return None
        return h.hexdigest()
    groups = []
    for sz, paths in by_size.items():
        if len(paths) < 2:
            continue
        for stage in (True, False):
            buckets = {}
            for p in paths:
                buckets.setdefault(hsh(p, stage), []).append(p)
            paths = [p for k, v in buckets.items() if k and len(v) > 1 for p in v]
            if not paths:
                break
        full = {}
        for p in paths:
            full.setdefault(hsh(p, False), []).append(p)
        for k, v in full.items():
            if k and len(v) > 1:
                groups.append(dict(size=sz, paths=sorted(v, key=os.path.getmtime), wasted=sz * (len(v) - 1)))
    return sorted(groups, key=lambda g: -g["wasted"])


def special_folders():
    res = []
    for label, p in (("iOS/iPadOS backups", "~/Library/Application Support/MobileSync/Backup"),
                     ("Mail downloads", "~/Library/Containers/com.apple.mail/Data/Library/Mail Downloads"),
                     ("Mail data", "~/Library/Mail"),
                     ("Messages attachments", "~/Library/Messages/Attachments"),
                     ("Xcode DerivedData", "~/Library/Developer/Xcode/DerivedData"),
                     ("Simulator devices", "~/Library/Developer/CoreSimulator/Devices")):
        p = os.path.expanduser(p)
        if not os.path.exists(p):
            res.append(dict(label=label, path=p, size=0, note="none"))
        elif not os.access(p, os.R_OK):
            res.append(dict(label=label, path=p, size=None, note="no access (privacy-protected: give Terminal Full Disk Access to measure)"))
        else:
            sz = du(p)
            res.append(dict(label=label, path=p, size=sz, note="" if sz else "empty or privacy-protected"))
    return res


def storage(full=True):
    r = dict(drive=drive_info(), snapshots=snapshots(), special=special_folders(), t=time.time())
    if full:
        r["speed"] = speed_test()
        r["map"] = storage_map()
        r["duplicates"] = duplicates()
    return r


# ---------- optimize: measure everything that limits this Mac, apply the safe user-level items ----------
def _defaults(domain, key):
    r = subprocess.run(["defaults", "read", domain, key], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def _rss(pattern):
    tot, n = 0, 0
    for ln in sh("ps", "-axo", "rss=,%cpu=,etime=,comm=").splitlines():
        p = ln.split(None, 3)
        if len(p) == 4 and re.search(pattern, p[3]):
            tot += int(p[0]) * 1024; n += 1
    return tot, n


def _cpu(pattern):
    return sum(float(ln.split(None, 1)[0]) for ln in sh("ps", "-axo", "%cpu=,comm=").splitlines()
               if len(ln.split(None, 1)) == 2 and re.search(pattern, ln.split(None, 1)[1]))


def _dns_ms(server=None, names=("apple.com", "github.com", "wikipedia.org", "brave.com", "cloudflare.com")):
    t = []
    for n in names:
        out = sh("dig", *([f"@{server}"] if server else []), "+tries=1", "+time=2", f"{int(time.time() * 1000) % 99999}.{n}",
                 timeout=5)  # random subdomain: never cached, a real recursive lookup
        mt = re.search(r"Query time: (\d+) msec", out)
        if mt:
            t.append(int(mt.group(1)))
    return round(sum(t) / len(t)) if t else None


def optimize(storage_info=None, apply=False, log=print):
    items = []
    add = lambda area, title, measured, status, action="", gain="": items.append(
        dict(area=area, title=title, measured=measured, status=status, action=action, gain=gain))

    # power
    pm = sh("pmset", "-g")
    pv = lambda k: (re.search(rf"^\s*{k}\s+(\S+)", pm, re.M) or [None, None])[1]
    add("Power", "Computer sleep / display sleep / disk sleep",
        f"sleep {pv('sleep')} min, displaysleep {pv('displaysleep')}, disksleep {pv('disksleep')}",
        "ok" if pv("sleep") in ("0", None) or int(pv("sleep") or 0) >= 10 else "needs OK",
        "sudo pmset -a sleep 0  # a desktop: never sleep mid-build (display sleep still saves power)")
    add("Power", "Power Nap (wakes the Mac for background work)", f"powernap {pv('powernap')}",
        "ok" if pv("powernap") in ("0", None) else "needs OK", "sudo pmset -a powernap 0")
    add("Power", "Standby / hibernate", f"standby {pv('standby')}, hibernatemode {pv('hibernatemode')}",
        "ok" if pv("hibernatemode") in ("0", None) else "needs OK",
        "sudo pmset -a hibernatemode 0  # desktop: no RAM image written to disk on sleep (saves 16 GB of writes)")
    add("Power", "High Power mode", "not offered on iMac19,1 (Apple Silicon/MacBook Pro only)", "n/a")

    # startup
    kexts = [f"{mt.group(1)} {mt.group(2)}" for l in sh("kmutil", "showloaded", "--list-only", timeout=60).splitlines()
             for mt in [re.search(r"\s([a-zA-Z][\w.-]+\.[\w.-]+) \(([^)]+)\)", l)] if mt and "com.apple" not in mt.group(1)]
    sysext = [l.strip() for l in sh("systemextensionsctl", "list").splitlines() if "\t" in l and "enabled" in l]
    boot = re.search(r"sec = (\d+)", sh("sysctl", "-n", "kern.boottime"))
    boot_t = int(boot.group(1)) if boot else 0
    add("Startup", "Third-party kernel extensions", f"{len(kexts)}: {', '.join(kexts[:4]) or 'none'}",
        "ok" if not kexts else "info", "kmutil showloaded --list-only | grep -v com.apple")
    add("Startup", "System extensions", f"{len(sysext)} enabled", "ok" if len(sysext) <= 3 else "info",
        "systemextensionsctl list")
    add("Startup", "Uptime", f"booted {time.strftime('%Y-%m-%d %H:%M', time.localtime(boot_t))} "
        f"({(time.time() - boot_t) / 86400:.1f} days)", "info")
    stale = sorted(set(re.findall(r"\b(com\.macpaw\.[\w.]+)", sh("launchctl", "list"))))  # no sfltool: it prompts for admin
    add("Startup", "Login items of apps in the Trash (stale BTM records)",
        f"{len(stale)}: " + ", ".join(stale),
        "ok" if not stale else "needs you",
        "System Settings > General > Login Items & Extensions: turn off the CleanMyMac X items "
        "(they disappear once the Trash is emptied)")

    # background services
    bs = sh("brew", "services", "list") if shutil.which("brew") or os.path.exists("/usr/local/bin/brew") else ""
    started = [l.split()[0] for l in bs.splitlines()[1:] if " started " in l]
    add("Background", "Homebrew services running", ", ".join(started) or "none", "ok" if not started else "info",
        "brew services stop <name>")
    vm, n = _rss(r"com\.apple\.Virtualization\.VirtualMachine")
    add("Background", "Virtual machines", f"{n} running, {human(vm)}" + (" (Claude's sandbox: fine, no cap needed)"
        if n == 1 and vm < 1536 << 20 else ""), "ok" if not n or vm < 1536 << 20 else "needs OK",
        "cap the VM's memory in its app (e.g. 2 GB for a Linux VM; Docker Desktop: Settings > Resources)",
        "each GB capped is a GB the pool keeps")
    nd, n1 = _rss(r"(^|/)node$")
    py, n2 = _rss(r"(^|/)[Pp]ython[0-9.]*$")
    owners = {}
    for ln in sh("ps", "-axo", "comm=").splitlines():  # who runs them: decided by where the program lives
        c = ln.strip()
        if re.search(r"(^|/)(node|[Pp]ython[0-9.]*)$", c):
            o = "MyApp app" if "/MyApp.app/" in c else "MyApp builder" if "/MYAPP/" in c else \
                "Claude" if "Claude" in c or "/.claude/" in c else "ClearX" if "MacMemoryManager" in c else "other"
            owners[o] = owners.get(o, 0) + 1
    add("Background", "node / python processes", f"node {n1} ({human(nd)}), python {n2} ({human(py)}): " +
        ", ".join(f"{k} {v}" for k, v in sorted(owners.items(), key=lambda kv: -kv[1])),
        "info" if n1 + n2 else "ok", "They belong to the apps listed; quit those apps to end them")
    gd, n = _rss(r"Google Drive")
    add("Background", "Google Drive", f"{human(gd)} RAM" if n else "not running", "info" if n else "ok",
        "Google Drive > Settings > Google Drive: Stream files (not Mirror) keeps the disk and RAM lower")

    # Xcode / dev
    dd = _defaults("com.apple.dt.Xcode", "IDECustomDerivedDataLocation")
    jobs = _defaults("com.apple.dt.Xcode", "IDEBuildOperationMaxNumberOfConcurrentCompileTasks")
    idx = _defaults("com.apple.dt.Xcode", "IDEIndexDisable")
    add("Dev", "Xcode/xcodebuild parallel compiles", f"{jobs} (set by ClearX from the pool: 4 healthy, 4 yellow, 2 pressure/hot or while you work; 8 with more RAM)",
        "applied", "", "fewer compiles at once under pressure = no swap thrash")
    add("Dev", "Xcode DerivedData location", dd or "default (~/Library/Developer/Xcode/DerivedData, internal SSD)", "ok")
    add("Dev", "Xcode indexing", "disabled" if idx in ("1", "YES") else "on (Xcode pauses it while building)", "ok")
    cc = shutil.which("ccache") or os.path.exists("/usr/local/bin/ccache")
    add("Dev", "ccache for the MyDAW build", ("installed" if cc else "not set up") + ": skipped on purpose, MyDAW would "
        "have to switch build systems (Xcode to Ninja) and that's not worth the risk now", "ok" if cc else "info",
        "brew install ccache; then in XDAW's build script: cmake -G Ninja -DCMAKE_C_COMPILER_LAUNCHER=ccache "
        "-DCMAKE_CXX_COMPILER_LAUNCHER=ccache (the Xcode generator ignores compiler launchers)",
        "a rebuild after a pull recompiles only what changed: typically minutes become seconds for JUCE")
    try:
        with open(HOME + "/Library/LaunchAgents/com.mydaw.auto-update.plist", "rb") as f:
            iv = plistlib.load(f).get("StartInterval")
    except OSError:
        iv = None
    add("Dev", "MyDAW auto-update interval", (f"checks every {iv // 60} min, " if iv else "") + "builds at most every 15 min (Intel only)",
        "ok", "", "each build is the Mac's biggest memory load; commits that land meanwhile go into the next build")

    # browser
    bdir = HOME + "/Library/Application Support/BraveSoftware/Brave-Browser"
    try:
        with open(bdir + "/Local State") as f:
            ls = json.load(f)
    except (OSError, ValueError):
        ls = {}
    hw = ls.get("hardware_acceleration_mode", {}).get("enabled", True)
    ext = len([d for d in glob.glob(bdir + "/Default/Extensions/*") if os.path.isdir(d) and not d.endswith("Temp")])
    br, n = _rss(r"Brave Browser")
    add("Browser", "Brave Memory Saver (tab discarding)",
        "policy on" if _defaults("com.brave.Browser", "HighEfficiencyModeEnabled") == "1" else "off",
        "applied" if _defaults("com.brave.Browser", "HighEfficiencyModeEnabled") == "1" else "needs OK",
        "defaults write com.brave.Browser HighEfficiencyModeEnabled -bool true")
    add("Browser", "Brave hardware acceleration", "on" if hw else "OFF", "ok" if hw else "needs you",
        "brave://settings/system > Use graphics acceleration")
    add("Browser", "Brave extensions / RAM", f"{ext} extensions, {n} processes, {human(br)}",
        "ok" if ext <= 8 else "info", "brave://extensions: each extension is a process")

    # spotlight
    add("Spotlight", "mds / mds_stores / mdworker CPU now", f"{_cpu(r'mds|mdworker'):.0f}%",
        "ok" if _cpu(r"mds|mdworker") < 20 else "info",
        "System Settings > Spotlight > Search Privacy: add ~/Developer/XDAW/src/build, ~/Developer/XDAW/auto-src/build, "
        "~/Developer/MYAPP/build, node_modules folders")

    # network
    route = sh("route", "-n", "get", "default")
    iface = (re.search(r"interface: (\S+)", route) or [None, "?"])[1]
    ports = sh("networksetup", "-listallhardwareports")
    port = (re.search(rf"Hardware Port: ([^\n]+)\nDevice: {re.escape(iface)}\n", ports) or [None, iface])[1]
    add("Network", "Default route", f"{port} ({iface})", "ok" if "Ethernet" in port else "info",
        "Ethernet is lower latency and doesn't share airtime with the Wi-Fi of your other devices")
    cur, cf = _dns_ms(), _dns_ms("1.1.1.1")
    add("Network", "DNS lookup time (uncached)", f"current {cur} ms, 1.1.1.1 {cf} ms",
        "needs OK" if cur and cf and cf < cur * 0.7 else "ok",
        f"System Settings > Network > {port} > Details > DNS: add 1.1.1.1" if cur and cf and cf < cur * 0.7 else
        "keep current DNS (1.1.1.1 isn't meaningfully faster here)")

    # display / GPU
    dp = sh("system_profiler", "SPDisplaysDataType", timeout=60)
    ui = (re.search(r"UI Looks like: (\d+) x (\d+)", dp) or re.search(r"Resolution: (\d+) x (\d+)", dp))
    if ui:
        w, h = int(ui.group(1)), int(ui.group(2))
        fb = w * 2 * h * 2 * 4
        add("Display", "5K scaling", f"looks like {w}x{h}: renders {w * 2}x{h * 2}, {fb >> 20} MB per frame buffer "
            f"(~{3 * fb >> 20} MB triple-buffered)",
            "ok" if (w, h) == (2560, 1440) else "info",
            "Default (2560x1440) renders at native 5K and is the cheapest sharp mode; 'More space' modes render "
            "larger and are downsampled, costing VRAM and GPU time")
    ws = _cpu(r"WindowServer")
    rt = _defaults("com.apple.universalaccess", "reduceTransparency")
    # rt is None when macOS privacy hides the setting from this process (e.g. run from the ClearX app): unknown, not off
    add("Display", "WindowServer CPU / Reduce transparency",
        f"{ws:.0f}% CPU, reduce transparency " + {"1": "on", "0": "off"}.get(rt, "on (or hidden by macOS privacy)"),
        "needs you" if rt == "0" else "ok", "System Settings > Accessibility > Display > Reduce transparency")

    # thermal
    fan = os.path.isdir("/Applications/Macs Fan Control.app")
    add("Thermal", "Macs Fan Control", "installed" if fan else "not installed", "your call",
        "see the bench: if CPU_Speed_Limit stays 100 during a full build, the stock fan curve is enough")

    # hardware ceiling
    mem = sh("system_profiler", "SPMemoryDataType", timeout=60)
    slots = re.findall(r"BANK \d+/ChannelA-DIMM\d+:|DIMM\d+[^\n]*:", mem)
    sizes = re.findall(r"Size: (\S+ \S+)", mem)
    spd = (re.search(r"Speed: (\S+ MHz)", mem) or [None, "?"])[1]
    vs = sh("vm_stat")
    so = int((re.search(r"Swapouts:\s+(\d+)", vs) or [0, 0])[1]) * 4096
    days = max(0.01, (time.time() - boot_t) / 86400)
    w, r = ssd_write_read()
    swap_w = so
    rate = w / days
    add("Storage", "Drive writes since boot (what wears an SSD; reads don't)",
        f"{human(w)} written in {days * 24:.1f} h, {human(swap_w)} of it swap; {human(r)} read (no wear)",
        "info" if rate < 100 << 30 else "applied",
        "", f"ClearX cuts the writes: 2 compile jobs while you work, apps frozen only while the compressor has room, "
        "Brave disk cache capped at 300 MB. " + (lambda wr_: (
            f"The drive itself reports {wr_['wear_pct']}% of its life used after {human(wr_['lifetime_written'])} written "
            f"(about {human(wr_.get('endurance', 0))} in total); at {human(rate)}/day the rest lasts "
            f"~{(wr_.get('endurance', 0) - wr_['lifetime_written']) / max(rate, 1) / 365:.1f} years of days like this one, "
            "and much longer with normal idle days.") if wr_ else
            "Real wear needs SMART: brew install smartmontools")(ssd_wear()))
    gbs = [int(re.match(r"(\d+)", x).group(1)) for x in sizes if re.match(r"(\d+)", x)]
    add("Hardware", "RAM", f"{sum(gbs)} GB installed ({len(gbs)} × {gbs[0] if gbs else '?'} GB) · {4 - len(gbs)} empty slots, {spd}; "
        f"{human(so)} swapped out since boot ({human(so / days)}/day)", "hardware",
        "32 GB: 2x16 GB DDR4-2666 PC4-21300 260-pin SO-DIMM in the empty slots, or 4x8/4x16 GB (Apple's max is 64 GB); "
        "rear hatch above the power port, no tools beyond a coin",
        f"the measured {human(so / days)}/day of swap-outs fits in RAM instead: builds stop being paused or "
        "throttled for memory, and swap writes to the SSD drop to ~0")
    si = storage_info or {}
    dr = si.get("drive") or drive_info()
    spdt = si.get("speed", {})
    add("Hardware", "Boot drive", f"{dr.get('kind', '?')} {dr.get('model', '')} {dr.get('protocol', '')}, "
        f"write {spdt.get('write_mb_s', '?')} MB/s, read {spdt.get('read_mb_s', '?')} MB/s",
        "ok" if dr.get("kind") == "SSD" else "hardware",
        "" if dr.get("kind") == "SSD" else "external Thunderbolt 3 NVMe boot drive (~$100-150): 5-10x faster swap",
        "" if dr.get("kind") == "SSD" else "swap and builds 5-10x faster")

    if apply:  # the safe user-level items, idempotent
        if _defaults("com.brave.Browser", "HighEfficiencyModeEnabled") != "1":
            subprocess.run(["defaults", "write", "com.brave.Browser", "HighEfficiencyModeEnabled", "-bool", "true"])
            log("MAINT optimize: Brave Memory Saver policy on")
    return dict(items=items, t=time.time())



# ---------- "Needs you": at most a few plain-English items, each with one button ----------
def btm_active(btm, key):
    """True if a login/background item matching key can still run: its record says enabled AND allowed.
    Items you switched off in Login Items read 'disallowed' and stay listed harmlessly until the app is deleted."""
    for rec in btm.split("\n #"):
        if key in rec.lower():
            d = re.search(r"Disposition: \[([^\]]*)\]", rec)
            if d and "enabled" in d.group(1) and "disallowed" not in d.group(1):
                return True
    return False


def needs(maint, st):
    out = []
    add = lambda id_, text, button, kind: out.append(dict(id=id_, text=text, button=button, kind=kind))
    up = (maint.get("updates") or {}).get("macos") or []
    if up:
        main = next((u for u in up if "macOS" in u["title"]), up[0])
        add("updates", f"Install {main['title']}" + (" and restart" if any(u.get("restart") for u in up) else ""),
            "Open Software Update", "open:x-apple.systempreferences:com.apple.Software-Update-Settings.extension")
    pm = sh("pmset", "-g")
    sl = re.search(r"^\s*sleep\s+(\d+)", pm, re.M)
    if sl and 0 < int(sl.group(1)) < 10:
        add("sleep", f"Your Mac goes to sleep after {sl.group(1)} min, which can pause long builds and downloads",
            "Fix", "fix:sleep")
    hm = re.search(r"^\s*hibernatemode\s+(\d+)", pm, re.M)
    if hm and hm.group(1) != "0":
        add("hibernate", "Stop writing a 16 GB copy of memory to your drive every time the Mac sleeps",
            "Fix", "fix:hibernate")
    # no sfltool here: it needs admin rights and prompts for a password. launchctl shows what can actually run.
    if re.search(r"\bcom\.macpaw\.", sh("launchctl", "list")):
        add("cleanmymac", "Turn off the CleanMyMac leftovers in Login Items", "Open Settings",
            "open:x-apple.systempreferences:com.apple.LoginItems-Settings.extension")
    caches = next((c for c in maint.get("junk") or [] if c["id"] == "caches"), None)
    if caches and caches["size"] >= 500 << 20:
        add("caches", f"Free {human(caches['size'])} of old app caches (moved to the Trash)", "Clean", "fix:caches")
    if subprocess.run(["defaults", "read", "com.apple.universalaccess", "reduceTransparency"],
                      capture_output=True, text=True).stdout.strip() == "0":  # unreadable (privacy) is not "off"
        add("transparency", "Turn on Reduce transparency so the screen uses less graphics power", "Open Settings",
            "open:x-apple.systempreferences:com.apple.Accessibility-Settings.extension")
    ui = re.search(r"UI Looks like: (\d+) x (\d+)", sh("system_profiler", "SPDisplaysDataType", timeout=30))
    if ui and (int(ui.group(1)), int(ui.group(2))) != (2560, 1440):
        add("scaling", f"Your screen is set to look like {ui.group(1)} x {ui.group(2)}, which makes the graphics card draw "
            "a bigger picture and shrink it. Default is sharper and lighter", "Open Settings",
            "open:x-apple.systempreferences:com.apple.Displays-Settings.extension")
    d = shutil.disk_usage("/System/Volumes/Data")
    if d.free < 40 << 30:
        add("disk", f"Your drive is getting full ({human(d.free)} free): memory overflow needs room", "See storage",
            "details:storage")
    return out


def fix(what, log):
    """Only on the user's click. Admin changes go through the standard macOS password prompt."""
    if what == "hibernate":
        r = subprocess.run(["osascript", "-e", 'do shell script "/usr/bin/pmset -a hibernatemode 0" '
                            'with prompt "ClearX wants to stop the 16 GB sleep file." with administrator privileges'],
                           capture_output=True, text=True)
        ok = r.returncode == 0
        log(f"MAINT fix hibernate: {'hibernatemode 0' if ok else 'cancelled'}  (undo: sudo pmset -a hibernatemode 3)")
        return ok
    if what == "sleep":  # a desktop: the computer never sleeps on its own (the display still does)
        r = subprocess.run(["osascript", "-e", 'do shell script "/usr/bin/pmset -a sleep 0" with prompt '
                            '"ClearX wants to stop the Mac sleeping by itself (the screen still turns off)." '
                            'with administrator privileges'], capture_output=True, text=True)
        ok = r.returncode == 0
        log(f"MAINT fix sleep: {'sleep 0' if ok else 'cancelled'}  (undo: sudo pmset -a sleep 1)")
        return ok
    if what == "simulators":
        return subprocess.run(["xcrun", "simctl", "delete", "unavailable"], capture_output=True, timeout=120).returncode == 0
    if what == "caches":
        return clean(["caches"], log) >= 0
    if what == "iosdev":  # on the user's click, after a confirm in the app: returns (freed now, moved to Trash)
        before = available()
        sh("xcrun", "simctl", "shutdown", "all", timeout=120)
        sh("xcrun", "simctl", "delete", "all", timeout=600)
        try:  # with no simulators left, every runtime is unused
            for rid in json.loads(sh("xcrun", "simctl", "runtime", "list", "-j", timeout=60) or "{}"):
                sh("xcrun", "simctl", "runtime", "delete", rid, timeout=600)
        except (ValueError, TypeError):
            pass
        trashed = sum(move_to_trash(d, log) for d in glob.glob(HOME + "/Library/Developer/Xcode/iOS DeviceSupport/*"))
        time.sleep(5)  # runtime images unmount and go a moment after the delete
        freed = max(0, available() - before)
        log(f"MAINT fix iosdev: simulators and runtimes deleted ({human(freed)} freed), iOS DeviceSupport "
            f"{human(trashed)} to the Trash")
        return freed, trashed
    return False


# ---------- "Reach N GB free" plan: candidates with sizes and one action each; nothing is deleted here ----------
def storage_plan(maint, goal_gb=250):
    # in Finder's terms, so "250 GB free" here is 250 GB available in Finder: decimal GB, free + purgeable
    free = available()
    goal = goal_gb * 10 ** 9
    items = []
    add = lambda id_, title, size, how, action, note="": items.append(
        dict(id=id_, title=title, size=size, how=how, action=action, note=note))
    for acct in glob.glob(HOME + "/Library/CloudStorage/GoogleDrive-*"):
        # one level inside each shared folder: MyDAW reads "MyDAW Library" straight from Drive (its library roots and
        # the Media screen), so that folder is never suggested; old archives next to it are
        for top in glob.glob(acct + "/.shortcut-targets-by-id/*/*") + glob.glob(acct + "/My Drive"):
            for sub in sorted(glob.glob(top + "/*")) or [top]:
                # never MyDAW Library, anything inside it (the shared folder itself can be MyDAW Library), or a folder
                # that holds it
                if not os.path.isdir(sub) or "MyDAW Library" in sub.split("/") or \
                        glob.glob(glob.escape(sub) + "/MyDAW Library") or glob.glob(glob.escape(sub) + "/*/MyDAW Library"):
                    continue
                sz = du(sub)
                if sz >= 1 << 30:
                    add("drive:" + sub, f"Google Drive “{os.path.basename(sub)}” is downloaded on this Mac", sz,
                        "In Finder, right-click the folder › Remove download. It stays in Google Drive and downloads "
                        "again if opened.", "reveal:" + sub,
                        "MyDAW scans your Drive for songs; this folder stays listed (as cloud files), only its local "
                        "copy goes." if ".shortcut-targets-by-id" in sub else "")
    ios = du(HOME + "/Library/Developer/CoreSimulator/Devices") + du(HOME + "/Library/Developer/Xcode/iOS DeviceSupport")
    rt = re.search(r"Total Disk Images: \d+ \(([\d.]+)G\)", sh("xcrun", "simctl", "runtime", "list", timeout=60))
    ios += int(float(rt.group(1)) * (1 << 30)) if rt else 0
    if ios >= 1 << 30:
        add("iosdev", "iOS simulators and iPhone debug symbols (Xcode)", ios,
            "Only if you no longer test MyApp on the iOS simulator: Xcode › Settings › Platforms › delete old iOS runtimes.",
            "fix:iosdev", "MyApp's iOS work uses these; they download again when needed (slowly).")
    for p, title, how in ((HOME + "/Library/Application Support/Autodesk", "Autodesk app data",
                           "If you no longer use Autodesk apps, uninstall them with Clean up › Uninstall."),
                          (HOME + "/.cache/codex-runtimes", "Codex runtimes cache", "Safe to remove; re-downloaded when used."),
                          (HOME + "/Library/Application Support/Claude", "Claude app data (includes its local VM image)",
                           "Claude › Settings can reset local data; most of it is the sandbox VM image.")):
        if os.path.isdir(p):
            sz = du(p)
            if sz >= 1 << 30:
                add("path:" + p, title, sz, how, "reveal:" + p)
    dups = (maint.get("storage") or {}).get("duplicates")
    if dups:
        add("dups", "Duplicate copies of your own files", sum(g["wasted"] for g in dups),
            "Storage › Duplicate files: keep the oldest, move the others to the Trash.", "details:storage")
    tr = du(TRASH) if os.path.isdir(TRASH) else 0
    if tr:
        add("trash", "Trash", tr, "Finder › Empty Trash (permanent).", "open:" + TRASH)
    items.sort(key=lambda i: -i["size"])
    total = sum(i["size"] for i in items)
    return dict(goal=goal, free=free, gap=max(0, goal - free), candidates=total,
                reachable=free + total >= goal, items=items, t=time.time(), decimal=True)
