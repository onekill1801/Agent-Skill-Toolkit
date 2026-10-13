"""Subprocess helpers that never leave a process tree behind (agents and builds spawn children)."""

import os
import signal
import subprocess


def popen_group_kwargs() -> dict:
    """Popen kwargs that put the child in its own group, so kill_tree() can take the whole tree."""
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def kill_tree(p, wait: float = 10) -> None:
    """Kill a process AND its descendants, then reap with a bounded wait; close pipes a detached
    grandchild may still hold so the caller never blocks on them."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True, timeout=30)
        else:
            os.killpg(os.getpgid(p.pid), signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        p.kill()
    except OSError:
        pass
    try:
        p.communicate(timeout=wait)
    except (subprocess.TimeoutExpired, ValueError):
        for f in (p.stdin, p.stdout, p.stderr):
            try:
                f and f.close()
            except OSError:
                pass


def run(argv, *, input=None, timeout=None, cwd=None, env=None, should_stop=None, poll=0.5):
    """subprocess.run() replacement: drains pipes, kills the TREE on timeout or when
    should_stop() turns True. Raises subprocess.TimeoutExpired / InterruptedError respectively."""
    import time
    p = subprocess.Popen(argv, stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                         errors="replace", cwd=cwd, env=env, **popen_group_kwargs())
    deadline = time.time() + timeout if timeout else None
    pending = input
    while True:
        try:
            out, err = p.communicate(input=pending, timeout=poll)
            return subprocess.CompletedProcess(argv, p.returncode, out, err)
        except subprocess.TimeoutExpired:
            pending = None
            if should_stop and should_stop():
                kill_tree(p)
                raise InterruptedError(f"{argv[0]} stopped")
            if deadline and time.time() > deadline:
                kill_tree(p)
                raise subprocess.TimeoutExpired(argv, timeout)
