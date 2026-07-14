#!/usr/bin/env python3
"""Cross-platform environment check for the Agent Skill Toolkit.

Reports the OS, Python, and which external CLIs are available, then maps that to
which toolkit features are ready. Works on Windows, macOS and Ubuntu/Linux.

Most tools are pure stdlib (urllib/socket) and run anywhere; a few wrap native
CLIs (mvn/npm/... for test_runner, a terminal emulator for fork_terminal) — this
tells you what's missing per machine. probe_db now talks MySQL & Postgres over a
socket directly, so psql/mysql are optional.

Zero external dependencies.

Usage:
    python doctor.py            # human-readable summary
    python doctor.py --json     # machine-readable JSON
"""

import argparse
import json
import os
import platform
import shutil
import sys

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

try:
    import config  # dev-automation config loader (.env)
except ImportError:
    config = None

# CLI -> what it unlocks. Optional ones don't block core usage.
CLI_CHECKS = {
    "git": "branch/commit/push in project repos",
    "mvn": "test_runner (Maven projects)",
    "gradle": "test_runner (Gradle projects)",
    "npm": "test_runner (Node projects)",
    "node": "Node runtime",
    "go": "test_runner (Go projects)",
    "pytest": "test_runner (Python projects)",
    "psql": "optional — probe_db --engine postgres uses a stdlib socket client (no CLI needed)",
    "mysql": "optional — probe_db --engine mysql uses a stdlib socket client (no CLI needed)",
    "kcat": "Kafka CLI (optional; probe_kafka uses REST/HTTP instead)",
    "docker": "local container builds (usually done on CI)",
    "kubectl": "k8s ops (usually done on CI)",
}

# Tools that are pure stdlib (urllib/socket/http) — run on any OS, need only a token.
STDLIB_TOOLS = [
    "azure_devops.py", "gitlab_api.py", "notifier.py",        # dev-automation core
    "probe_api.py", "probe_redis.py", "probe_kafka.py",        # stack-verify (HTTP/socket)
    "probe_db.py (mysql + postgres via socket — no CLI)",
    "jenkins.py", "kafka_ui.py", "flow_check.py",
    "run_log.py", "postman_gen.py", "test_runner.py",
    "atask-automation/*",
]


def _terminal_for_os() -> dict:
    """fork_terminal needs an OS-appropriate launcher."""
    system = platform.system()
    if system == "Windows":
        return {"system": system, "needs": "cmd (built-in)", "ok": True}
    if system == "Darwin":
        return {"system": system, "needs": "osascript (built-in)", "ok": bool(shutil.which("osascript"))}
    term = next((t for t in ("gnome-terminal", "x-terminal-emulator", "xterm") if shutil.which(t)), None)
    return {"system": system, "needs": "gnome-terminal | x-terminal-emulator | xterm",
            "found": term, "ok": term is not None}


def collect() -> dict:
    clis = {}
    for name, purpose in CLI_CHECKS.items():
        path = shutil.which(name)
        clis[name] = {"found": path is not None, "path": path, "unlocks": purpose}

    env_report = {}
    if config is not None:
        try:
            env_report["dev_automation_missing"] = config.validate()  # azure/gitlab keys
        except Exception as e:  # noqa: BLE001
            env_report["error"] = str(e)
        env_report["jenkins_configured"] = bool(config.get("JENKINS_URL") and config.get("JENKINS_TOKEN"))
        env_report["kafka_ui_configured"] = bool(config.get("KAFKA_UI_URL") and config.get("KAFKA_UI_USER"))

    return {
        "os": {"system": platform.system(), "release": platform.release(),
               "machine": platform.machine()},
        "python": {"version": platform.python_version(), "executable": sys.executable,
                   "stdout_encoding": getattr(sys.stdout, "encoding", None)},
        "stdlib_tools_always_ok": STDLIB_TOOLS,
        "clis": clis,
        "fork_terminal": _terminal_for_os(),
        "env": env_report,
    }


def _human(report: dict) -> str:
    o, p = report["os"], report["python"]
    lines = [
        f"OS      : {o['system']} {o['release']} ({o['machine']})",
        f"Python  : {p['version']}  [{p['stdout_encoding']}]  {p['executable']}",
        "",
        "Tools chạy mọi OS (chỉ cần stdlib + token): OK",
        "",
        "CLI ngoài (cho tính năng cần wrap CLI):",
    ]
    for name, info in report["clis"].items():
        mark = "✓" if info["found"] else "·"
        lines.append(f"  {mark} {name:<8} {'' if info['found'] else '(thiếu) '}→ {info['unlocks']}")
    ft = report["fork_terminal"]
    lines += ["", f"fork_terminal ({ft['system']}): {'OK' if ft['ok'] else 'THIẾU terminal: ' + ft['needs']}"]
    env = report.get("env") or {}
    if "dev_automation_missing" in env:
        miss = env["dev_automation_missing"]
        lines += ["", "Cấu hình .env:",
                  f"  dev-automation (Azure/GitLab): {'OK' if not miss else 'thiếu ' + ', '.join(miss)}",
                  f"  Jenkins: {'OK' if env.get('jenkins_configured') else 'chưa cấu hình'}",
                  f"  Kafka UI: {'OK' if env.get('kafka_ui_configured') else 'chưa cấu hình'}"]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Cross-platform environment check")
    parser.add_argument("--json", action="store_true", help="output JSON")
    args = parser.parse_args()
    report = collect()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(_human(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
