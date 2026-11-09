#!/usr/bin/env python3
"""The health watch (src/health.py): parsing, the checks that need no Mac, the report."""
import os, shutil, sys, tempfile, time, unittest

D = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, os.path.join(D, "src"))
import health  # noqa: E402


class Health(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="clearx-health-")
        self.old, os.environ["HOME"] = os.environ.get("HOME"), self.home

    def tearDown(self):
        os.environ["HOME"] = self.old
        shutil.rmtree(self.home, ignore_errors=True)

    def test_etime(self):
        self.assertEqual(health.etime_s("05"), 5)
        self.assertEqual(health.etime_s("01:02"), 62)
        self.assertEqual(health.etime_s("03-18:27:37"), 3 * 86400 + 18 * 3600 + 27 * 60 + 37)

    def test_stuck_update_service(self):
        ps = [(44300, 1, 21.7, 4 * 86400, "/System/Library/CoreServices/Software Update.app/Contents/Resources/softwareupdated")]
        self.assertEqual(health.stuck(ps)[0][1], "softwareupdated")
        self.assertEqual(health.stuck([(1, 1, 21.7, 3600, "softwareupdated")]), [])

    def test_leftover_caffeinate_found_not_ended_in_preview(self):
        ps = [(14138, 1, 0.0, 28 * 3600, "caffeinate -is"), (99, 500, 0.0, 28 * 3600, "caffeinate -i make")]
        f = health.awake(ps, print, fix=False)
        self.assertEqual([x[1] for x in f], ["caffeinate-14138"])  # (one with a living parent is its app's own)

    def test_leftovers(self):
        p = os.path.join(self.home, "Developer", "TEK", "_to_delete")
        os.makedirs(p)
        old = time.time() - 5 * 86400
        os.utime(p, (old, old))
        self.assertEqual(len(health.leftovers(os.path.join(self.home, "Developer"))), 1)

    def test_report_newest_first(self):
        path = os.path.join(self.home, "r.md")
        health.write_report([("serious", "no-backup", "Nothing backs this Mac up.")], path)
        health.write_report([], path)
        txt = open(path).read()
        self.assertLess(txt.index("Nothing wrong found"), txt.index("Nothing backs"))

    def test_only_new_serious_notified_once(self):
        orig, orig_l = health.CHECKS, health.leftovers
        health.CHECKS = ("leftovers",)
        try:
            st = {}
            health.leftovers = lambda: [("serious", "x", "X")]
            self.assertEqual(len(health.check(state=st, fix=False)[1]), 1)
            self.assertEqual(len(health.check(state=st, fix=False)[1]), 0)
        finally:
            health.CHECKS, health.leftovers = orig, orig_l


if __name__ == "__main__":
    unittest.main()
