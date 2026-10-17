#!/usr/bin/env python3
"""Keep a daemon running: restart it when it exits, and when a new commit / config change lands.

    python supervise.py --name Coordinator -- python -u ../../auto-dev/tools/coordinator.py work
    python supervise.py --name dashboard --no-reload -- python ../../dev-automation/tools/dashboard.py serve

  * the child exits (crash, fatal error, killed) or cannot start -> restart after 2 s, 4 s, 8 s … 60 s
    (reset once it ran 5 minutes); every exit / restart goes to temp/daemon_health.jsonl
    (`daemon_common.py status`)
  * a NEW COMMIT (git HEAD), .env or work/agents.json change -> the child is stopped and restarted at once,
    so a long-running tab never keeps old code / old tokens. Uncommitted edits do NOT reload (deliberate:
    no restart in the middle of editing; commit to roll out). `--no-reload` turns this off.
  * stopping is ALWAYS the whole process tree: graceful first (CTRL_BREAK / SIGTERM to the daemon), then
    every descendant still alive is killed — an agent subprocess of the Coordinator must not keep running
    while its task is re-run elsewhere. On Windows the tree lives in a Job Object with KILL_ON_JOB_CLOSE:
    even if the supervisor itself is killed or the tab is closed, Windows ends the whole tree.
  * Ctrl+C in the tab stops the tree and the supervisor; a second Ctrl+C during the stop is ignored.

Output of the child goes to the same terminal. Stdlib only.
"""

import argparse
import os
import runpy
import signal
import subprocess
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
runpy.run_path(os.path.join(os.path.dirname(os.path.dirname(_HERE)), "_runtime", "bootstrap.py"))["activate"]()
from agent_skill_toolkit.core import paths, proc  # noqa: E402

sys.path.insert(0, _HERE)
import daemon_common as dc  # noqa: E402

BACKOFF_START, BACKOFF_MAX = 2.0, 60.0
HEALTHY_AFTER_S = 300          # a run this long resets the backoff
POLL_S = 1.0                   # child liveness check
WATCH_EVERY_S = 10.0           # code / config change check
STOP_GRACE_S = 20.0            # graceful stop, then the whole tree is killed
RELOAD = "code/cấu hình đổi"


def _say(name: str, msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] [supervise:{name}] {msg}", flush=True)


# ----------------------------------------------------------------------------- the process tree

class Tree:
    """The daemon and EVERY descendant. Windows: a Job Object (KILL_ON_JOB_CLOSE — also covers a supervisor
    that dies); POSIX: the daemon's process group plus a ppid walk at stop time (a descendant may have
    started its own session/group)."""

    def __init__(self, cmd: list):
        self.job = _WinJob() if os.name == "nt" else None
        self.p = subprocess.Popen(cmd, **proc.popen_group_kwargs())
        try:
            if self.job:
                self.job.assign(self.p)
        except BaseException:                                       # incl. Ctrl+C right here: no orphan
            proc.kill_tree(self.p)
            if self.job:
                self.job.close()
            raise

    def poll(self):
        return self.p.poll()

    @property
    def returncode(self):
        return self.p.returncode

    def stop(self, grace: float = STOP_GRACE_S) -> None:
        """Graceful to the daemon, then kill whatever of the tree is left; returns once it is all gone."""
        previous = signal.getsignal(signal.SIGINT)
        try:
            signal.signal(signal.SIGINT, signal.SIG_IGN)            # a 2nd Ctrl+C must not orphan the tree
        except ValueError:
            previous = None                                         # not the main thread (tests)
        try:
            pids = [] if self.job else _descendants(self.p.pid)
            if self.p.poll() is None:
                try:
                    if os.name == "nt":
                        self.p.send_signal(signal.CTRL_BREAK_EVENT)
                    else:
                        os.killpg(os.getpgid(self.p.pid), signal.SIGTERM)
                except (OSError, ValueError):
                    pass
                try:
                    self.p.wait(timeout=grace)
                except subprocess.TimeoutExpired:
                    pass
            if self.job:
                self.job.kill_all()                                 # every descendant, wherever its group
            else:
                for pid in pids + _descendants(self.p.pid):
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except OSError:
                        pass
            if self.p.poll() is None:
                proc.kill_tree(self.p)
            try:
                self.p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
        finally:
            if self.job:
                self.job.close()
            if previous is not None:
                signal.signal(signal.SIGINT, previous)

    def alive(self) -> int:
        """How many processes of the tree still run (Windows: the job's count)."""
        if self.job:
            return self.job.active()
        return int(self.p.poll() is None) + len(_descendants(self.p.pid))


