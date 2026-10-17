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
    """The daemon and EVERY descendant, contained for its whole life:
      * Windows — a Job Object (KILL_ON_JOB_CLOSE). The daemon is created SUSPENDED, put in the job, then
        resumed, so nothing it starts can be outside the job (Codex supervise R2 #1); a dying supervisor
        or a closed tab ends the tree too.
      * POSIX — Linux makes the supervisor a child subreaper (orphans of a crashed daemon re-parent to
        it, so they stay findable — R2 #3); everywhere, the daemon's environment carries a unique marker
        every descendant inherits, so stop() finds even a re-parented one by it (and only ours)."""

    def __init__(self, cmd: list):
        self.job, self.marker, self.interrupted = None, "", False
        previous = _catch_sigint(self)              # no Ctrl+C between spawn and containment (R2 #2) — recorded
        try:
            if os.name == "nt":
                self.job = _WinJob()
                kw = proc.popen_group_kwargs()
                kw["creationflags"] = kw.get("creationflags", 0) | 0x00000004      # CREATE_SUSPENDED
                self.p = None
                try:
                    self.p = subprocess.Popen(cmd, **kw)
                    self.job.assign(self.p)
                    _nt_resume(self.p)
                except BaseException:                              # incl. Popen itself (R3 #3): no handle leak
                    if self.p is not None:
                        self.p.kill()
                    self.job.close()
                    raise
            else:
                _become_subreaper()
                # every descendant inherits this marker in its environment, even after a crashed daemon's
                # child is re-parented (macOS has no subreaper — R3 #1): stop() finds them by it
                self.marker = f"{os.getpid()}-{time.monotonic_ns()}"
                env = {**os.environ, TREE_ENV: self.marker}
                self.p = subprocess.Popen(cmd, env=env, **proc.popen_group_kwargs())
        except BaseException:
            if self.interrupted:                           # Ctrl+C during a launch that then failed (R6 #2)
                raise KeyboardInterrupt from None
            raise
        finally:
            _restore_sigint(previous)

    def poll(self):
        return self.p.poll()

    @property
    def returncode(self):
        return self.p.returncode

    def stop(self, grace: float = STOP_GRACE_S) -> list:
        """Graceful to the daemon, then kill whatever of the tree is left. Returns what is STILL alive
        (empty = all gone). A Ctrl+C during the stop is not lost: it sets `interrupted` (Codex supervise
        R5 #1) and the stop completes."""
        self.interrupted = False
        previous = _catch_sigint(self)
        try:
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
            return self.kill_rest()
        finally:
            _restore_sigint(previous)

    def kill_rest(self) -> list:
        """Kill every remaining process of the tree and verify; returns the survivors (a process stuck in
        uninterruptible I/O ignores even SIGKILL until it returns — the caller must not start a new daemon
        while any is left: R5 #2)."""
        if self.job:
            if self.job.h:                                      # a closed job = already verified empty
                try:
                    self.job.kill_all()                         # every descendant, wherever its group
                    left = self.job.active()
                except OSError as e:                            # unknown = not empty: keep the gate closed
                    return [f"không đọc được job: {e}"]
                if left:
                    return [f"{left} tiến trình trong job"]
                self.job.close()
        else:
            # recorded while it ran + everything below this supervisor (Linux subreaper: also a crashed
            # daemon's orphans) + env-marked; repeated until a scan finds nothing (R4 #1)
            survivors = _kill_until_empty(self)
            if survivors:
                return [str(pid) for pid in survivors]
        if self.p.poll() is None:
            proc.kill_tree(self.p)
        try:
            self.p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            return [f"pid {self.p.pid}"]
        return []

    def alive(self) -> int:
        """How many processes of the tree still run (Windows: the job's count)."""
        if self.job:
            return self.job.active()
        return int(self.p.poll() is None) + len(_descendants(self.p.pid) or [])


TREE_ENV = "TOOLKIT_SUPERVISE_TREE"


def _marked(marker: str):
    """POSIX: pids whose environment carries TREE_ENV=<marker> (Linux /proc, else `ps eww`), or None when
    the process list itself can not be read."""
    needle, found = f"{TREE_ENV}={marker}", []
    if os.path.isdir("/proc"):
        try:
            entries = os.listdir("/proc")
        except OSError:
            return None
        for d in entries:
            if not d.isdigit() or int(d) == os.getpid():
                continue
            try:
                with open(f"/proc/{d}/environ", "rb") as f:
                    if needle.encode() in f.read().split(b"\0"):
                        found.append(int(d))
            except OSError:
                continue                                   # gone meanwhile / not ours to read
        return found
    try:
        r = subprocess.run(["ps", "-A", "-ww", "-E", "-o", "pid=,command="], capture_output=True, text=True,
                           timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    for line in r.stdout.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and parts[0].isdigit() and int(parts[0]) != os.getpid() and needle in parts[1].split():
            found.append(int(parts[0]))
    return found


def _reap_adopted(wait: float = 5) -> None:
    """Collect exit statuses of children adopted as subreaper (only processes that are already gone)."""
    if os.name == "nt":
        return
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            time.sleep(0.05)


def _kill_until_empty(tree, wait: float = 15) -> list:
    """POSIX: discover (below the supervisor + env-marked) and SIGKILL, AGAIN AND AGAIN until a complete
    scan finds nothing — a process killed late may have started another child after the previous scan.
    Only processes proven to be OURS are signalled: a recorded pid must still carry the tree's env marker
    or be below the supervisor (a pid may have been reused — Codex supervise R7 #1). A scan that FAILS
    (ps / proc unreadable) is not "empty": the survivors list says so and the restart gate stays shut (R7 #2)."""
    deadline = time.monotonic() + wait
    while True:
        below, marked = _descendants(os.getpid()), _marked(tree.marker)
        if below is None or marked is None:
            if time.monotonic() >= deadline:
                return ["không liệt kê được tiến trình (ps / /proc lỗi)"]
            time.sleep(0.5)
            continue
        ours = (set(below) | set(marked)) - {os.getpid()}
        alive = [pid for pid in ours if _signal(pid, signal.SIGKILL)]
        _reap_adopted(wait=0.2)                            # Linux subreaper: no zombies (R3 #2)
        if not alive:
            return []
        if time.monotonic() >= deadline:
            return [str(pid) for pid in sorted(alive)]
        time.sleep(0.1)


def _signal(pid: int, sig) -> bool:
    try:
        os.kill(pid, sig)
        return True
    except OSError:
        return False


def _catch_sigint(tree):
    """During a stop, Ctrl+C is RECORDED on the tree instead of raising (cleanup must finish)."""
    def flag(*_):
        tree.interrupted = True
    try:
        previous = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, flag)
        return previous
    except ValueError:
        return None                                                 # not the main thread (tests)


