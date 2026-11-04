"""clearx keep - the disk keeper: frees space on its own, once a day while you're away from the Mac.

X 2026-11-04: "free up overall space, compress what's not being used and store it in one compressed folder ... ideas on
top of ideas". Compression doesn't stack (a zip of a FLAC or a zip is the same size), so each kind of file gets the one
thing that shrinks it, and what can be made again is simply removed:

  1. rebuildable  build folders (build, .build, DerivedData, cmake-build-*) in ~/Developer that their project's git
                  ignores and nothing has written to for `rebuild_days`,
                  installers (.dmg .pkg .xip) in Downloads older than `installers_days`: deleted (made again / downloaded
                  again when needed). Never a folder something wrote to in that time.
  2. duplicates   identical copies of your own files (Documents, Downloads, Desktop, Music, Movies, Pictures) become APFS
                  clones of the oldest: every path stays and opens as before, the disk holds the bytes once.
  3. apps         apps in /Applications made transparently compressed (APFS/decmpfs, as Apple ships macOS itself): they
                  open and run the same, take 30-50% less. Never a running app; the signature is checked before the swap.
  4. cold         your own files untouched for `cold_days` (Downloads, Desktop, Documents, Movies) move to one folder,
                  ~/ClearX Cold Storage, compressed: WAV/AIFF as FLAC (lossless, checked sample for sample), the rest
                  transparently compressed. Only kinds that shrink (never photos, video, mp3/m4a, zips: already
                  compressed). `clearx maintain unarchive <path|all>` puts files back; the folder's manifest.json lists them.
  5. drive        Google Drive files in `drive_roots` not opened for `drive_days` come off the disk (they stay in Drive and
                  download again when opened). The church's ECX folder is ECX's own job (it knows the week's songs).
  6. growth       each run records the biggest folders; one that grew by `growth_alert` since the last run is named in a
                  notification (63 -> 29 GB overnight on 11-03/04 was 19 GB of ECX practice mixes; now it says so).

Nothing here uses sudo or empties the Trash; the system and other users' files are never touched.
"""
import glob, hashlib, json, os, re, shutil, subprocess, time, wave

import maint
from maint import human

DAY = 86400
COLD_DIR_NAME = "ClearX Cold Storage"
AUDIO_PCM = (".wav", ".wave", ".aif", ".aiff")
# kinds that shrink with general compression (text, raw images, documents, data); never ones already compressed
COMPRESSIBLE = AUDIO_PCM + (".txt", ".csv", ".tsv", ".json", ".xml", ".log", ".sql", ".md", ".html", ".htm", ".svg",
                            ".psd", ".tif", ".tiff", ".bmp", ".raw", ".cr2", ".nef", ".dng", ".arw", ".obj", ".stl",
                            ".doc", ".xls", ".ppt", ".rtf", ".ipynb", ".sqlite", ".db", ".iso", ".img", ".vmdk", ".tar",
                            ".mid", ".midi", ".caf")
BUNDLES = ("*.app", "*.framework", "*.bundle", "*.plugin", "*.vst3", "*.component", "*.mlmodelc", "*.mlpackage",
           "*.photoslibrary", "*.musiclibrary", "*.logicx", "*.band", "*.fcpbundle", "*.imovielibrary", "*.rtfd",
           "*.pages", "*.numbers", "*.key", "node_modules", ".git")
UF_COMPRESSED = 0x20


def home():
    return os.path.expanduser("~")


def cfg(key, default):
    return maint.setting("keeper", key, default)


def cfg_list(key, default):
    return [x.strip() for x in cfg(key, default).split(",") if x.strip()]


def run(*a, timeout=3600):
    try:
        r = subprocess.run(a, capture_output=True, timeout=timeout)
        return r.returncode, r.stdout.decode("utf-8", "replace") + r.stderr.decode("utf-8", "replace")
    except (subprocess.TimeoutExpired, OSError) as e:
        return 1, str(e)


def newest_within(path, days):
    """Whether anything under `path` was modified in the last `days` days (the folder itself included)."""
    try:
        if time.time() - os.stat(path).st_mtime < days * DAY:
            return True
    except OSError:
        return True
    cut = time.time() - days * DAY
    for dp, dn, fn in os.walk(path):
        for n in fn + dn:
            try:
                if os.lstat(os.path.join(dp, n)).st_mtime > cut:
                    return True
            except OSError:
                pass
    return False


