"""supervise.py: a daemon is restarted when it exits (with backoff) and when code / config changes."""

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".claude" / "skills" / "dev-automation" / "tools"))
import supervise as S  # noqa: E402


class SuperviseTest(unittest.TestCase):
    def setUp(self):
        self.log = []
        p = mock.patch.object(S.dc, "health_log", side_effect=lambda *a: self.log.append(a))
        p.start()
        self.addCleanup(p.stop)
        self.sleeps = []

    def sleep(self, s):
        self.sleeps.append(s)
        time.sleep(0.01 if s == S.POLL_S else 0)

    def test_restarts_a_crashing_child_with_growing_backoff(self):
        cmd = [sys.executable, "-c", "import sys; sys.exit(3)"]
        S.run(cmd, "t", reload=False, max_restarts=3, sleep=self.sleep)
        events = [e[1] for e in self.log]
        self.assertEqual(events, ["supervisor_started", "restarted", "restarted", "restarted", "restarted"])
        waits = [s for s in self.sleeps if s != S.POLL_S]
        self.assertEqual(waits, [2.0, 4.0, 8.0])                              # last restart: max reached first
        self.assertIn("thoát mã 3", self.log[1][2])

    def test_a_code_change_restarts_at_once(self):
        cmd = [sys.executable, "-c", "import time; time.sleep(30)"]
        prints = iter([("a",), ("b",)] + [("b",)] * 50)
        with mock.patch.object(S, "fingerprint", side_effect=lambda root: next(prints)), \
             mock.patch.object(S, "WATCH_EVERY_S", 0), \
             mock.patch.object(S, "stop", wraps=lambda p, grace=1: S.proc.kill_tree(p)) as stop:
            S.run(cmd, "t", reload=True, root=str(ROOT), max_restarts=0, sleep=self.sleep)
        stop.assert_called_once()
        self.assertEqual([e[1] for e in self.log], ["supervisor_started", "reloaded"])

    def test_ctrl_c_stops_child_and_supervisor(self):
        cmd = [sys.executable, "-c", "import time; time.sleep(30)"]

        def interrupt(_):
            raise KeyboardInterrupt
        with mock.patch.object(S, "stop", wraps=lambda p, grace=1: S.proc.kill_tree(p)) as stop:
            self.assertEqual(S.run(cmd, "t", reload=False, sleep=interrupt), 0)
        stop.assert_called_once()
        self.assertEqual(self.log[-1][1], "supervisor_stopped")

    def test_graceful_stop_then_kill(self):
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], **S.proc.popen_group_kwargs())
        t0 = time.monotonic()
        S.stop(p, grace=5)
        self.assertIsNotNone(p.poll())
        self.assertLess(time.monotonic() - t0, 10)

    def test_fingerprint_follows_head_and_config(self):
        tmp = tempfile.mkdtemp()
        subprocess.run(["git", "init", "-q", tmp], check=True)
        env = os.path.join(tmp, ".env")
        open(env, "w").close()
        a = S.fingerprint(tmp)
        os.utime(env, (1, 1))
        self.assertNotEqual(S.fingerprint(tmp), a)

    def test_cli_needs_a_command(self):
        self.assertEqual(S.main(["--name", "x"]), 2)
        self.assertEqual(S.main(["--"]), 2)


if __name__ == "__main__":
    unittest.main()
