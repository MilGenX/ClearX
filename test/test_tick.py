#!/usr/bin/env python3
"""What a whole tick does, on a fake Mac (test/fake_machine.py): no real processes, signals or commands.
  python3 -m unittest discover -s test
"""
import os, signal, sys, tempfile, unittest

D = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
TMP = tempfile.mkdtemp(prefix="clearx-unit-")
os.environ.update(CLEARX_STATE=TMP, CLEARX_LOGS=TMP)
sys.path.insert(0, D)
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import clearx as cx  # noqa: E402
from fake_machine import FakeMachine, proc, GB, MB  # noqa: E402

FRONT, HOG, SYSTEM, CLAUDE, SPINNER = 10, 20, 30, 40, 50
STOP, CONT = int(signal.SIGSTOP), int(signal.SIGCONT)


def machine():
    return FakeMachine([proc(FRONT, "Pages", 900 * MB), proc(HOG, "hog", 3000 * MB, path="/opt/hog"),
                        proc(SYSTEM, "someservice", 900 * MB, path="/System/Library/someservice"),
                        proc(CLAUDE, "Claude", 900 * MB), proc(SPINNER, "spinner", 100 * MB, path="/opt/spinner")],
                       front=FRONT, apps=[dict(name="Pages", pid=FRONT)]).install(cx)