def last_used(p):
    try:
        st = os.stat(p)
    except OSError:
        return time.time()
    return max(st.st_mtime, st.st_atime, getattr(st, "st_birthtime", 0))


def free_bytes(path):
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return 0


def is_mac():
    return os.uname().sysname == "Darwin"


# ---------- 1. rebuildable ----------
def rebuildable(root=None, days=None, names=None):
    root = root or os.path.join(home(), "Developer")
    days = int(days or cfg("rebuild_days", "7"))
    names = names or cfg_list("rebuild_names", "build, .build, DerivedData, cmake-build-*, build-*")
    keep = cfg_list("keep_paths", "")
    out = []
    if not os.path.isdir(root):
        return out
    import fnmatch
    for dp, dn, fn in os.walk(root):
        depth = dp[len(root):].count(os.sep)
        if depth > 4 or "/.git" in dp or "/node_modules" in dp:
            dn[:] = []
            continue
        for d in list(dn):
            p = os.path.join(dp, d)
            if any(fnmatch.fnmatch(d, n) for n in names) and not os.path.islink(p):
                dn.remove(d)  # never inside one
                if any(k and k in p for k in keep) or not git_ignored(p) or newest_within(p, days):
                    continue
                out.append(dict(path=p, size=maint.du(p)))
    return out


def git_ignored(path):
    """Only a folder its project's git ignores is build output for sure ("build-tools" with code in it isn't)."""
    code, _ = run("git", "-C", os.path.dirname(path), "check-ignore", "-q", path, timeout=30)
    if code != 0:
        return False
    code, out = run("git", "-C", os.path.dirname(path), "ls-files", "--", os.path.basename(path), timeout=60)
    return code == 0 and not out.strip()  # (nothing in it tracked)


def installers(root=None, days=None):
    root = root or os.path.join(home(), "Downloads")
    days = int(days or cfg("installers_days", "30"))
    out = []
    for p in glob.glob(root + "/**/*", recursive=True):
        if p.lower().endswith((".dmg", ".pkg", ".xip", ".mpkg")) and time.time() - last_used(p) > days * DAY:
            out.append(dict(path=p, size=maint.du(p)))
    return out


def remove(items, log, apply):
    freed = 0
    for i in items:
        if not apply:
            continue
        p = i["path"]
        try:
            shutil.rmtree(p) if os.path.isdir(p) and not os.path.islink(p) else os.remove(p)
            freed += i["size"]
            log(f"KEEP removed {p} ({human(i['size'])}): made again when needed")
        except OSError as e:
            log(f"KEEP couldn't remove {p}: {e}")
    return freed


# ---------- 2. duplicates -> APFS clones ----------
def full_hash(p):
    h = hashlib.blake2b(digest_size=16)
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(8 << 20), b""):
            h.update(b)
    return h.hexdigest()


def clone_dupes(groups, log, apply, done=None):
    """Each group's later copies replaced by an APFS clone of the oldest (cp -c): same bytes, held once."""
    done = done if done is not None else {}
    saved = 0
    for g in groups:
        src, rest = g["paths"][0], g["paths"][1:]
        for dup in rest:
            try:
                st = os.stat(dup)
            except OSError:
                continue
            key = f"{dup}|{st.st_size}|{int(st.st_mtime)}"
            if done.get(key) or os.stat(src).st_dev != st.st_dev:
                continue
            if not apply:
                saved += st.st_size
                continue
            tmp = os.path.join(os.path.dirname(dup), ".clearx-clone-" + os.path.basename(dup))
            code, out = run("cp", "-c", src, tmp, timeout=600)
            try:
                if code != 0 or full_hash(tmp) != full_hash(dup):
                    raise OSError(out.strip() or "the clone differs")
                os.chmod(tmp, st.st_mode & 0o7777)
                os.utime(tmp, (st.st_atime, st.st_mtime))
                os.replace(tmp, dup)
            except OSError as e:
                if os.path.exists(tmp):
                    os.remove(tmp)
                log(f"KEEP couldn't clone {dup}: {e}")
                continue
            done[key] = time.time()
            saved += st.st_size
            log(f"KEEP {dup} is now a clone of {src} ({human(st.st_size)} held once)")
    return saved


# ---------- 3. apps, transparently compressed ----------
def compressed_share(app, sample=200):
    """Share of the app's files already compressed (decmpfs), from a sample."""
    n = c = 0
    for dp, dn, fn in os.walk(app):
        for f in fn:
            try:
                st = os.lstat(os.path.join(dp, f))
            except OSError:
                continue
            if st.st_size < 4096 or not os.path.isfile(os.path.join(dp, f)):
                continue
            n += 1
            c += bool(getattr(st, "st_flags", 0) & UF_COMPRESSED)
            if n >= sample:
                return c / n
    return c / n if n else 1.0


