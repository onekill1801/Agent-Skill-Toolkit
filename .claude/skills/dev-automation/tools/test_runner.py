#!/usr/bin/env python3
"""Test/build runner for the auto-dev pipeline (Tầng Test).

Runs a project's build/test/lint command and reports a structured PASS/FAIL
verdict so the orchestrator can gate delivery on a green result.

Resolution order for *what* to run:
  1. Explicit --cmd "<shell command>"           (highest priority)
  2. Registry lookup: --project <name> -> reads <work_dir>/projects.json
       and uses its "test_cmd" / "build_cmd" / "lint_cmd" entry
  3. Auto-detect from files in --cwd (pom.xml, package.json, build.gradle, ...)

Where to run: --cwd <dir>, else the project's "clone_dir" from the registry,
else the current working directory.

Zero external dependencies — Python stdlib only.

Kinds: build · test (UNIT, auto-gated) · lint · integration (*IT/Failsafe, MANUAL) ·
frontend (UI unit tests in a monolith, MANUAL). Auto-detected Maven/Gradle commands
prefer the repo's committed wrapper (mvnw/gradlew) when present, to avoid tool-version
drift — as JHipster pins its build tool. Explicit --cmd / registry commands are used
verbatim.

Usage:
    python test_runner.py run --project atask --kind test
    python test_runner.py run --project atask --kind integration   # manual *IT run
    python test_runner.py run --cmd "mvn -B test" --cwd /home/me/work/atask
    python test_runner.py run --cwd . --auto
    python test_runner.py detect --cwd .
    python test_runner.py detect --project atask

Output: a single JSON object on stdout.
    {"passed": true, "exit_code": 0, "kind": "test", "command": "...",
     "cwd": "...", "duration_sec": 12.3, "summary": "...", "log_tail": "..."}
On a FAIL verdict the result also carries an "error_context" field: the failure log
wrapped in an <error_context>...</error_context> tag, ready to hand straight to a
fix-agent (Agent<->Tool comms use strict HTML/XML tags — see
auto-dev/prompts/SYSTEM_PROMPT.md), instead of loose Markdown.
On failure to even start: {"error": true, "message": "..."}
"""

import argparse
import json
import os
import subprocess
import sys
import time

# Cross-platform: force UTF-8 stdout so JSON output doesn't crash on a Windows
# cp1252/cp437 console when build/test logs contain non-ASCII. No-op elsewhere.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

# How many trailing lines of combined output to keep in the JSON result.
LOG_TAIL_LINES = 60
DEFAULT_TIMEOUT_SEC = 1800  # 30 min hard cap so a hung build can't block forever.

# Auto-detection table: marker file in cwd -> default commands per kind.
# Order matters — first marker found wins for a given kind. If the winning
# marker has no command for the requested kind (e.g. a JHipster monolith has
# pom.xml first but the "frontend" command lives with package.json), _detect
# keeps scanning — see below.
#
# Kinds:
#   build / test / lint  — the auto-gated stages (test = UNIT tests only).
#   integration          — slow *IT / Failsafe suite (Testcontainers on JHipster);
#                          MANUAL — not run by the auto gate.
#   frontend             — JS/TS unit tests for the UI in a monolith; MANUAL.
_DETECT = [
    ("pom.xml", {
        "build": "mvn -B -ntp -q -DskipTests package",
        "test": "mvn -B -ntp test",              # Surefire → unit tests (*Test) only
        "lint": "mvn -B -ntp -q checkstyle:check",
        "integration": "mvn -B -ntp verify",     # Failsafe → integration tests (*IT)
    }),
    ("build.gradle", {
        "build": "gradle build -x test",
        "test": "gradle test",
        "lint": "gradle check -x test",
        "integration": "gradle integrationTest",
    }),
    ("build.gradle.kts", {
        "build": "gradle build -x test",
        "test": "gradle test",
        "lint": "gradle check -x test",
        "integration": "gradle integrationTest",
    }),
    ("package.json", {
        "build": "npm run build",
        "test": "npm test",
        "lint": "npm run lint",
        "frontend": "npm test",
    }),
    ("pyproject.toml", {
        "build": "python -m build",
        "test": "pytest -q",
        "lint": "ruff check .",
    }),
    ("requirements.txt", {
        "build": "",
        "test": "pytest -q",
        "lint": "ruff check .",
    }),
    ("go.mod", {
        "build": "go build ./...",
        "test": "go test ./...",
        "lint": "go vet ./...",
    }),
]


