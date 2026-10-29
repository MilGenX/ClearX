#!/usr/bin/env python3
"""v3 self-tests with fake processes only, against an isolated state dir and a test config (the live daemon's
state is untouched). Fake compiler = test/bin/clang (the memory hog under a compiler's name).
Fake app = TextEdit (opened fresh by the test, quit at the end).
  python3 test/selftest-v3.py   -> test/selftest-v3.log
"""
import json, os, re, shutil, signal, subprocess, sys, tempfile, time

D = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
TMP = tempfile.mkdtemp(prefix="mg-selftest-")
shutil.copy(os.path.join(D, "state", "state.json"), TMP)
CONF = os.path.join(TMP, "test.conf")
s = open(os.path.join(D, "clearx.conf")).read()
s = re.sub(r"(\[freezer\][^\[]*?)enabled = false", r"\1enabled = true", s)
s = re.sub(r"(\[freezer\][^\[]*?)idle_min = 5", r"\1idle_min = 0", s)
s = re.sub(r"(\[freezer\][^\[]*?)min_mem = 300M", r"\1min_mem = 1M", s)
s = re.sub(r"(\[interactive\][^\[]*?)enabled = false", r"\1enabled = true", s)
s = re.sub(r"(\[reserve\][^\[]*?)enabled = false", r"\1enabled = true", s)
s = s.replace("target_regex =", "target_regex = (test/bin/clang|TextEdit)")
open(CONF, "w").write(s)
os.environ.update(CLEARX_CONF=CONF, CLEARX_STATE=TMP, CLEARX_LOGS=TMP)
sys.path.insert(0, D)
import clearx as mg  # noqa: E402

out = open(os.path.join(D, "test", "selftest-v3.log"), "w")


def say(*a):
    line = " ".join(str(x) for x in a)
    print(line, flush=True)
    out.write(line + "\n"); out.flush()


def stat(pid):
    return subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()


def tick(**patch):
    saved = {k: getattr(mg, k) for k in patch}
    for k, v in patch.items():
        setattr(mg, k, v)
    try:
        mg.tick()
    finally:
        for k, v in saved.items():
            setattr(mg, k, v)
    return mg.load_state()


results = {}
say(f"== clearx v3 self-test {time.strftime('%Y-%m-%d %H:%M:%S')} (isolated state {TMP})")

# --- 1. interactive-aware builds -------------------------------------------------------------------------
say("\n-- 1. interactive-aware builds (fake clang, 300 MB)")
c = subprocess.Popen([os.path.join(D, "test/bin/clang"), "100", "300", "100"])
time.sleep(4)
st = tick(hid_idle_s=lambda: 5)
bg = st.get("bg", {}).get(str(c.pid), {})
say(f"you're active (idle 5 s): clang background QoS = {bool(bg)} ({bg.get('why')}); Xcode jobs for next build = {st.get('xcode_jobs')}")
results["interactive_active"] = bool(bg) and st.get("xcode_jobs", 99) <= 4
st = tick(hid_idle_s=lambda: 300)
say(f"you're away (idle 300 s): clang still backgrounded = {str(c.pid) in st.get('bg', {})}")
results["interactive_idle"] = str(c.pid) not in st.get("bg", {})

# --- 2. RAM reserve -----------------------------------------------------------------------------------------
say("\n-- 2. RAM reserve (target forced above what's ready, so it must escalate)")
open(CONF, "a").close()
s2 = open(CONF).read().replace("target = 10G", "target = 15G")
open(CONF, "w").write(s2)
stages = []
for i in range(4):
    st = tick(hid_idle_s=lambda: 300)
    stages.append(st.get("reserve_stage"))
r = st.get("reserve", {})
say(f"stages over 4 ticks: {stages}; ready {r.get('ready', 0) >> 20} MB, target {r.get('target', 0) >> 20} MB, "
    f"reachable={r.get('reachable')}; Xcode jobs = {st.get('xcode_jobs')}; clang backgrounded = {str(c.pid) in st.get('bg', {})}")
results["reserve_escalates"] = stages[-1] >= 3 and st.get("xcode_jobs") == 2
open(CONF, "w").write(s2.replace("target = 15G", "target = 1G"))
for i in range(5):
    st = tick(hid_idle_s=lambda: 300)
