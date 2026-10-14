#!/usr/bin/env python3
"""Single entry point for every call the toolkit makes to the `orca` CLI.

Orca ships fast; when its CLI changes, only this file should need edits. Every
call uses `--json` and returns the parsed `result` (or raises OrcaError).
Orca is OPTIONAL: callers check `available()` and fall back when it is not.

Binary lookup: $ORCA_BIN > PATH `orca` > per-OS CLI shim installed by the app.

Usage (debug):
    python orca_adapter.py status
    python orca_adapter.py automation-find "Auto review MR"
    python orca_adapter.py automation-enabled <id>
"""

import json
import os
import platform
import shutil
import subprocess
import sys

import probe_common as pc  # noqa: F401  (UTF-8 stdout on Windows)

DEFAULT_TIMEOUT = 30


class OrcaError(Exception):
    def __init__(self, message: str, code: str = "orca"):
        super().__init__(message)
        self.message, self.code = message, code


def _shim_candidates() -> list:
    home = os.path.expanduser("~")
    system = platform.system()
    if system == "Windows":
        local = os.environ.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
        roaming = os.environ.get("APPDATA") or os.path.join(home, "AppData", "Roaming")
        # .exe only: a .cmd/.bat shim is run by cmd.exe, which re-parses arguments, so text
        # we pass through (MR titles, comments) could inject commands.
        return [os.path.join(local, "Programs", "orca", "resources", "bin", "orca.exe"),
                os.path.join(roaming, "orca", "windows-orca-cli-shim", "orca.exe")]
    if system == "Darwin":
        return [os.path.join(home, "Library", "Application Support", "orca", "macos-orca-cli-shim", "orca"),
                "/Applications/Orca.app/Contents/Resources/bin/orca"]
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
    return [os.path.join(base, "orca", "linux-orca-cli-shim", "orca")]


