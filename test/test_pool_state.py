#!/usr/bin/env python3
"""Pool state unit tests: no real processes, signals or machine readings.
  python3 -m unittest discover -s test
"""
import configparser, os, sys, tempfile, unittest

D = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
TMP = tempfile.mkdtemp(prefix="clearx-unit-")
os.environ.update(CLEARX_STATE=TMP, CLEARX_LOGS=TMP)
sys.path.insert(0, D)
import clearx as cx  # noqa: E402

GB = 1 << 30
NOW = 1000000.0


def conf():
    c = configparser.ConfigParser()
    c.read_string("[general]\n[levels]\n[vram]\n[predict]\n[pool]\n")
    return c


def history(rates):
    """One row per 10 s tick, oldest first, ending at NOW; rates in MB/s."""
    t0 = NOW - 10 * (len(rates) - 1)
    return [[t0 + 10 * i, 6 * GB, 8 * GB, r, 0, 0] for i, r in enumerate(rates)]


def level(rates, avail=6 * GB, pressure=1, swap=8 * GB, free=GB // 2):
    m = dict(pressure=pressure, swap=swap, free=free, avail=avail, app=4 * GB, wired=3 * GB, compressed=GB, cached=4 * GB)
    st = dict(hist=history(rates))
    return cx.pool_state(conf(), st, m, None, NOW, rates[-1] * 10)["state"]


class PoolLevels(unittest.TestCase):
    def setUp(self):
        self.saved = cx.disk_probe, cx.thermal, cx.loadavg, cx.sh
        cx.disk_probe = lambda: (100 * GB, 1.0)
        cx.thermal = lambda: (100, 100)
        cx.loadavg = lambda: [1.0, 1.0, 1.0]
        cx.sh = lambda *a, **k: str(16 * GB)

    def tearDown(self):
        cx.disk_probe, cx.thermal, cx.loadavg, cx.sh = self.saved

    def test_allocated_swap_alone_is_green(self):
        self.assertEqual(level([0] * 7, swap=12 * GB), "green")

    def test_low_free_alone_is_green(self):
        self.assertEqual(level([0] * 7, free=100 << 20), "green")

    def test_one_swap_burst_with_stale_swap_is_green(self):
        # the common false red: 8 GB allocated, one 334 MB tick, 5.4 GB available, normal pressure
        self.assertEqual(level([0, 0, 0, 0, 0, 0, 33.4], avail=int(5.4 * GB)), "green")

    def test_sustained_swap_with_room_is_only_yellow(self):
        self.assertEqual(level([55] * 7, avail=6 * GB), "yellow")

    def test_sustained_swap_with_little_room_is_orange(self):
        self.assertEqual(level([55] * 7, avail=int(3.1 * GB)), "orange")

    def test_sustained_swap_almost_out_is_red(self):
        self.assertEqual(level([55] * 7, avail=int(1.5 * GB)), "red")

    def test_critical_pressure_is_always_red(self):
        self.assertEqual(level([0] * 7, pressure=4, avail=8 * GB), "red")

    def test_pressure_warn(self):
        self.assertEqual(level([0] * 7, pressure=2, avail=6 * GB), "yellow")
        self.assertEqual(level([0] * 7, pressure=2, avail=3 * GB), "orange")

    def test_spike_right_after_wake_is_not_read_as_sustained(self):
        # 2 GB in one tick averages 33 MB/s over the minute: not the 200 MB/s of that one tick, so never red
        self.assertEqual(level([0, 200], avail=int(1.5 * GB)), "orange")


class SwapOutRate(unittest.TestCase):
    def test_average_over_window(self):
        self.assertAlmostEqual(cx.swap_out_rate(history([30] * 7), NOW, 60), 30)

    def test_old_rows_are_ignored(self):
        self.assertEqual(cx.swap_out_rate(history([500] * 3 + [0] * 7), NOW, 60), 0)

    def test_empty(self):
        self.assertEqual(cx.swap_out_rate([], NOW, 60), 0)


if __name__ == "__main__":
    unittest.main()
