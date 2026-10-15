#!/usr/bin/env python3
"""SSH fan-out over LAN machines, driven by a host registry.

Reads work/hosts.json (override with WORK_DIR), shape:
{
  "may-build":  {"host": "192.168.1.20", "user": "chung", "port": 22},
  "nas":        {"host": "192.168.1.30", "user": "admin", "os": "linux"},
  "win-test":   {"host": "192.168.1.40", "user": "chung", "shell": "powershell"}
}

Auth is key-based: we call the system `ssh` with BatchMode=yes, so a host that
isn't set up for key auth fails fast instead of hanging on a password prompt.

Only hosts present in the registry can be targeted (allowlist). Commands are
classified for risk so the caller (or the Telegram approval hook) can gate the
dangerous ones.

CLI:
    python ssh_exec.py list
    python ssh_exec.py ping <alias>
    python ssh_exec.py run <alias> "<command>" [--dry-run] [--timeout 60]
    python ssh_exec.py classify "<command>"
"""

import json
import os
import re
import subprocess
import sys

import rc_config as cfg

# Patterns that should NEVER auto-run without an explicit human OK.
DANGER_PATTERNS = [
    r"\brm\s+-[rf]{1,2}\b", r"\brm\s+-[a-z]*f", r"\bmkfs\b", r"\bdd\s+if=",
    r">\s*/dev/sd", r":\(\)\s*\{", r"\bshutdown\b", r"\breboot\b",
    r"\bhalt\b", r"\bpoweroff\b", r"\bformat\b", r"\bdiskpart\b",
    r"\bdel\s+/[sq]", r"\brmdir\s+/s", r"Remove-Item.+-Recurse",
    r"\bchmod\s+-R\b", r"\bchown\s+-R\b", r"\b(drop|truncate)\s+(table|database)\b",
    r"\biptables\b", r"\bufw\s+", r"\bkill(all)?\s+-9\b",
    r">\s*/etc/", r"\bcrontab\s+-r\b", r"\buserdel\b", r"\bgpg\b.*--delete",
]

# Heuristic: commands that only read state -> low risk.
READONLY_HINTS = [
    r"^\s*(ls|dir|cat|type|tail|head|less|more|pwd|whoami|hostname|uptime|"
    r"date|df|du|free|ps|top|systemctl\s+status|service\s+\S+\s+status|"
    r"docker\s+(ps|images|logs|stats)|kubectl\s+get|git\s+(status|log|diff|branch|show)|"
    r"echo|env|printenv|uname|ip\s+a|ifconfig|netstat|ss|ping|nslookup|"
    r"Get-\w+|grep|find|stat|wc|which|where)\b",
]


def classify(command: str) -> str:
    """Return 'danger' | 'write' | 'read' for a command string."""
    for pat in DANGER_PATTERNS:
        if re.search(pat, command, re.IGNORECASE):
            return "danger"
    for pat in READONLY_HINTS:
        if re.search(pat, command.strip(), re.IGNORECASE):
            return "read"
    return "write"


def _registry_path() -> str:
    return os.path.join(cfg.work_dir(), "hosts.json")


def load_hosts() -> dict:
    path = _registry_path()
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except ValueError:
        return {}


def _resolve(alias: str) -> dict:
    hosts = load_hosts()
    if alias not in hosts:
        raise KeyError(alias)
    return hosts[alias]


def _ssh_argv(spec: dict, command: str, timeout: int) -> list:
    user = spec.get("user", "")
    host = spec["host"]
    target = f"{user}@{host}" if user else host
    argv = [
        "ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
        "-o", f"ConnectTimeout={min(timeout, 20)}",
    ]
    if spec.get("port"):
        argv += ["-p", str(spec["port"])]
    if spec.get("key"):
        argv += ["-i", os.path.expanduser(spec["key"])]
    argv += [target, command]
    return argv


def run(alias: str, command: str, dry_run: bool = False,
        timeout: int = 60) -> dict:
    """Run a command on a registered host over SSH. Returns a JSON-able dict."""
    try:
        spec = _resolve(alias)
    except KeyError:
        return {"error": True, "message": f"host '{alias}' không có trong registry "
                f"({_registry_path()}). Chạy `list` để xem hosts."}

    risk = classify(command)
    argv = _ssh_argv(spec, command, timeout)
    result = {
        "host": alias, "address": spec.get("host"),
        "command": command, "risk": risk,
        "argv": " ".join(argv[:-1]) + f" '{command}'",
    }
    if dry_run:
        result["dry_run"] = True
        return result

    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout + 10,
            encoding="utf-8", errors="replace")
        result.update({
            "returncode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        })
    except FileNotFoundError:
        result.update({"error": True, "message": "không tìm thấy `ssh` trên PATH "
                       "(cài OpenSSH client)."})
    except subprocess.TimeoutExpired:
        result.update({"error": True, "message": f"timeout sau {timeout}s."})
    return result


def ping(alias: str) -> dict:
    """Cheap connectivity check: run `echo ok` over SSH."""
    return run(alias, "echo ok", timeout=15)


def _print(data):
    sys.stdout.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(1)
    cmd = args[0].lower()

    if cmd == "list":
        hosts = load_hosts()
        if not hosts:
            print(f"Registry trống/không tồn tại: {_registry_path()}")
            print("Tạo work/hosts.json theo mẫu hosts.sample.json trong skill.")
            sys.exit(1)
        _print(hosts)
    elif cmd == "classify" and len(args) >= 2:
        _print({"command": args[1], "risk": classify(args[1])})
    elif cmd == "ping" and len(args) >= 2:
        _print(ping(args[1]))
    elif cmd == "run" and len(args) >= 3:
        dry = "--dry-run" in args
        timeout = 60
        if "--timeout" in args:
            timeout = int(args[args.index("--timeout") + 1])
        _print(run(args[1], args[2], dry_run=dry, timeout=timeout))
    else:
        print(__doc__)
        sys.exit(1)