def _is_batch(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in (".cmd", ".bat")


def _safe_exe(path: str) -> str:
    """Return `path`, or its sibling orca.exe when `path` is a batch shim; '' if only a shim exists."""
    if not path or not _is_batch(path):
        return path
    exe = os.path.join(os.path.dirname(path), "orca.exe")
    return exe if os.path.isfile(exe) else ""


def find_bin() -> str:
    if os.environ.get("ORCA_BIN"):
        exe = _safe_exe(os.environ["ORCA_BIN"])
        if not exe:
            raise OrcaError("ORCA_BIN points to a .cmd/.bat shim with no orca.exe beside it; "
                            "point it at orca.exe", "unsafe")
        return exe
    exe = _safe_exe(shutil.which("orca") or "")
    if exe:
        return exe
    for c in _shim_candidates():
        if os.path.isfile(c):
            return c
    return ""


def call(*args, timeout: int = DEFAULT_TIMEOUT) -> dict:
    """Run `orca <args> --json`; return the `result` object or raise OrcaError."""
    exe = find_bin()
    if not exe:
        raise OrcaError("orca CLI not found (ORCA_BIN / PATH / app shim)", "missing")
    argv = [exe, *[str(a) for a in args], "--json"]
    try:
        p = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise OrcaError(f"orca {args[0]} timed out after {timeout}s", "timeout")
    except OSError as e:
        raise OrcaError(f"cannot run orca: {e}", "missing")
    try:
        out = json.loads(p.stdout or "{}")
    except ValueError:
        raise OrcaError((p.stderr or p.stdout or f"orca exited {p.returncode}").strip()[:500], "output")
    if not out.get("ok", p.returncode == 0):
        err = out.get("error") or {}
        raise OrcaError(err.get("message") or str(err) or "orca error", err.get("code", "orca"))
    return out.get("result", out)


def available() -> bool:
    """True when the CLI exists AND the Orca runtime is reachable."""
    try:
        st = status()
    except (OrcaError, OSError, subprocess.TimeoutExpired):
        return False
    rt = st.get("runtime")
    if isinstance(rt, dict):  # --json shape: {"runtime": {"reachable": true}}
        return bool(rt.get("reachable"))
    return bool(st.get("runtimeReachable"))  # text shape


def status() -> dict:
    exe = find_bin()
    if not exe:
        raise OrcaError("orca CLI not found", "missing")
    p = subprocess.run([exe, "status", "--json"], capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=15, stdin=subprocess.DEVNULL)
    try:
        out = json.loads(p.stdout or "{}")
        return out.get("result", out)
    except ValueError:  # older CLIs print `key: value` lines
        res = {}
        for ln in (p.stdout or "").splitlines():
            if ":" in ln:
                k, v = ln.split(":", 1)
                res[k.strip()] = {"true": True, "false": False}.get(v.strip(), v.strip())
        return res


# ------------------------------------------------------------------ accounts

def account_list() -> dict:
    """`orca account list`: managed Claude/Codex accounts (email, org — no tokens) + rateLimits
    (the active account's live usage, `inactiveClaudeAccounts` = Orca's last cached reading)."""
    return call("account", "list")


# ------------------------------------------------------------------ automations

def automation_list() -> list:
    return call("automations", "list").get("automations", [])


def automation_find(name: str) -> dict:
    return next((a for a in automation_list() if a.get("name") == name), {})


def automation_enabled(automation_id: str) -> bool:
    res = call("automations", "show", automation_id)
    a = res.get("automation", res)
    return bool(a.get("enabled"))


def automation_run(automation_id: str) -> dict:
    return call("automations", "run", automation_id)


def automation_create(name: str, prompt: str, provider: str, trigger: str, *,
                      precheck: str = "", repo: str = "", workspace: str = "",
                      workspace_mode: str = "", enabled: bool = False, timezone: str = "") -> dict:
    args = ["automations", "create", "--name", name, "--prompt", prompt,
            "--provider", provider, "--trigger", trigger]
    if precheck:
        args += ["--precheck", precheck]
    if repo:
        args += ["--repo", repo]
    if workspace:
        args += ["--workspace", workspace]
    if workspace_mode:
        args += ["--workspace-mode", workspace_mode]
    if timezone:
        args += ["--timezone", timezone]
    args += ["--enabled" if enabled else "--disabled"]   # Orca enables new automations by default
    return call(*args)


def automation_set_enabled(automation_id: str, enabled: bool) -> dict:
    return call("automations", "edit", automation_id, "--enabled" if enabled else "--disabled")


# ------------------------------------------------------------------ orchestration

def run_ensure(objective: str, state_file: str) -> str:
    """Return a Run id, creating one the first time (id cached in `state_file`)."""
    try:
        with open(state_file, encoding="utf-8") as f:
            rid = json.load(f).get("run_id", "")
        if rid:
            return rid
    except (OSError, ValueError):
        pass
    rid = call("orchestration", "run-create", "--objective", objective)["run"]["id"]
    os.makedirs(os.path.dirname(state_file), exist_ok=True)
    with open(state_file, "w", encoding="utf-8") as f:
        json.dump({"run_id": rid, "objective": objective}, f)
    return rid


def task_create(run_id: str, title: str, spec: str, display_name: str = "") -> str:
    args = ["orchestration", "task-create", "--run", run_id, "--task-title", title, "--spec", spec]
    if display_name:
        args += ["--display-name", display_name]
    return call(*args)["task"]["id"]


def task_update(run_id: str, task_id: str, status: str, result: dict = None) -> dict:
    """status: pending | ready | dispatched | completed | failed | blocked."""
    args = ["orchestration", "task-update", "--run", run_id, "--id", task_id, "--status", status]
    if result is not None:
        args += ["--result", json.dumps(result, ensure_ascii=False)]
    return call(*args)


def gate_create(task_id: str, question: str, options: list) -> str:
    return call("orchestration", "gate-create", "--task", task_id, "--question", question,
                "--options", json.dumps(options, ensure_ascii=False))["gate"]["id"]


def gate_get(run_id: str, gate_id: str) -> dict:
    gates = call("orchestration", "gate-list", "--run", run_id).get("gates", [])
    return next((g for g in gates if g.get("id") == gate_id), {})


# ------------------------------------------------------------------ worktrees / terminals

def worktree_show(selector: str) -> dict:
    res = call("worktree", "show", "--worktree", selector)
    return res.get("worktree", res)


def worktree_set(selector: str, *, status: str = "", comment: str = "", display_name: str = "") -> dict:
    args = ["worktree", "set", "--worktree", selector]
    if status:
        args += ["--workspace-status", status]
    if comment:
        args += ["--comment", comment]
    if display_name:
        args += ["--display-name", display_name]
    return call(*args)


def terminal_close(handle: str, tab: bool = True) -> dict:
    return call("terminal", "close", "--terminal", handle, *(["--tab"] if tab else []))


def terminal_create(worktree: str, title: str, command: str, focus: bool = False) -> dict:
    args = ["terminal", "create", "--worktree", worktree, "--title", title, "--command", command]
    if focus:
        args += ["--focus"]
    return call(*args)


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    try:
        if not argv or argv[0] == "status":
            out = status()
        elif argv[0] == "automation-find":
            out = automation_find(argv[1])
        elif argv[0] == "automation-enabled":
            out = {"enabled": automation_enabled(argv[1])}
        else:
            out = {"error": True, "message": f"unknown command {argv[0]}"}
    except OrcaError as e:
        out = {"error": True, "code": e.code, "message": e.message}
    return pc.emit(out)


if __name__ == "__main__":
    sys.exit(main())