def _repo_root() -> str:
    """Walk up from this file to the repo root (where CLAUDE.md / .git lives)."""
    search = os.path.dirname(os.path.abspath(__file__))
    for _ in range(6):
        if os.path.isdir(os.path.join(search, ".git")) or os.path.isfile(os.path.join(search, "CLAUDE.md")):
            return search
        search = os.path.dirname(search)
    return os.path.dirname(os.path.abspath(__file__))


def _work_dir() -> str:
    """Registry dir holding projects.json: $WORK_DIR if set, else <repo>/work."""
    return os.environ.get("WORK_DIR") or os.path.join(_repo_root(), "work")


def _load_registry() -> dict:
    """Read <work_dir>/projects.json, or {} if it does not exist."""
    path = os.path.join(_work_dir(), "projects.json")
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        return {"__error__": f"cannot read {path}: {e}"}


def _project_entry(name: str) -> dict | None:
    reg = _load_registry()
    if "__error__" in reg:
        return None
    return reg.get(name)


def _prefer_wrapper(command: str, cwd: str) -> str:
    """Prefer the project's committed build-tool wrapper over a global binary.

    JHipster (and most modern JVM projects) pin their Maven/Gradle version via a
    wrapper script checked into the repo (`mvnw` / `gradlew`). Using the wrapper
    avoids version drift between the agent's machine and the project's expected
    build. Only rewrites a command that STARTS with the bare tool name, and only
    when the wrapper file actually exists in `cwd`; explicit --cmd / registry
    commands are never touched (see _resolve).
    """
    win = os.name == "nt"
    swaps = [
        # (bare prefix, wrapper file to look for, replacement prefix)
        ("mvn ", "mvnw.cmd" if win else "mvnw", "mvnw.cmd " if win else "./mvnw "),
        ("gradle ", "gradlew.bat" if win else "gradlew", "gradlew.bat " if win else "./gradlew "),
    ]
    for prefix, wrapper_file, repl in swaps:
        if command.startswith(prefix) and os.path.isfile(os.path.join(cwd, wrapper_file)):
            return repl + command[len(prefix):]
    return command


def _detect(cwd: str, kind: str) -> str:
    """Return an auto-detected command for `kind`, or '' if nothing matched.

    A marker that matches but has no command for `kind` does NOT short-circuit —
    scanning continues so a monolith (e.g. pom.xml + package.json) can still
    resolve a package.json-only kind such as `frontend`.
    """
    for marker, cmds in _DETECT:
        if os.path.isfile(os.path.join(cwd, marker)):
            cmd = cmds.get(kind, "")
            if cmd:
                return cmd
            # marker matched but defines no command for this kind — keep looking
    return ""


def _resolve(args) -> tuple[str, str]:
    """Resolve (command, cwd). Raises ValueError with a clear message."""
    entry = _project_entry(args.project) if args.project else None

    # Resolve cwd first so auto-detect can inspect it.
    cwd = args.cwd
    if not cwd and entry:
        cwd = entry.get("clone_dir")
    if not cwd:
        cwd = os.getcwd()
    cwd = os.path.abspath(os.path.expanduser(cwd))
    if not os.path.isdir(cwd):
        raise ValueError(f"cwd does not exist: {cwd}")

    # Resolve command.
    if args.cmd:
        return args.cmd, cwd

    key = f"{args.kind}_cmd"  # e.g. "test_cmd"
    if entry and entry.get(key):
        return entry[key], cwd

    if args.auto or entry is not None or not args.project:
        detected = _detect(cwd, args.kind)
        if detected:
            return _prefer_wrapper(detected, cwd), cwd

    raise ValueError(
        f"no command for kind='{args.kind}'. "
        f"Pass --cmd, add '{key}' to projects.json for '{args.project}', "
        f"or place a known build file in {cwd} for auto-detect."
    )


def _tail(text: str, lines: int = LOG_TAIL_LINES) -> str:
    parts = text.splitlines()
    if len(parts) <= lines:
        return text
    return "...(truncated)...\n" + "\n".join(parts[-lines:])


