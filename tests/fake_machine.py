"""A fake Mac for tick tests: no real processes, signals, commands or clocks. FakeMachine.install(cx) points a loaded
clearx module at it; every signal and command ClearX would send lands in `calls`, every log line in `logs`."""
import copy, json, os, signal, types

GB = 1 << 30
MB = 1 << 20
LIVE_CONF = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "config", "clearx.conf")


def proc(pid, name, mem=100 * MB, ppid=1, path=None, cpu=1.0, stat="S", nice=0):
    return dict(pid=pid, ppid=ppid, name=name, path=path or f"/Applications/{name}", stat=stat, nice=nice, cpu=cpu,
                rss=mem, mem=mem)


class FakeMachine:
    def __init__(self, procs, front=None, apps=(), conf=LIVE_CONF):
        self.clock = 1000000.0
        self.procs = {p["pid"]: p for p in procs}
        self.front = front
        self.apps = list(apps)
        self.conf = conf
        self.mem = dict(pressure=1, swap=8 * GB, free=GB, avail=6 * GB, app=4 * GB, wired=3 * GB, compressed=GB,
                        cached=4 * GB, swapouts=0)
        self.speed = 100
        self.load = [2.0, 2.0, 2.0]
        self.idle = 300.0
        self.spill = 0
        self.state = {}
        self.logs, self.calls, self.notes = [], [], []

    def swap_out(self, mb):
        self.mem["swapouts"] += int(mb * MB / 4096)

    def _kill(self, pid, sig):
        self.calls.append(("kill", pid, int(sig)))
        if pid in self.procs:
            self.procs[pid]["stat"] = "T" if int(sig) == int(signal.SIGSTOP) else "S"

    def _run(self, cmd, **k):
        self.calls.append(("run", tuple(cmd)))
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    def _popen(self, cmd, **k):
        self.calls.append(("popen", tuple(cmd)))
        return types.SimpleNamespace(pid=777777)

    def install(self, cx):
        fake_time = types.SimpleNamespace(time=lambda: self.clock, sleep=lambda s: None, strftime=lambda *a: "T")
        fake_os = types.SimpleNamespace(**{k: getattr(os, k) for k in dir(os) if not k.startswith("__")})
        fake_os.kill = self._kill
        fake_os.getpid, fake_os.getppid = (lambda: 999999), (lambda: 999998)
        fake_sub = types.SimpleNamespace(run=self._run, Popen=self._popen, DEVNULL=-3, STDOUT=-2)
        cx.time, cx.os, cx.subprocess = fake_time, fake_os, fake_sub
        cx.CONF = self.conf
        cx.LOCAL_CONF = os.path.join(os.path.dirname(os.path.realpath(__file__)), "local.conf")
        cx.OFF_FILE = "/nonexistent/clearx-off"
        cx.log = self.logs.append
        cx.notify = lambda title, msg: self.notes.append(title)
        cx.load_state = lambda: json.loads(json.dumps(self.state))
        cx.save_state = lambda st: setattr(self, "state", json.loads(json.dumps(st)))
        cx.sh = lambda *a, **k: str(16 * GB) if a[-1] == "hw.memsize" else ""
        cx.metrics = lambda: dict(self.mem)
        cx.gpu_full = lambda: dict(used=GB, sys=self.spill, util=5)
        cx.disk_probe = lambda: (99 * GB, 1.0)
        cx.thermal = lambda: (self.speed, 100)
        cx.loadavg = lambda: list(self.load)
        cx.proc_list = lambda: copy.deepcopy(self.procs)
        cx.add_footprint = lambda procs: None
        cx.front_pid = lambda: self.front
        cx.gui_apps = lambda: copy.deepcopy(self.apps)
        cx.cmdline = lambda pid: self.procs.get(pid, {}).get("path", "")
        cx.start_time = lambda pid: "start"
        started = self.clock
        cx.start_epoch = lambda pid: started + pid  # lower pids started earlier
        cx.hid_idle_s = lambda: self.idle
        cx.assertion_pids = lambda: set()
        cx.net_active = lambda st, now, **k: set()
        cx.track_agents = cx.track_ssd = lambda *a: None
        cx.record_health = lambda st: (100, [])
        cx.ACTIVATIONS = "/nonexistent/clearx-activations"
        return self

    def tick(self, cx, seconds=10, **k):
        self.clock += seconds
        cx.tick(**k)
        return self.state