def _restore_sigint(previous) -> None:
    if previous is not None:
        signal.signal(signal.SIGINT, previous)


def _nt_resume(p) -> None:
    import ctypes
    from ctypes import wintypes
    f = ctypes.WinDLL("ntdll").NtResumeProcess
    f.argtypes, f.restype = [wintypes.HANDLE], ctypes.c_long
    if f(int(p._handle)) != 0:
        raise OSError("NtResumeProcess failed")


def _become_subreaper() -> None:
    """Linux: orphaned descendants re-parent to this process instead of init (PR_SET_CHILD_SUBREAPER)."""
    if not sys.platform.startswith("linux"):
        return
    try:
        import ctypes
        ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0)
    except (OSError, AttributeError):
        pass


def _descendants(pid: int):
    """POSIX: every process whose ancestry reaches `pid` (from `ps`, at call time), or None when the
    process list can not be read."""
    try:
        r = subprocess.run(["ps", "-A", "-o", "pid=,ppid="], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    kids = {}
    for line in r.stdout.splitlines():
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
        # full signatures: an undeclared argument is passed as a C int and would cut a 64-bit HANDLE (R6 #1)
        H, BOOL, DWORD = wintypes.HANDLE, wintypes.BOOL, wintypes.DWORD
        for fn, args, res in (("CreateJobObjectW", [ctypes.c_void_p, wintypes.LPCWSTR], H),
                              ("SetInformationJobObject", [H, ctypes.c_int, ctypes.c_void_p, DWORD], BOOL),
                              ("AssignProcessToJobObject", [H, H], BOOL),
                              ("TerminateJobObject", [H, wintypes.UINT], BOOL),
                              ("QueryInformationJobObject", [H, ctypes.c_int, ctypes.c_void_p, DWORD, ctypes.c_void_p], BOOL),
                              ("CloseHandle", [H], BOOL)):
            f = getattr(k32, fn)
            f.argtypes, f.restype = args, res
        self.h = k32.CreateJobObjectW(None, None)
        if not self.h:
            raise OSError(ctypes.get_last_error(), "CreateJobObject failed")
        info = Extended()
        info.Basic.LimitFlags = 0x2000                              # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k32.SetInformationJobObject(self.h, 9, ctypes.byref(info), ctypes.sizeof(info)):
            err = ctypes.get_last_error()
            self.close()                                           # no handle leak per retry (R4 #2)
            raise OSError(err, "SetInformationJobObject failed")

    def assign(self, p) -> None:
        if not self.k32.AssignProcessToJobObject(self.h, int(p._handle)):
            raise OSError(self.ct.get_last_error(), "AssignProcessToJobObject failed")

    def active(self) -> int:
        """Processes still in the job. A failed query RAISES — it is never read as an empty job (R6 #1)."""
        acc = self.Accounting()
        if not self.k32.QueryInformationJobObject(self.h, 1, self.ct.byref(acc), self.ct.sizeof(acc), None):
            raise OSError(self.ct.get_last_error(), "QueryInformationJobObject failed")
        return int(acc.ActiveProcesses)

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

def _finish(tree, name: str, sleep, interrupted: bool = False) -> bool:
    """Stop the tree and do not return while any of it is alive (no new daemon next to a survivor of
    the old one — R5 #2): survivors are reported and killed again every 5 s. Returns True when the user
    pressed Ctrl+C (during the stop or the wait): the supervisor must exit (R5 #1)."""
    left = tree.stop()
    interrupted = interrupted or getattr(tree, "interrupted", False)
    while left:
        msg = "không dừng hẳn được: " + ", ".join(left)[:200]
        _say(name, msg + (" — thoát (Ctrl+C)" if interrupted else " — thử lại sau 5 s, chưa chạy bản mới"))
        dc.health_log(name, "cleanup_blocked", msg)
        if interrupted:
            break
        try:
            sleep(5)
        except KeyboardInterrupt:
            interrupted = True
            continue
        left = tree.kill_rest()
    return interrupted


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
            if tree is not None and tree.interrupted:              # Ctrl+C while it was starting
                raise KeyboardInterrupt
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
            if tree is not None and _finish(tree, name, sleep):  # the WHOLE tree, also after a crash (R1 #1)
                dc.health_log(name, "supervisor_stopped", "Ctrl+C")
                return 0
        except KeyboardInterrupt:
            _say(name, "Ctrl+C — dừng")
            if tree is not None:
                tree.interrupted = True
                _finish(tree, name, sleep, interrupted=True)
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
