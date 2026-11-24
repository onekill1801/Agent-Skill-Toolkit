"""supervise.py: a daemon is restarted when it exits (with backoff) and on a new commit / config change;
stopping always takes the whole process tree."""

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

SLEEPER = [sys.executable, "-c", "import time; time.sleep(60)"]


def _alive(pid: int) -> bool:
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


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
        S.run([sys.executable, "-c", "import sys; sys.exit(3)"], "t", reload=False, max_restarts=3, sleep=self.sleep)
        self.assertEqual([e[1] for e in self.log], ["supervisor_started"] + ["restarted"] * 4)
        self.assertEqual([s for s in self.sleeps if s != S.POLL_S], [2.0, 4.0, 8.0])
        self.assertIn("thoát mã 3", self.log[1][2])

    def test_a_launch_failure_takes_the_backoff_path(self):                    # Codex supervise R1 #5
        S.run([os.path.join(tempfile.mkdtemp(), "no-such-program.exe")], "t", reload=False, max_restarts=1,
              sleep=self.sleep)
        self.assertEqual([e[1] for e in self.log], ["supervisor_started", "restarted", "restarted"])
        self.assertIn("không khởi động được", self.log[1][2])

    def test_a_new_commit_restarts_at_once(self):
        prints = iter([("a", 1, 1), ("b", 1, 1)] + [("b", 1, 1)] * 50)
        with mock.patch.object(S, "fingerprint", side_effect=lambda root, last=None: next(prints)), \
             mock.patch.object(S, "WATCH_EVERY_S", 0), mock.patch.object(S, "STOP_GRACE_S", 5):
            S.run(SLEEPER, "t", reload=True, root=str(ROOT), max_restarts=0, sleep=self.sleep)
        self.assertEqual([e[1] for e in self.log], ["supervisor_started", "reloaded"])

    def test_ctrl_c_stops_the_tree_and_the_supervisor(self):
        trees = []
        real = S.Tree

        def track(cmd):
            trees.append(real(cmd))
            return trees[-1]

        def interrupt(_):
            raise KeyboardInterrupt
        with mock.patch.object(S, "Tree", side_effect=track), mock.patch.object(S, "STOP_GRACE_S", 5):
            self.assertEqual(S.run(SLEEPER, "t", reload=False, sleep=interrupt), 0)
        self.assertIsNotNone(trees[0].poll())
        self.assertEqual(self.log[-1][1], "supervisor_stopped")

    def test_stop_kills_descendants_in_their_own_process_group(self):          # Codex supervise R1 #1
        tmp = tempfile.mkdtemp()
        pidfile = os.path.join(tmp, "grandchild.pid")
        flags = "creationflags=0x200" if os.name == "nt" else "start_new_session=True"
        code = ("import subprocess, sys, time\n"
                f"g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], {flags})\n"
                f"open({pidfile!r}, 'w').write(str(g.pid))\n"
                "time.sleep(60)\n")
        tree = S.Tree([sys.executable, "-c", code])
        deadline = time.monotonic() + 20
        def read():
            try:
                with open(pidfile) as f:
                    return f.read()
            except OSError:
                return ""
        while not read() and time.monotonic() < deadline:
            time.sleep(0.1)
        grandchild = int(read())
        self.assertTrue(_alive(grandchild))
        tree.stop(grace=2)
        time.sleep(0.5)
        self.assertFalse(_alive(grandchild), "a descendant survived the stop")
        self.assertIsNotNone(tree.poll())

    def test_descendants_of_a_crashed_daemon_are_still_killed(self):         # Codex supervise R2 #3 / #1
        tmp = tempfile.mkdtemp()
        pidfile = os.path.join(tmp, "orphan.pid")
        flags = "creationflags=0x200" if os.name == "nt" else "start_new_session=True"
        code = ("import subprocess, sys\n"                           # spawns at once, then crashes
                f"g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], {flags})\n"
                f"open({pidfile!r}, 'w').write(str(g.pid))\n"
                "sys.exit(1)\n")
        tree = S.Tree([sys.executable, "-c", code])
        tree.p.wait(timeout=20)                                       # the daemon is gone
        with open(pidfile) as f:
            orphan = int(f.read())
        self.assertTrue(_alive(orphan))
        tree.stop(grace=1)
        time.sleep(0.5)
        self.assertFalse(_alive(orphan), "the orphan of a crashed daemon survived")

    @unittest.skipUnless(os.name == "nt", "Windows Job Object")
    def test_a_failed_launch_closes_its_job(self):                           # Codex supervise R3 #3
        closed = []
        real = S._WinJob

        class Spy(real):
            def close(self):
                closed.append(True)
                super().close()
        with mock.patch.object(S, "_WinJob", Spy):
            with self.assertRaises(OSError):
                S.Tree([os.path.join(tempfile.mkdtemp(), "missing.exe")])
        self.assertEqual(closed, [True])

    @unittest.skipUnless(os.name == "nt", "Windows Job Object")
    def test_job_setup_failure_closes_the_handle(self):                      # Codex supervise R4 #2
        k32 = mock.Mock()
        k32.CreateJobObjectW.return_value = 1234
        k32.SetInformationJobObject.return_value = 0
        with mock.patch("ctypes.WinDLL", return_value=k32):
            with self.assertRaises(OSError):
                S._WinJob()
        k32.CloseHandle.assert_called_once_with(1234)

    @unittest.skipIf(os.name == "nt", "POSIX cleanup")
    def test_a_descendant_spawning_during_shutdown_is_caught(self):          # Codex supervise R4 #1
        code = ("import subprocess, sys, time\n"
                "while True:\n"                                       # keeps starting detached children
                "    subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)\n"
                "    time.sleep(0.05)\n")
        tree = S.Tree([sys.executable, "-c", code])
        time.sleep(1)
        tree.stop(grace=1)
        time.sleep(0.5)
        self.assertEqual(S._marked(tree.marker), [])

    def fake_tree(self, stops, rests=()):
        """A Tree double: stop() returns stops[i] (survivors) and may flag a Ctrl+C."""
        test = self

        class Fake:
            made = []

            def __init__(self, cmd):
                self.interrupted, self.rest = False, list(rests)
                Fake.made.append(self)

            def poll(self):
                return 0

            returncode = 0

            def track(self):
                pass

            def stop(self, grace=0):
                out = stops.pop(0)
                if out == "CTRL_C":
                    self.interrupted = True
                    return []
                return out

            def kill_rest(self):
                return self.rest.pop(0) if self.rest else []
        test.Fake = Fake
        return Fake

    def test_ctrl_c_during_a_stop_is_honoured(self):                         # Codex supervise R5 #1
        with mock.patch.object(S, "Tree", self.fake_tree(["CTRL_C"])):
            self.assertEqual(S.run(["x"], "t", reload=False, max_restarts=5, sleep=self.sleep), 0)
        self.assertEqual(len(self.Fake.made), 1)                             # no new daemon after the Ctrl+C
        self.assertEqual(self.log[-1][1], "supervisor_stopped")

    def test_no_restart_while_the_old_tree_survives(self):                   # Codex supervise R5 #2
        with mock.patch.object(S, "Tree", self.fake_tree([["123"], []], rests=[["123"], []])):
            S.run(["x"], "t", reload=False, max_restarts=1, sleep=self.sleep)
        events = [e[1] for e in self.log]
        first_restart = events.index("restarted")
        self.assertEqual(events[1:first_restart], ["cleanup_blocked", "cleanup_blocked"])
        self.assertEqual(self.sleeps.count(5), 2)                            # waited, killed again, then restarted

    def test_ctrl_c_during_a_failed_launch_still_stops(self):               # Codex supervise R6 #2
        def popen(*a, **k):
            S.signal.raise_signal(S.signal.SIGINT)                   # Ctrl+C arrives during the launch…
            raise OSError("launch failed")                           # …which then fails
        with mock.patch.object(S.subprocess, "Popen", side_effect=popen):
            self.assertEqual(S.run(["x"], "t", reload=False, max_restarts=5, sleep=self.sleep), 0)
        self.assertEqual(self.log[-1][1], "supervisor_stopped")
        self.assertNotIn("restarted", [e[1] for e in self.log])

    @unittest.skipUnless(os.name == "nt", "Windows Job Object")
    def test_a_failed_job_query_is_not_an_empty_job(self):                   # Codex supervise R6 #1
        tree = S.Tree(SLEEPER)
        try:
            with mock.patch.object(tree.job, "active", side_effect=OSError("query failed")):
                self.assertTrue(tree.kill_rest())                           # survivors reported, gate stays shut
        finally:
            tree.stop(grace=1)
        self.assertEqual(tree.kill_rest(), [])

    def test_a_failed_process_scan_is_not_an_empty_tree(self):               # Codex supervise R7 #2
        tree = mock.Mock(marker="m")
        with mock.patch.object(S, "_descendants", return_value=None), \
             mock.patch.object(S, "_marked", return_value=[]), mock.patch.object(S.time, "sleep"):
            left = S._kill_until_empty(tree, wait=0)
        self.assertTrue(left)                                               # gate stays shut
        self.assertIn("không liệt kê được", left[0])

    def test_only_esrch_means_gone(self):                                     # Codex supervise R8 #1
        with mock.patch.object(S.os, "kill", side_effect=ProcessLookupError):
            self.assertFalse(S._signal(1, 9))
        with mock.patch.object(S.os, "kill", side_effect=PermissionError):
            self.assertTrue(S._signal(1, 9))                                  # alive, just not ours to kill
        tree = mock.Mock(marker="m")
        with mock.patch.object(S, "_descendants", return_value=[4242]), \
             mock.patch.object(S, "_marked", return_value=[]), \
             mock.patch.object(S.os, "kill", side_effect=PermissionError), \
             mock.patch.object(S, "_reap_adopted"), mock.patch.object(S.time, "sleep"), \
             mock.patch.object(S.signal, "SIGKILL", 9, create=True):
            self.assertEqual(S._kill_until_empty(tree, wait=0), ["4242"])      # a survivor: gate stays shut

    def test_git_failure_is_not_a_change(self):                               # Codex supervise R1 #3
        last = ("abc1234", 5.0, 6.0)
        with mock.patch.object(S, "_git_head", return_value=None), \
             mock.patch.object(S.os.path, "getmtime", side_effect=OSError):
            self.assertEqual(S.fingerprint("x", last), last)

    def test_fingerprint_follows_head_and_config(self):
        tmp = tempfile.mkdtemp()
        subprocess.run(["git", "init", "-q", tmp], check=True)
        env = os.path.join(tmp, ".env")
        open(env, "w").close()
        a = S.fingerprint(tmp)
        os.utime(env, (1, 1))
        self.assertNotEqual(S.fingerprint(tmp, a), a)

    def test_cli_needs_a_command(self):
        self.assertEqual(S.main(["--name", "x"]), 2)
        self.assertEqual(S.main(["--"]), 2)


if __name__ == "__main__":
    unittest.main()
