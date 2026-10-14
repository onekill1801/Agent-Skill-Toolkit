#!/usr/bin/env python3
"""Shared core primitives (checklist R.M3): env, console, locks, text — same behaviour as the skill
helpers they replace.
    python -m unittest discover -s tests
"""

import io
import os
import runpy
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
runpy.run_path(os.path.join(_ROOT, ".claude", "skills", "_runtime", "bootstrap.py"))["activate"]()
from agent_skill_toolkit.core import console, env, locks, text  # noqa: E402


class EnvTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="env-")
        self.file = os.path.join(self.tmp, "x.env")
        with open(self.file, "w", encoding="utf-8") as f:
            f.write("# comment\nA=1\nB = ignored\nC=two # trailing\nD=\"quoted # kept\"\nE='x'\n_F=ok\nexport G=no\n")

    def test_parse_matches_the_skill_loader(self):
        sys.path.insert(0, os.path.join(_ROOT, ".claude", "skills", "dev-automation", "tools"))
        import config as skill_config
        self.assertEqual(env.parse("A=first\nA=second\n"), {"A": "first"})   # Codex R.M3 #3
        got = env.parse(open(self.file, encoding="utf-8").read())
        self.assertEqual(got, {"A": "1", "C": "two", "D": "quoted # kept", "E": "x", "_F": "ok"})
        for raw in ("two # trailing", '"quoted # kept"', "'x'", "  spaced  "):
            self.assertEqual(env._strip_inline_comment(raw), skill_config._strip_inline_comment(raw), raw)

    def test_environment_wins_and_explicit_file(self):
        with mock.patch.dict(os.environ, {"TOOLKIT_ENV_FILE": self.file, "A": "from-env"}):
            os.environ.pop("C", None)
            self.assertEqual(env.load(force=True), self.file)
            self.assertEqual(env.get("A"), "from-env")
            self.assertEqual(env.get("C"), "two")
            self.assertEqual(env.get("MISSING", "dflt"), "dflt")
        with mock.patch.dict(os.environ, {"TOOLKIT_ENV_FILE": os.path.join(self.tmp, "nope.env")}):
            self.assertEqual(env.load(force=True), "")


class LockTest(unittest.TestCase):
    def test_exclusive_timeout_and_stale(self):
        path = os.path.join(tempfile.mkdtemp(prefix="lock-"), "a.lock")
        with locks.file_lock(path):
            with self.assertRaises(TimeoutError):
                with locks.file_lock(path, timeout=0.3):
                    pass
        self.assertTrue(os.path.exists(path))             # the lock file stays; the OS lock is what matters
        with open(path, "w") as f:                        # a file left behind is just a file: no lock held
            f.write("999 0")
        with locks.file_lock(path, timeout=1, stale_after=60):
            pass

    def test_a_crashed_holder_releases_the_lock(self):        # Codex R.M3 #1: the OS releases it
        import subprocess
        path = os.path.join(tempfile.mkdtemp(prefix="lock-"), "c.lock")
        code = ("import runpy,sys,time;runpy.run_path(sys.argv[1])['activate']();"
                "from agent_skill_toolkit.core import locks\n"
                "with locks.file_lock(sys.argv[2]):\n    print('held', flush=True); time.sleep(30)")
        p = subprocess.Popen([sys.executable, "-c", code, os.path.join(_ROOT, ".claude", "skills", "_runtime", "bootstrap.py"),
                              path], stdout=subprocess.PIPE, text=True)
        self.assertEqual(p.stdout.readline().strip(), "held")
        with self.assertRaises(TimeoutError):
            with locks.file_lock(path, timeout=0.3):
                pass
        p.kill()
        p.wait()
        with locks.file_lock(path, timeout=5):           # released by the OS, no stale guessing
            pass
        self.assertTrue(os.path.exists(path))            # never unlinked

    def test_serialises_threads(self):
        path = os.path.join(tempfile.mkdtemp(prefix="lock-"), "b.lock")
        inside, peak = [0], [0]

        def work():
            with locks.file_lock(path, timeout=10, poll=0.01):
                inside[0] += 1
                peak[0] = max(peak[0], inside[0])
                time.sleep(0.02)
                inside[0] -= 1
        ts = [threading.Thread(target=work) for _ in range(6)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(peak[0], 1)

    def test_review_queue_lock_uses_the_primitive(self):
        sys.path.insert(0, os.path.join(_ROOT, ".claude", "skills", "mr-review", "tools"))
        import review_common as rc
        with mock.patch.dict(os.environ, {"REVIEW_STATE_DIR": tempfile.mkdtemp(prefix="rq-")}):
            with mock.patch.object(locks, "file_lock", wraps=locks.file_lock) as spy:
                with rc.file_lock("queue", timeout=1):
                    pass
            self.assertTrue(spy.called)
            self.assertTrue(spy.call_args[0][0].endswith(os.path.join("locks", "queue.lock")))

    def test_env_search_matches_the_original_places(self):     # Codex R.M3 #2
        root = env.paths.toolkit_root()
        seen = []
        real = os.path.isfile
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TOOLKIT_ENV_FILE", None)
            with mock.patch.object(env.os, "getcwd", return_value=tempfile.mkdtemp(prefix="cwd-")),                     mock.patch.object(env.os.path, "isfile", side_effect=lambda p: seen.append(p) or False):
                self.assertEqual(env.find_env_file(), "")
        want = [os.path.join(root, *parts, ".env") for parts in
                (("x",), (".claude", "skills", "dev-automation", "tools"), (".claude", "skills", "dev-automation"),
                 (".claude", "skills"), (".claude",), ())]
        self.assertEqual([os.path.normcase(p) for p in seen[1:]], [os.path.normcase(p) for p in want[1:]])


class ConsoleTextTest(unittest.TestCase):
    def test_utf8_never_raises(self):
        buf = io.StringIO()
        console.utf8([buf, object()])                     # no reconfigure / odd stream: ignored

    def test_text_helpers_unchanged(self):
        from agent_skill_toolkit.workflows.intake import lanes
        self.assertIs(lanes.quoted, text.quoted)
        self.assertEqual(text.quoted("a\nb «x» *y*"), "«a b ‹x› \\*y\\*»")
        self.assertEqual(text.one_line("a\n\n[b](c)"), "a \\[b\\](c)")


if __name__ == "__main__":
    unittest.main()
