#!/usr/bin/env python3
"""Keep a daemon running: restart it when it exits, and when the toolkit code or config changes.

    python supervise.py --name Coordinator -- python -u ../../auto-dev/tools/coordinator.py work
    python supervise.py --name group_watch --no-reload -- python -u ../../tchat-automation/tools/group_watch.py

  * the child exits (crash, fatal error, killed)  -> restart after 2 s, 4 s, 8 s … 60 s (reset once it has
    run 5 minutes); every exit / restart is logged to temp/daemon_health.jsonl (`daemon_common.py status`)
  * a new commit (git HEAD), .env or work/agents.json changes -> the child is stopped gracefully and
    restarted at once, so a long-running tab never keeps running old code / old tokens (`--no-reload` off)
  * Ctrl+C in the tab stops the child and the supervisor (the child runs in its own process group, so
    the supervisor decides how it stops: graceful first, then the whole tree is killed)

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
STOP_GRACE_S = 20.0            # graceful stop, then the tree is killed


def _say(name: str, msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] [supervise:{name}] {msg}", flush=True)


def fingerprint(root: str) -> tuple:
    """What a restart must pick up: the commit the code is at, and the config files' mtimes."""
    try:
        head = subprocess.run(["git", "-C", root, "rev-parse", "HEAD"], capture_output=True, text=True,
                              timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        head = ""
    stamps = []
    for rel in (".env", os.path.join("work", "agents.json")):
        try:
            stamps.append(os.path.getmtime(os.path.join(root, rel)))
        except OSError:
            stamps.append(None)
    return (head, *stamps)


def stop(p: subprocess.Popen, grace: float = STOP_GRACE_S) -> None:
    """Ask the child's process group to stop (CTRL_BREAK on Windows, SIGTERM elsewhere); kill the whole
    tree if it is still alive after `grace` seconds."""
    if p.poll() is not None:
        return
    try:
        if os.name == "nt":
            p.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(os.getpgid(p.pid), signal.SIGTERM)
    except (OSError, ValueError):
        pass
    try:
        p.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        proc.kill_tree(p)


def run(cmd: list, name: str, reload: bool = True, root: str = "", max_restarts: int = -1,
        sleep=time.sleep) -> int:
    """Supervise `cmd` until Ctrl+C (or `max_restarts` restarts, for tests). Returns 0."""
    root = root or paths.toolkit_root()
    backoff, restarts = BACKOFF_START, 0
    dc.health_log(name, "supervisor_started", " ".join(cmd)[:300])
    while True:
        seen = fingerprint(root) if reload else None
        started = time.monotonic()
        _say(name, "chạy: " + " ".join(cmd))
        p = subprocess.Popen(cmd, **proc.popen_group_kwargs())
        reason, last_watch = None, time.monotonic()
        try:
            while reason is None:
                if p.poll() is not None:
                    reason = f"thoát mã {p.returncode}"
                    break
                if reload and time.monotonic() - last_watch >= WATCH_EVERY_S:
                    last_watch = time.monotonic()
                    now = fingerprint(root)
                    if now != seen:
                        reason = "code/cấu hình đổi"
                        _say(name, "phát hiện commit mới hoặc .env / agents.json đổi — khởi động lại")
                        stop(p)
                        break
                sleep(POLL_S)
        except KeyboardInterrupt:
            _say(name, "Ctrl+C — dừng")
            stop(p)
            dc.health_log(name, "supervisor_stopped", "Ctrl+C")
            return 0
        ran = time.monotonic() - started
        if reason == "code/cấu hình đổi":
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
    ap.add_argument("--no-reload", action="store_true", help="do not restart on code/config changes")
    a = ap.parse_args(argv[:i])
    cmd = argv[i + 1:]
    if not cmd:
        print("supervise.py: missing command after --", file=sys.stderr)
        return 2
    script = next((x for x in cmd if x.endswith(".py")), cmd[0])
    return run(cmd, a.name or os.path.splitext(os.path.basename(script))[0], reload=not a.no_reload)


if __name__ == "__main__":
    sys.exit(main())