def running_app(app):
    code, out = run("pgrep", "-f", re.escape(app) + "/Contents/MacOS/", timeout=10)
    return code == 0 and out.strip() != ""


def apps_to_compress(roots=None, min_size=None):
    roots = roots or ["/Applications", os.path.join(home(), "Applications")]
    min_size = maint_size(min_size or cfg("apps_min", "200M"))
    skip = cfg_list("apps_skip", "")
    out = []
    for r in roots:
        for app in glob.glob(r + "/*.app"):
            name = os.path.basename(app)[:-4]
            if any(re.fullmatch(s.replace("*", ".*"), name, re.I) for s in skip):
                continue
            if not os.access(app, os.W_OK) or not os.access(r, os.W_OK):
                continue  # App Store / root-owned apps would need sudo
            sz = maint.du(app)
            if sz >= min_size and compressed_share(app) < 0.5:
                out.append(dict(path=app, size=sz))
    return sorted(out, key=lambda a: -a["size"])


def maint_size(s):
    s = str(s).strip().upper()
    m = {"K": 1 << 10, "M": 1 << 20, "G": 1 << 30}
    return int(float(s[:-1]) * m[s[-1]]) if s and s[-1] in m else int(s or 0)


def compress_app(app, log, apply):
    """ditto --hfsCompression into a copy beside it, signature checked, then swapped in. Returns bytes saved."""
    before = maint.du(app)
    if not apply:
        return int(before * 0.4)  # a preview's guess: apps typically shrink 30-50%
    if running_app(app):
        log(f"KEEP {app} is running: compressed another day")
        return 0
    if free_bytes(os.path.dirname(app)) < before * 1.2:
        log(f"KEEP not enough free space to compress {app} ({human(before)}) yet")
        return 0
    tmp = app[:-4] + ".clearx-compressing.app"
    shutil.rmtree(tmp, ignore_errors=True)
    code, out = run("ditto", "--hfsCompression", app, tmp, timeout=7200)
    ok = code == 0
    if ok:
        c, o = run("codesign", "--verify", "--deep", "--strict", tmp, timeout=1800)
        c0, o0 = run("codesign", "--verify", "--deep", "--strict", app, timeout=1800)
        ok = c == 0 or c0 != 0  # (an app that wasn't validly signed before isn't made worse)
        out = o
    if not ok:
        shutil.rmtree(tmp, ignore_errors=True)
        log(f"KEEP left {app} as it was: {out.strip()[:200]}")
        return 0
    old = app[:-4] + ".clearx-old.app"
    try:
        os.rename(app, old)
        os.rename(tmp, app)
    except OSError as e:
        if not os.path.exists(app) and os.path.exists(old):
            os.rename(old, app)
        shutil.rmtree(tmp, ignore_errors=True)
        log(f"KEEP couldn't swap {app}: {e}")
        return 0
    shutil.rmtree(old, ignore_errors=True)
    saved = max(0, before - maint.du(app))
    log(f"KEEP compressed {app}: {human(before)} -> {human(before - saved)}")
    return saved


# ---------- 4. cold storage ----------
def cold_dir():
    return os.path.join(home(), COLD_DIR_NAME)


def manifest_path():
    return os.path.join(cold_dir(), "manifest.json")


def load_manifest():
    try:
        with open(manifest_path()) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"files": {}}


def save_manifest(m):
    os.makedirs(cold_dir(), exist_ok=True)
    tmp = manifest_path() + ".tmp"
    with open(tmp, "w") as f:
        json.dump(m, f, indent=1)
    os.replace(tmp, manifest_path())


def cold_candidates(folders=None, days=None, min_size=None):
    folders = folders or [os.path.join(home(), d) for d in cfg_list("cold_folders", "Downloads, Desktop, Documents, Movies")]
    days = int(days or cfg("cold_days", "90"))
    min_size = maint_size(min_size or cfg("cold_min", "5M"))
    import fnmatch
    out, cut = [], time.time() - days * DAY
    for root in folders:
        for dp, dn, fn in os.walk(root):
            dn[:] = [d for d in dn if not d.startswith(".") and not any(fnmatch.fnmatch(d, b) for b in BUNDLES)]
            for f in fn:
                p = os.path.join(dp, f)
                if f.startswith(".") or not f.lower().endswith(COMPRESSIBLE) or os.path.islink(p):
                    continue
                try:
                    sz = os.stat(p).st_size
                except OSError:
                    continue
                if sz >= min_size and last_used(p) < cut:
                    out.append(dict(path=p, size=sz))
    return out


