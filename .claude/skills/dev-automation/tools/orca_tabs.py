#!/usr/bin/env python3
"""Keep the toolkit daemons as VISIBLE Orca terminal tabs: (re)open a tab for every daemon whose
`supervise.py --name <name>` process is not running — after Orca starts, or when a tab was closed.
    python orca_tabs.py list                 # the tabs of work/daemon_tabs.json + running or not
    python orca_tabs.py ensure [--dry-run]   # open the missing ones (Orca not running -> nothing to do)
Meant to run every minute from the OS scheduler (systemd --user timer / Task Scheduler): Orca has no
"on startup" hook, so the tabs come back within a minute of Orca being reachable.

work/daemon_tabs.json is MACHINE-SPECIFIC (gitignored; not in secret_sync's default paths):
    {"worktree": "id:<repoId>::<toolkit path>",
     "tabs": [{"name": "Coordinator", "command": "python3 -u .claude/skills/auto-dev/tools/coordinator.py work"},
              {"name": "dashboard", "command": "python3 -u .../dashboard.py serve --port 8765", "reload": false}]}
Each tab runs `supervise.py --name <name> [--no-reload] -- <command>` from the toolkit root, so the
supervisor restarts the daemon inside the tab; this tool only replaces a tab whose supervisor is gone.
Liveness = a process whose argv has supervise.py and `--name <name>` — never the tab title, so a daemon
started by hand (or in a restored tab) is not doubled. All Orca calls go through orca_adapter.
"""

import argparse
import json
import os
import shlex
import subprocess
import sys

import orca_adapter as orca
import probe_common as pc

_HERE = os.path.dirname(os.path.abspath(__file__))
SUPERVISE = ".claude/skills/dev-automation/tools/supervise.py"


def _repo_root() -> str:
    return os.path.abspath(os.path.join(_HERE, "..", "..", "..", ".."))


def config_path() -> str:
    return os.path.join(os.environ.get("WORK_DIR") or os.path.join(_repo_root(), "work"), "daemon_tabs.json")


def load_config() -> dict:
    with open(config_path(), encoding="utf-8") as f:
        cfg = json.load(f)
    if not cfg.get("worktree") or not isinstance(cfg.get("tabs"), list):
        raise ValueError(f"{config_path()}: needs 'worktree' and a 'tabs' list")
    for t in cfg["tabs"]:
        if not t.get("name") or not t.get("command"):
            raise ValueError(f"{config_path()}: every tab needs 'name' and 'command'")
    return cfg


def _cmdlines() -> list:
    """argv of every process, as lists of strings (Linux /proc, else `ps`, Windows via PowerShell)."""
    if os.path.isdir("/proc"):
        out = []
        for d in os.listdir("/proc"):
            if not d.isdigit():
                continue
            try:
                with open(f"/proc/{d}/cmdline", "rb") as f:
                    raw = f.read()
            except OSError:
                continue
            if raw:
                out.append([a.decode("utf-8", "replace") for a in raw.rstrip(b"\0").split(b"\0")])
        return out
    if os.name == "nt":
        ps = ["powershell", "-NoProfile", "-Command",
              "Get-CimInstance Win32_Process | ForEach-Object { $_.CommandLine }"]
        text = subprocess.run(ps, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=30, stdin=subprocess.DEVNULL).stdout
        return [shlex.split(ln, posix=False) for ln in text.splitlines() if ln.strip()]
    text = subprocess.run(["ps", "-axww", "-o", "args="], capture_output=True, text=True,
                          errors="replace", timeout=30, stdin=subprocess.DEVNULL).stdout
    return [shlex.split(ln) for ln in text.splitlines() if ln.strip()]


def running(cmdlines: list) -> set:
    """Names of the supervisors alive in `cmdlines`."""
    names = set()
    for argv in cmdlines:
        argv = [a.strip('"') for a in argv]
        if not any(os.path.basename(a) == "supervise.py" for a in argv):
            continue
        for i, a in enumerate(argv[:-1]):
            if a == "--name":
                names.add(argv[i + 1])
                break
    return names


def tab_command(tab: dict) -> str:
    """The line typed into the tab's shell: POSIX sh on Linux/macOS, PowerShell (Orca's default) on Windows."""
    reload_flag = "" if tab.get("reload", True) else " --no-reload"
    if os.name == "nt":
        return f"$env:PYTHONUTF8=1; python -u {SUPERVISE} --name '{tab['name']}'{reload_flag} -- {tab['command']}"
    return f"PYTHONUTF8=1 python3 -u {SUPERVISE} --name {shlex.quote(tab['name'])}{reload_flag} -- {tab['command']}"


def list_tabs(cfg: dict) -> dict:
    alive = running(_cmdlines())
    return {"config": config_path(), "worktree": cfg["worktree"],
            "tabs": [{"name": t["name"], "running": t["name"] in alive} for t in cfg["tabs"]]}


def ensure(cfg: dict, dry_run: bool = False) -> dict:
    if not orca.available():
        return {"orca": "not running", "opened": [], "running": []}
    alive = running(_cmdlines())
    opened, failed = [], []
    for t in cfg["tabs"]:
        if t["name"] in alive:
            continue
        if dry_run:
            opened.append({"name": t["name"], "command": tab_command(t), "dry_run": True})
            continue
        try:
            res = orca.terminal_create(cfg["worktree"], t["name"], tab_command(t))
            opened.append({"name": t["name"], "handle": (res.get("terminal") or {}).get("handle")})
        except orca.OrcaError as e:
            failed.append({"name": t["name"], "code": e.code, "message": e.message})
    out = {"orca": "ready", "running": sorted(alive & {t["name"] for t in cfg["tabs"]}), "opened": opened}
    if failed:
        out.update(error=True, failed=failed)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Reopen missing toolkit daemon tabs in Orca")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    e = sub.add_parser("ensure")
    e.add_argument("--dry-run", action="store_true", help="show what would open, open nothing")
    a = ap.parse_args(argv)
    try:
        cfg = load_config()
    except (OSError, ValueError) as ex:
        return pc.emit({"error": True, "message": str(ex)})
    return pc.emit(list_tabs(cfg) if a.cmd == "list" else ensure(cfg, a.dry_run))


if __name__ == "__main__":
    sys.exit(main())