class Tick(unittest.TestCase):
    def signals(self, fm, sig):
        return {c[1] for c in fm.calls if c[0] == "kill" and c[2] == sig}

    def test_swap_burst_with_stale_swap_pauses_nothing(self):
        fm = machine()
        fm.tick(cx)
        fm.swap_out(334)
        st = fm.tick(cx)
        self.assertEqual(st["level"], "green")
        self.assertEqual(self.signals(fm, STOP), set())

    def test_critical_pressure_pauses_only_background_hogs(self):
        fm = machine()
        fm.tick(cx)
        fm.mem.update(pressure=4, avail=GB)
        st = fm.tick(cx)
        self.assertEqual(st["level"], "red")
        self.assertIn(HOG, self.signals(fm, STOP))
        self.assertTrue(self.signals(fm, STOP).isdisjoint({FRONT, SYSTEM, CLAUDE}))

    def test_paused_hog_resumes_after_steady_green(self):
        fm = machine()
        fm.tick(cx)
        fm.mem.update(pressure=4, avail=GB)
        fm.tick(cx)
        fm.mem.update(pressure=1, avail=6 * GB)
        for _ in range(12):
            fm.tick(cx)
        self.assertIn(HOG, self.signals(fm, CONT))
        self.assertEqual(fm.state["paused"], {})

    def orange(self, fm):
        fm.swap_out(2000)  # 2 GB in one tick: ~33 MB/s over the minute, with under 4 GB available
        fm.mem.update(avail=3 * GB)
        return fm.tick(cx)

    def test_orange_pauses_the_largest_background_job_while_you_work(self):
        fm = machine()
        fm.idle = 5
        fm.tick(cx)
        st = self.orange(fm)
        self.assertEqual(st["level"], "orange")
        self.assertIn(HOG, self.signals(fm, STOP))

    def test_orange_leaves_the_background_job_running_while_you_are_away(self):
        fm = machine()
        fm.tick(cx)
        st = self.orange(fm)
        self.assertEqual(st["level"], "orange")
        self.assertNotIn(HOG, self.signals(fm, STOP))
        self.assertIn(str(HOG), st.get("bg", {}))  # still lowered: its paging waits behind everything else

    def test_job_paused_while_you_worked_resumes_once_you_are_away(self):
        fm = machine()
        fm.idle = 5
        fm.tick(cx)
        self.orange(fm)
        fm.idle = 300
        st = self.orange(fm)
        self.assertIn(HOG, self.signals(fm, CONT))
        self.assertNotIn(str(HOG), st["paused"])

    def jobs(self, fm):
        return [c[1][-1] for c in fm.calls if c[0] == "run" and "IDEBuildOperationMaxNumberOfConcurrentCompileTasks" in c[1]][-1]

    def test_next_build_gets_every_core_when_memory_is_fine(self):
        fm = machine()
        fm.tick(cx)
        self.assertEqual(self.jobs(fm), str(os.cpu_count()))

    def test_heat_caps_builds_only_while_you_are_here(self):
        fm = machine()
        fm.speed = 76
        fm.idle = 5
        fm.tick(cx)
        self.assertEqual(self.jobs(fm), "2")
        fm = machine()
        fm.speed = 76
        fm.tick(cx)
        self.assertEqual(self.jobs(fm), str(os.cpu_count()))

    def test_open_daw_lowers_nothing_while_the_mac_is_fine(self):
        fm = machine()
        fm.procs[60] = proc(60, "Logic Pro", 500 * MB)
        fm.procs[70] = proc(70, "Brave Browser", 400 * MB)
        st = fm.tick(cx)
        self.assertEqual(st["mode"], "studio")
        self.assertNotIn("70", st.get("bg", {}))
        fm.speed = 76  # macOS lowering the CPU speed: now the DAW's competition steps back
        st = fm.tick(cx)
        self.assertIn("70", st.get("bg", {}))
        fm.speed = 100  # and back to normal once the Mac has been fine for a while
        for _ in range(10):
            st = fm.tick(cx)
        self.assertNotIn("70", st.get("bg", {}))

    def test_level_steps_down_one_at_a_time(self):
        fm = machine()
        fm.tick(cx)
        fm.mem.update(pressure=4, avail=GB)
        fm.tick(cx)
        fm.mem.update(pressure=1, avail=6 * GB)
        self.assertEqual([fm.tick(cx)["level"] for _ in range(3)], ["orange", "yellow", "green"])

    def test_cpu_governor_leaves_busy_apps_alone_when_the_mac_is_fine(self):
        fm = machine()
        fm.procs[SPINNER]["cpu"] = 500.0
        for _ in range(8):
            fm.tick(cx)
        self.assertNotIn(str(SPINNER), fm.state.get("bg", {}))

    def test_cpu_governor_lowers_then_releases(self):
        fm = machine()
        fm.speed = 90  # macOS is lowering the CPU speed
        fm.procs[SPINNER]["cpu"] = 500.0
        for _ in range(8):
            fm.tick(cx)
        self.assertIn(str(SPINNER), fm.state["bg"])
        fm.procs[SPINNER]["cpu"] = 1.0
        fm.tick(cx)
        self.assertNotIn(str(SPINNER), fm.state["bg"])

    def test_reserve_escalates_when_reachable_and_builds_give_way(self):
        fm = machine()
        fm.procs[60] = proc(60, "clang", 300 * MB, path="/opt/clang")
        fm.mem.update(free=GB // 2, cached=GB)
        stages = [fm.tick(cx)["reserve_stage"] for _ in range(5)]
        self.assertEqual(stages, [1, 2, 3, 4, 4])
        self.assertEqual(fm.state["xcode_jobs"], 2)
        self.assertIn("60", fm.state["bg"])

    def test_reserve_stops_at_stage_2_when_out_of_reach(self):
        fm = machine()
        fm.procs[FRONT]["mem"] = 11 * GB  # what you're using leaves less than the 3 GB target
        fm.mem.update(free=GB // 2, cached=GB)
        stages = [fm.tick(cx)["reserve_stage"] for _ in range(4)]
        self.assertEqual(stages, [1, 2, 2, 2])
        self.assertFalse(fm.state["reserve"]["reachable"])

    def queue(self):
        """Two builds on a strained Mac: 70 runs (older), 80 waits with a compile job (81) under it."""
        fm = machine()
        fm.speed = 90
        fm.procs[70] = proc(70, "make", 10 * MB, path="/usr/bin/make")
        fm.procs[80] = proc(80, "make", 10 * MB, path="/usr/bin/make")
        fm.procs[81] = proc(81, "clang", 300 * MB, ppid=80, path="/opt/clang")
        return fm

    def test_queue_holds_the_compile_jobs_of_a_waiting_build(self):
        fm = self.queue()
        for _ in range(4):
            fm.tick(cx)
        self.assertEqual(self.signals(fm, CONT), set())
        self.assertEqual(fm.state["paused"]["81"]["kind"], "queue")
        self.assertEqual(fm.procs[81]["stat"], "T")

    def test_queue_releases_the_next_build_when_the_first_ends(self):
        fm = self.queue()
        fm.tick(cx)
        del fm.procs[70]
        fm.tick(cx)
        self.assertEqual(fm.procs[81]["stat"], "S")
        self.assertEqual(fm.state["paused"], {})

    def test_queue_holds_again_after_being_woken_from_outside(self):
        fm = self.queue()
        fm.tick(cx)
        fm.procs[80]["stat"] = fm.procs[81]["stat"] = "S"
        fm.tick(cx)
        self.assertEqual((fm.procs[80]["stat"], fm.procs[81]["stat"]), ("T", "T"))
        self.assertTrue(any("woken from outside" in l for l in fm.logs))

    def test_builds_run_side_by_side_when_the_mac_is_fine(self):
        fm = self.queue()
        fm.speed = 100
        for _ in range(3):
            fm.tick(cx)
        self.assertEqual(self.signals(fm, STOP), set())
        self.assertTrue(all(q["running"] for q in fm.state["build_queue"]))

    def test_queue_lets_a_build_through_after_max_wait(self):
        fm = self.queue()
        fm.tick(cx)
        fm.tick(cx, seconds=181 * 60)
        fm.tick(cx)
        self.assertEqual((fm.procs[80]["stat"], fm.procs[81]["stat"]), ("S", "S"))

    def test_shell_that_only_mentions_a_build_is_not_in_line(self):
        fm = self.queue()
        fm.procs.pop(70)
        fm.procs[60] = proc(60, "zsh", 5 * MB, path="/bin/zsh -c grep 'release.py build' notes.txt")
        fm.procs[61] = proc(61, "sleep", MB, ppid=60, path="/bin/sleep")
        st = fm.tick(cx)
        self.assertEqual([q["pid"] for q in st["build_queue"]], [80])
        self.assertEqual(self.signals(fm, STOP), set())

    def test_real_build_started_from_a_shell_is_in_line(self):
        fm = self.queue()
        fm.procs[60] = proc(60, "zsh", 5 * MB, path="/bin/zsh -c python3 tools/release.py build")
        fm.procs[61] = proc(61, "Python", 50 * MB, ppid=60, path="/usr/bin/python3 tools/release.py build")
        st = fm.tick(cx)
        self.assertIn(61, [q["pid"] for q in st["build_queue"]])
        self.assertNotIn(60, [q["pid"] for q in st["build_queue"]])

    def test_guard_still_takes_over_a_compile_job_paused_for_pressure(self):
        fm = machine()
        fm.procs[81] = proc(81, "clang", 300 * MB, stat="T", path="/opt/clang")
        fm.state = dict(paused={"81": dict(name="clang", t=fm.clock, why="pool red", kind="pressure", start="start", mem=300 * MB)})
        fm.tick(cx)
        self.assertIn(81, self.signals(fm, CONT))

    def test_off_switch_does_nothing(self):
        fm = machine()
        cx.OFF_FILE = __file__  # exists
        try:
            fm.mem.update(pressure=4, avail=GB)
            fm.tick(cx)
            self.assertEqual(fm.calls, [])
        finally:
            cx.OFF_FILE = "/nonexistent/clearx-off"

    def test_dry_run_sends_nothing(self):
        fm = machine()
        fm.tick(cx)
        fm.mem.update(pressure=4, avail=GB)
        fm.calls.clear()
        fm.tick(cx, dry=True)
        self.assertEqual(self.signals(fm, STOP), set())


if __name__ == "__main__":
    unittest.main()