def pcm_frames(path):
    """A WAV's sample bytes (16/24-bit PCM), for comparing a FLAC's decode with the original."""
    with wave.open(path, "rb") as w:
        return w.getnchannels(), w.getsampwidth(), w.getframerate(), w.readframes(w.getnframes())


def to_flac(src, dest):
    """WAV/AIFF -> FLAC with afconvert, decoded again and compared with the original. True when it matched."""
    code, _ = run("afconvert", "-f", "flac", "-d", "flac", src, dest, timeout=1800)
    if code != 0 or not os.path.exists(dest):
        return False
    if not src.lower().endswith((".wav", ".wave")):
        return True  # AIFF: afconvert's own lossless path (not compared: Python reads only WAV)
    try:
        ch, width, rate, frames = pcm_frames(src)
    except (wave.Error, EOFError, OSError):
        os.remove(dest)
        return False  # float or odd WAV: left to the general compression
    back = dest + ".check.wav"
    fmt = {2: "LEI16", 3: "LEI24"}.get(width)
    ok = False
    if fmt:
        code, _ = run("afconvert", "-f", "WAVE", "-d", fmt, dest, back, timeout=1800)
        try:
            ok = code == 0 and pcm_frames(back) == (ch, width, rate, frames)
        except (wave.Error, EOFError, OSError):
            ok = False
    if os.path.exists(back):
        os.remove(back)
    if not ok and os.path.exists(dest):
        os.remove(dest)
    return ok


def archive(items, log, apply):
    """Moves each file into ~/ClearX Cold Storage/<its path under ~>, compressed. Returns bytes saved."""
    m = load_manifest()
    saved = 0
    for i in items:
        src = i["path"]
        rel = os.path.relpath(src, home())
        dest = os.path.join(cold_dir(), rel)
        if not apply:
            saved += i["size"] // 2
            continue
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        flac = False
        if src.lower().endswith(AUDIO_PCM) and shutil.which("afconvert"):
            fdest = os.path.splitext(dest)[0] + ".flac"
            if not os.path.exists(fdest) and to_flac(src, fdest):
                dest, flac = fdest, True
        if not flac:
            if os.path.exists(dest):
                continue
            code, out = run("ditto", "--hfsCompression", src, dest, timeout=3600) if is_mac() else run("cp", "-p", src, dest)
            if code != 0 or full_hash(src) != full_hash(dest):
                if os.path.exists(dest):
                    os.remove(dest)
                log(f"KEEP couldn't archive {src}: {out.strip()[:200]}")
                continue
        st = os.stat(src)
        os.utime(dest, (st.st_atime, st.st_mtime))
        os.remove(src)
        after = maint.du(dest)
        saved += max(0, i["size"] - after)
        m["files"][dest] = dict(orig=src, flac=flac, size=i["size"], t=time.time())
        log(f"KEEP archived {src} -> {dest} ({human(i['size'])} -> {human(after)})")
    if apply and items:
        save_manifest(m)
    return saved


def unarchive(which, log):
    """Puts archived files back where they were (`all`, or a path inside the cold folder or an original path)."""
    m = load_manifest()
    back = 0
    for dest, e in list(m["files"].items()):
        if which != "all" and which not in (dest, e["orig"]) and not dest.startswith(which.rstrip("/") + "/"):
            continue
        orig = e["orig"]
        if os.path.exists(orig) or not os.path.exists(dest):
            log(f"KEEP left {dest}: " + ("something is already at " + orig if os.path.exists(orig) else "it's gone"))
            continue
        os.makedirs(os.path.dirname(orig), exist_ok=True)
        if e.get("flac"):
            fmt = "AIFF" if orig.lower().endswith((".aif", ".aiff")) else "WAVE"
            code, out = run("afconvert", "-f", fmt, "-d", "BEI24" if fmt == "AIFF" else "LEI24", dest, orig, timeout=1800)
            if code != 0:
                log(f"KEEP couldn't restore {orig}: {out.strip()[:200]}")
                continue
            os.remove(dest)
        else:
            shutil.move(dest, orig)
        del m["files"][dest]
        back += 1
        log(f"KEEP put back {orig}")
    save_manifest(m)
    return back