say(f"target lowered to 1 GB: stage back to {st.get('reserve_stage')}")
results["reserve_releases"] = st.get("reserve_stage") == 0
c.send_signal(signal.SIGTERM); c.wait()

# --- 3. App Freezer + instant wake via ClearX.app -------------------------------------------------------
say("\n-- 3. App Freezer (TextEdit as the fake app) + wake on activation by ClearX.app")
open(CONF, "w").write(s2.replace("target = 15G", "target = 15G"))  # reserve below target -> freezer active at green
was_running = any(a["name"] == "TextEdit" for a in mg.gui_apps())
subprocess.run(["open", "-g", "-a", "TextEdit"]); time.sleep(4)
te = next((a for a in mg.gui_apps() if a["name"] == "TextEdit"), None)
st = mg.load_state(); st.setdefault("front_seen", {})["TextEdit"] = time.time() - 3600; mg.save_state(st)
for i in range(3):
    st = tick(hid_idle_s=lambda: 300)
frozen = [k for k, i in st.get("paused", {}).items() if i.get("kind") == "freeze" and i.get("app") == "TextEdit"]
say(f"TextEdit pid {te and te['pid']}: frozen processes = {len(frozen)}, ps stat = {stat(te['pid']) if te else '?'}")
results["freeze"] = bool(frozen) and "T" in stat(te["pid"])
t0 = time.time()
subprocess.run(["open", "-a", "TextEdit"])
while "T" in stat(te["pid"]) and time.time() - t0 < 5:
    time.sleep(0.005)
woke = time.time() - t0
say(f"activated TextEdit: running again after {woke * 1000:.0f} ms (open -a + activation + ClearX.app SIGCONT); "
    f"stat = {stat(te['pid'])}")
wl = open(os.path.join(D, "logs", "wake.log")).read().strip().splitlines()[-1:] if os.path.exists(os.path.join(D, "logs", "wake.log")) else []
say(f"ClearX.app wake.log: {wl[0] if wl else '(none)'}")
results["wake"] = "T" not in stat(te["pid"])
st = tick(hid_idle_s=lambda: 300)
say(f"daemon next tick: TextEdit still listed as frozen = {any(i.get('app') == 'TextEdit' and i.get('kind') == 'freeze' for i in st.get('paused', {}).values())}")

# safety nets: audio/assertion and front app never frozen; ClearX.app gone -> wake all
say(f"front app this tick: {st.get('foreground')} (never frozen); pids holding power assertions: {len(mg.assertion_pids())}")
st = mg.load_state(); st["front_seen"]["TextEdit"] = time.time() - 3600; mg.save_state(st)
subprocess.run(["osascript", "-e", 'tell application "Claude" to activate'], capture_output=True); time.sleep(1)
tick(hid_idle_s=lambda: 300)
refrozen = "T" in stat(te["pid"])
real = mg.proc_list
mg_procs = lambda: {k: v for k, v in real().items() if v["name"] != "ClearX"}
st = tick(hid_idle_s=lambda: 300, proc_list=mg_procs)
say(f"re-frozen = {refrozen}; with ClearX.app 'not running' the daemon woke it: stat = {stat(te['pid'])}")
results["watchdog"] = refrozen and "T" not in stat(te["pid"])
if te and not was_running:
    subprocess.run(["osascript", "-e", 'tell application "TextEdit" to quit'], capture_output=True)

# --- 4. storage plan ---------------------------------------------------------------------------------------
say("\n-- 4. Reach 250 GB plan")
import maint  # noqa: E402
p = maint.storage_plan(mg.load_maint(), 250)
say(f"free {p['free'] >> 30} GB, gap {p['gap'] >> 30} GB, candidates {p['candidates'] >> 30} GB, reachable={p['reachable']}")
for i in p["items"]:
    say(f"  {i['size'] >> 30:>4} GB  {i['title']}")
results["plan"] = bool(p["items"])

say("\n== results: " + json.dumps(results))
say("ALL PASS" if all(results.values()) else "SOME FAILED")
shutil.rmtree(TMP, ignore_errors=True)