def _error_context(command: str, cwd: str, detail: str) -> str:
    """Bọc log lỗi runtime vào thẻ <error_context> để ném ngược cho Agent phụ sửa code.

    Giao tiếp Agent↔Tool dùng thẻ HTML/XML nghiêm ngặt (xem
    auto-dev/prompts/SYSTEM_PROMPT.md) thay vì Markdown — Agent phụ đọc thẳng thẻ này
    làm ngữ cảnh sửa, không phải parse Markdown lỏng lẻo.
    """
    return (
        "<error_context>\n"
        f"  <command>{command}</command>\n"
        f"  <cwd>{cwd}</cwd>\n"
        f"  <log>{detail}</log>\n"
        "</error_context>"
    )


def cmd_run(args) -> dict:
    command, cwd = _resolve(args)
    start = time.monotonic()
    try:
        proc = subprocess.run(
            command,
            cwd=cwd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=args.timeout,
        )
    except subprocess.TimeoutExpired:
        return {
            "passed": False,
            "exit_code": None,
            "kind": args.kind,
            "command": command,
            "cwd": cwd,
            "timed_out": True,
            "summary": f"command timed out after {args.timeout}s",
            # Giao tiếp Agent↔Tool: bọc lỗi vào thẻ cho Agent phụ sửa.
            "error_context": _error_context(
                command, cwd, f"command timed out after {args.timeout}s"
            ),
        }
    duration = round(time.monotonic() - start, 1)
    combined = (proc.stdout or "") + (proc.stderr or "")
    passed = proc.returncode == 0
    summary = "PASS" if passed else f"FAIL (exit {proc.returncode})"
    log_tail = _tail(combined)
    result = {
        "passed": passed,
        "exit_code": proc.returncode,
        "kind": args.kind,
        "command": command,
        "cwd": cwd,
        "duration_sec": duration,
        "summary": summary,
        "log_tail": log_tail,
    }
    # Chỉ FAIL mới kèm <error_context> — PASS không cần ngữ cảnh sửa lỗi.
    if not passed:
        result["error_context"] = _error_context(command, cwd, log_tail)
    return result


def cmd_detect(args) -> dict:
    entry = _project_entry(args.project) if args.project else None
    cwd = args.cwd or (entry.get("clone_dir") if entry else None) or os.getcwd()
    cwd = os.path.abspath(os.path.expanduser(cwd))
    result = {"cwd": cwd, "registry_found": entry is not None}
    for kind in ("build", "test", "lint", "integration", "frontend"):
        key = f"{kind}_cmd"
        from_registry = entry.get(key) if entry else None
        detected = _detect(cwd, kind)
        result[kind] = {
            "registry": from_registry or None,
            "detected": _prefer_wrapper(detected, cwd) if detected else None,
        }
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Run project build/test/lint for the auto-dev pipeline.")
    sub = parser.add_subparsers(dest="action", required=True)

    p_run = sub.add_parser("run", help="Run a command and report PASS/FAIL")
    p_run.add_argument("--project", help="Project name to look up in projects.json")
    p_run.add_argument("--cwd", help="Directory to run in (overrides registry clone_dir)")
    p_run.add_argument("--cmd", help="Explicit shell command (overrides registry + auto)")
    p_run.add_argument(
        "--kind", default="test",
        choices=["build", "test", "lint", "integration", "frontend"],
        help="test=unit (auto-gated); integration=*IT/Failsafe and frontend=UI unit are MANUAL",
    )
    p_run.add_argument("--auto", action="store_true", help="Force auto-detect even without a registry entry")
    p_run.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SEC)

    p_det = sub.add_parser("detect", help="Show resolved/detected commands without running")
    p_det.add_argument("--project")
    p_det.add_argument("--cwd")

    args = parser.parse_args()
    try:
        if args.action == "run":
            out = cmd_run(args)
        else:
            out = cmd_detect(args)
    except ValueError as e:
        out = {"error": True, "message": str(e)}

    print(json.dumps(out, ensure_ascii=False, indent=2))
    # Exit non-zero only on hard error; a clean FAIL verdict still exits 0
    # so the orchestrator reads the JSON rather than treating it as a crash.
    return 1 if out.get("error") else 0


if __name__ == "__main__":
    sys.exit(main())