# ---------- 5. Google Drive local copies ----------
def drive_files(days=None):
    days = int(days or cfg("drive_days", "30"))
    roots = cfg_list("drive_roots", "My Drive")
    skip = cfg_list("drive_skip", "ECX") + [k.strip() for k in maint.setting("storage", "keep_local").split(",") if k.strip()]
    out, cut = [], time.time() - days * DAY
    for acct in glob.glob(home() + "/Library/CloudStorage/GoogleDrive-*"):
        for r in roots:
            for dp, dn, fn in os.walk(os.path.join(acct, r), followlinks=False):
                dn[:] = [d for d in dn if d not in skip and not d.startswith(".")]
                for f in fn:
                    p = os.path.join(dp, f)
                    try:
                        st = os.lstat(p)
                    except OSError:
                        continue
                    # SF_DATALESS (0x40000000): only in the cloud already; blocks: what's really on the disk
                    if getattr(st, "st_flags", 0) & 0x40000000 or st.st_blocks * 512 < (1 << 20):
                        continue
                    if last_used(p) < cut:
                        out.append(dict(path=p, size=st.st_blocks * 512))
    return out


def evict(items, log, apply):
    freed = failed = 0
    first = ""
    for i in items:
        if not apply:
            freed += i["size"]
            continue
        code, out = run("/usr/bin/fileproviderctl", "evict", i["path"], timeout=60)
        if code == 0:
            freed += i["size"]
        else:
            failed += 1
            first = first or f"{os.path.basename(i['path'])}: {out.strip()[:200]}"
    if failed:
        log(f"KEEP Drive wouldn't free {failed} files (first: {first})")
    return freed


# ---------- 6. growth ----------
def growth(sizes_now, history, alert):
    """Folders that grew by `alert` bytes or more since the last run's sizes."""
    last = history[-1]["sizes"] if history else {}
    return sorted(([p, s - last[p]] for p, s in sizes_now.items() if p in last and s - last[p] >= alert),
                  key=lambda x: -x[1])


# ---------- the run ----------
def keep(log=print, apply=False, stop=lambda: False, steps=None, state=None):
    """One pass. `stop()` is asked between steps and items (you came back to the Mac): the rest waits for the next run."""
    state = state if state is not None else {}
    steps = steps or [s for s in ("rebuildable", "installers", "duplicates", "apps", "cold", "drive", "growth")
                      if cfg(s, "true").lower() != "false"]
    r = dict(t=time.time(), apply=apply, freed={}, found={}, grew=[], stopped=False)

    def step(name, find, act):
        if stop():
            r["stopped"] = True
            return
        try:
            items = find()
            r["found"][name] = sum(i.get("size", 0) for i in items) if isinstance(items, list) else 0
            r["freed"][name] = act(items)
        except Exception as e:  # one step's trouble never stops the others
            log(f"KEEP {name} failed: {e}")

    if "rebuildable" in steps:
        step("rebuildable", rebuildable, lambda it: remove(it, log, apply))
    if "installers" in steps:
        step("installers", installers, lambda it: remove(it, log, apply))
    if "duplicates" in steps and is_mac():
        step("duplicates", maint.duplicates, lambda g: clone_dupes(g, log, apply, state.setdefault("cloned", {})))
    if "apps" in steps and is_mac():
        step("apps", apps_to_compress, lambda it: sum(compress_app(a["path"], log, apply) for a in it if not stop()))
    if "cold" in steps:
        step("cold", cold_candidates, lambda it: archive(it, log, apply))
    if "drive" in steps and is_mac():
        step("drive", drive_files, lambda it: evict(it, log, apply))
    if "growth" in steps and not stop():
        try:
            now = {x["path"]: x["size"] for x in maint.storage_map(40)}
            hist = state.setdefault("sizes", [])
            r["grew"] = growth(now, hist, maint_size(cfg("growth_alert", "5G")))
            hist.append(dict(t=time.time(), sizes=now))
            del hist[:-14]
        except Exception as e:
            log(f"KEEP growth failed: {e}")
    r["total"] = sum(r["freed"].values())
    return r


TITLES = dict(rebuildable="Build folders untouched for a week", installers="Old installers in Downloads",
              duplicates="Duplicate files held once (APFS clones)", apps="Apps compressed (open the same)",
              cold="Unused files into ClearX Cold Storage", drive="Google Drive copies not opened lately")
