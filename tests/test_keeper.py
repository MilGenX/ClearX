#!/usr/bin/env python3
"""The disk keeper (src/keeper.py) on a fake home folder: what it picks, what it leaves, archive and put back.
  python3 -m unittest discover -s tests
"""
import os, shutil, struct, subprocess, sys, tempfile, time, unittest, wave

D = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, os.path.join(D, "src"))
import keeper  # noqa: E402

DAY = 86400


def touch(p, size=0, age_days=0):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(b"x" * size)
    t = time.time() - age_days * DAY
    os.utime(p, (t, t))
    return p


class Keeper(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="clearx-keeper-")
        self.old = os.environ.get("HOME")
        os.environ["HOME"] = self.home
        self.logs = []

    def tearDown(self):
        os.environ["HOME"] = self.old
        shutil.rmtree(self.home, ignore_errors=True)

    def log(self, m):
        self.logs.append(m)

    def test_rebuildable_only_untouched_build_folders(self):
        dev = os.path.join(self.home, "Developer")
        for proj in ("App", "Live", "Tools"):
            os.makedirs(os.path.join(dev, proj))
            subprocess.run(["git", "init", "-q", os.path.join(dev, proj)], check=True)
            with open(os.path.join(dev, proj, ".gitignore"), "w") as f:
                f.write("build/\n")
        tracked = touch(os.path.join(dev, "Tools/build/script.sh"), 10, age_days=30)  # code that lives in "build"
        subprocess.run(["git", "-C", os.path.join(dev, "Tools"), "add", "-f", "build/script.sh"], check=True)
        old = touch(os.path.join(dev, "App/build/obj/a.o"), 1000, age_days=30)
        os.utime(os.path.dirname(old), (time.time() - 30 * DAY,) * 2)
        os.utime(os.path.dirname(os.path.dirname(old)), (time.time() - 30 * DAY,) * 2)
        touch(os.path.join(dev, "Live/build/obj/b.o"), 1000, age_days=0)  # a build running now
        touch(os.path.join(dev, "App/src/main.c"), 10, age_days=30)
        found = [i["path"] for i in keeper.rebuildable(dev, days=7, names=["build"])]
        self.assertEqual(found, [os.path.join(dev, "App/build")])
        keeper.remove([dict(path=found[0], size=1000)], self.log, apply=True)
        self.assertFalse(os.path.exists(found[0]))
        self.assertTrue(os.path.exists(os.path.join(dev, "App/src/main.c")))
        self.assertTrue(os.path.exists(os.path.join(dev, "Live/build/obj/b.o")))
        self.assertTrue(os.path.exists(tracked))

    def test_preview_changes_nothing(self):
        p = touch(os.path.join(self.home, "Downloads/Old.dmg"), 100, age_days=60)
        touch(os.path.join(self.home, "Downloads/New.dmg"), 100, age_days=1)
        found = keeper.installers(os.path.join(self.home, "Downloads"), days=30)
        self.assertEqual([i["path"] for i in found], [p])
        keeper.remove(found, self.log, apply=False)
        self.assertTrue(os.path.exists(p))

    def test_cold_picks_only_old_files_of_kinds_that_shrink(self):
        docs = os.path.join(self.home, "Documents")
        log = touch(os.path.join(docs, "server.log"), 6 << 20, age_days=200)
        touch(os.path.join(docs, "photo.jpg"), 6 << 20, age_days=200)        # already compressed
        touch(os.path.join(docs, "notes.txt"), 6 << 20, age_days=10)          # in use
        touch(os.path.join(docs, "Song.logicx/Alternatives/a.txt"), 6 << 20, age_days=200)  # inside a bundle
        found = [i["path"] for i in keeper.cold_candidates([docs], days=90, min_size="5M")]
        self.assertEqual(found, [log])

    def test_archive_and_put_back(self):
        p = touch(os.path.join(self.home, "Documents/data.csv"), 6 << 20, age_days=200)
        keeper.archive([dict(path=p, size=6 << 20)], self.log, apply=True)
        dest = os.path.join(self.home, keeper.COLD_DIR_NAME, "Documents/data.csv")
        self.assertFalse(os.path.exists(p))
        self.assertTrue(os.path.exists(dest))
        self.assertIn(dest, keeper.load_manifest()["files"])
        self.assertEqual(keeper.unarchive("all", self.log), 1)
        self.assertTrue(os.path.exists(p))
        self.assertEqual(os.path.getsize(p), 6 << 20)
        self.assertEqual(keeper.load_manifest()["files"], {})

    def test_growth_names_the_folder_that_jumped(self):
        hist = [dict(t=0, sizes={"/a": 10 << 30, "/b": 5 << 30})]
        grew = keeper.growth({"/a": 31 << 30, "/b": 6 << 30, "/new": 50 << 30}, hist, 5 << 30)
        self.assertEqual(grew, [["/a", 21 << 30]])

    def test_pcm_compare_reads_wav_frames(self):
        p = os.path.join(self.home, "t.wav")
        with wave.open(p, "wb") as w:
            w.setnchannels(2); w.setsampwidth(2); w.setframerate(44100)
            w.writeframes(b"".join(struct.pack("<hh", i, -i) for i in range(1000)))
        ch, width, rate, frames = keeper.pcm_frames(p)
        self.assertEqual((ch, width, rate, len(frames)), (2, 2, 44100, 4000))

    def test_a_failing_step_never_stops_the_others(self):
        orig = keeper.installers
        keeper.installers = lambda: (_ for _ in ()).throw(OSError("boom"))
        try:
            touch(os.path.join(self.home, "Documents/x.log"), 6 << 20, age_days=200)
            r = keeper.keep(log=self.log, apply=False, steps=["installers", "cold"])
        finally:
            keeper.installers = orig
        self.assertIn("cold", r["found"])
        self.assertTrue(any("installers failed" in m for m in self.logs))


if __name__ == "__main__":
    unittest.main()