def _descendants(pid: int) -> list:
    """POSIX: every process whose ancestry reaches `pid` (from `ps`, at call time)."""
    try:
        out = subprocess.run(["ps", "-A", "-o", "pid=,ppid="], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    kids = {}
    for line in out.splitlines():
        try:
            c, par = (int(x) for x in line.split())
        except ValueError:
            continue
        kids.setdefault(par, []).append(c)
    found, todo = [], [pid]
    while todo:
        for c in kids.get(todo.pop(), []):
            if c not in found:
                found.append(c)
                todo.append(c)
    return found


class _WinJob:
    """Windows Job Object, KILL_ON_JOB_CLOSE (ctypes, stdlib). Children inherit it (whatever process
    group they create), so terminating / closing the job ends the whole tree."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self.ct, k32 = ctypes, ctypes.WinDLL("kernel32", use_last_error=True)
        self.k32 = k32

        class Basic(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class Io(ctypes.Structure):
            _fields_ = [(n, ctypes.c_uint64) for n in ("r", "w", "o", "rb", "wb", "ob")]

        class Extended(ctypes.Structure):
            _fields_ = [("Basic", Basic), ("Io", Io), ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        class Accounting(ctypes.Structure):
            _fields_ = [("TotalUserTime", ctypes.c_int64), ("TotalKernelTime", ctypes.c_int64),
                        ("ThisPeriodTotalUserTime", ctypes.c_int64), ("ThisPeriodTotalKernelTime", ctypes.c_int64),
                        ("TotalPageFaultCount", wintypes.DWORD), ("TotalProcesses", wintypes.DWORD),
                        ("ActiveProcesses", wintypes.DWORD), ("TotalTerminatedProcesses", wintypes.DWORD)]
        self.Accounting = Accounting
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        self.h = k32.CreateJobObjectW(None, None)
        if not self.h:
            raise OSError(ctypes.get_last_error(), "CreateJobObject failed")
        info = Extended()
        info.Basic.LimitFlags = 0x2000                              # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k32.SetInformationJobObject(self.h, 9, ctypes.byref(info), ctypes.sizeof(info)):
            raise OSError(ctypes.get_last_error(), "SetInformationJobObject failed")

    def assign(self, p) -> None:
        if not self.k32.AssignProcessToJobObject(self.h, self.ct.c_void_p(int(p._handle))):
            raise OSError(self.ct.get_last_error(), "AssignProcessToJobObject failed")

    def active(self) -> int:
        acc = self.Accounting()
        ok = self.k32.QueryInformationJobObject(self.h, 1, self.ct.byref(acc), self.ct.sizeof(acc), None)
        return int(acc.ActiveProcesses) if ok else 0

    def kill_all(self, wait: float = 10) -> None:
        self.k32.TerminateJobObject(self.h, 1)
        deadline = time.monotonic() + wait
        while self.active() and time.monotonic() < deadline:       # verify: nothing of the tree is left
            time.sleep(0.1)

    def close(self) -> None:
        if self.h:
            self.k32.CloseHandle(self.h)
            self.h = None


# ----------------------------------------------------------------------------- change detection

def _git_head(root: str):
    """The commit HEAD points at, or None when it can not be read right now (never a 'change')."""
    try:
        r = subprocess.run(["git", "-C", root, "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    head = r.stdout.strip()
    return head if r.returncode == 0 and len(head) >= 7 else None


def fingerprint(root: str, last: tuple = None) -> tuple:
    """(HEAD, .env mtime, agents.json mtime). A part that can not be read keeps its LAST value: a git
    timeout or a file being rewritten is not a change (Codex supervise R1 #3)."""
    cur = [_git_head(root)]
    for rel in (".env", os.path.join("work", "agents.json")):
        try:
            cur.append(os.path.getmtime(os.path.join(root, rel)))
        except OSError:
            cur.append(None)
    if last:
        cur = [c if c is not None else old for c, old in zip(cur, last)]
    return tuple(cur)


# ----------------------------------------------------------------------------- the loop

def run(cmd: list, name: str, reload: bool = True, root: str = "", max_restarts: int = -1,
        sleep=time.sleep) -> int:
    """Supervise `cmd` until Ctrl+C (or `max_restarts` restarts, for tests). Returns 0."""
    root = root or paths.toolkit_root()
    backoff, restarts, seen = BACKOFF_START, 0, None
    dc.health_log(name, "supervisor_started", " ".join(cmd)[:300])
    while True:
        tree, reason, started = None, None, time.monotonic()
        try:
            if reload:
                seen = fingerprint(root, seen)
            _say(name, "chạy: " + " ".join(cmd))
            try:
                tree = Tree(cmd)
            except OSError as e:                                   # cannot start: same backoff path (R1 #5)
                reason = f"không khởi động được: {e}"
            last_watch = time.monotonic()
            while reason is None:
                if tree.poll() is not None:
                    reason = f"thoát mã {tree.returncode}"
                    break
                if reload and time.monotonic() - last_watch >= WATCH_EVERY_S:
                    last_watch = time.monotonic()
                    now = fingerprint(root, seen)
                    if now != seen:
                        seen, reason = now, RELOAD
                        _say(name, "phát hiện commit mới hoặc .env / agents.json đổi — khởi động lại")
                        break
                sleep(POLL_S)
            if tree is not None:
                tree.stop()                                        # the WHOLE tree, also after a crash (R1 #1)
        except KeyboardInterrupt:
            _say(name, "Ctrl+C — dừng")
            if tree is not None:
                tree.stop()
            dc.health_log(name, "supervisor_stopped", "Ctrl+C")
            return 0
        ran = time.monotonic() - started
        if reason == RELOAD:
            dc.health_log(name, "reloaded", reason)
            wait = 0.0
        else:
            backoff = BACKOFF_START if ran >= HEALTHY_AFTER_S else backoff
            wait = backoff
            backoff = min(BACKOFF_MAX, backoff * 2)
            dc.health_log(name, "restarted", f"{reason} sau {round(ran)} s; chạy lại sau {wait:.0f} s")
            _say(name, f"{reason} (chạy {round(ran)} s) — chạy lại sau {wait:.0f} s")
        restarts += 1
        if 0 <= max_restarts < restarts:
            return 0
        try:
            sleep(wait)
        except KeyboardInterrupt:
            dc.health_log(name, "supervisor_stopped", "Ctrl+C")
            return 0


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if "--" not in argv:
        print("usage: supervise.py [--name N] [--no-reload] -- <command ...>", file=sys.stderr)
        return 2
    i = argv.index("--")
    ap = argparse.ArgumentParser(prog="supervise.py")
    ap.add_argument("--name", default="")
    ap.add_argument("--no-reload", action="store_true", help="do not restart on a new commit / config change")
    a = ap.parse_args(argv[:i])
    cmd = argv[i + 1:]
    if not cmd:
        print("supervise.py: missing command after --", file=sys.stderr)
        return 2
    script = next((x for x in cmd if x.endswith(".py")), cmd[0])
    return run(cmd, a.name or os.path.splitext(os.path.basename(script))[0], reload=not a.no_reload)


if __name__ == "__main__":
    sys.exit(main())
