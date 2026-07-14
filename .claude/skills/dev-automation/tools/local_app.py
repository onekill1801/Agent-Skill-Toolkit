#!/usr/bin/env python3
"""Local app-under-test lifecycle for the stack-verify toolkit.

flow_check / probe_* assert against a RUNNING service; they don't start one. This
tool fills that gap: start an app on localhost (e.g. `mvn spring-boot:run`), wait
for its health endpoint to come up, then (after the e2e scenario) stop it. Lets you
run the full loop locally: build/run -> call API -> watch DB -> tear down.

Zero external dependencies — Python stdlib only. Cross-platform (Windows/macOS/Linux).

State per app in <repo>/temp/local_apps/<name>.json ; logs in <name>.log.

Usage:
    # 1) start (detached, logs captured); --project sets cwd from the registry clone_dir
    python local_app.py start --name atask --project atask \
        --cmd "mvn -q spring-boot:run -Dspring-boot.run.profiles=dev"
    # 2) block until healthy (JHipster: /management/health returns {"status":"UP"})
    python local_app.py wait-health --name atask \
        --url http://localhost:8271/management/health --timeout 240 --expect-text UP
    # 3) inspect
    python local_app.py status --name atask
    python local_app.py logs   --name atask --tail 80
    # 4) ... run flow_check / probe_* against localhost ...
    # 5) stop (kills the whole process tree)
    python local_app.py stop   --name atask

Output: a single JSON object.
"""

import argparse
import json
import os
import signal
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request

import config
import project_config
import probe_common as pc

IS_WINDOWS = os.name == "nt"


def _repo_root() -> str:
    search = os.path.dirname(os.path.abspath(__file__))
    for _ in range(6):
        if os.path.isdir(os.path.join(search, ".git")) or os.path.isfile(os.path.join(search, "CLAUDE.md")):
            return search
        search = os.path.dirname(search)
    return os.getcwd()


def _state_dir() -> str:
    d = os.path.join(_repo_root(), "temp", "local_apps")
    os.makedirs(d, exist_ok=True)
    return d


def _state_path(name: str) -> str:
    return os.path.join(_state_dir(), f"{name}.json")


def _load_state(name: str):
    p = _state_path(name)
    if not os.path.isfile(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _save_state(name: str, data: dict):
    with open(_state_path(name), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _pid_alive(pid: int) -> bool:
    if IS_WINDOWS:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"],
                             capture_output=True, text=True)
        return str(pid) in out.stdout
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _write_argfile(cwd: str) -> dict:
    """target/cp.txt (maven build-classpath) -> target/local_run.args (java @argfile).

    Java 9+ argfiles dodge the Windows 32k command-line limit that kills
    `mvn spring-boot:run` on big projects (CreateProcess error=206) — the same trick
    IntelliJ uses. Classpath = target/classes + every dependency jar; system-scope
    jars are already in maven's output, and any extra jars in src/main/resources/lib
    are appended defensively. Paths use forward slashes (argfile quoting treats
    backslash as escape).
    """
    cp_file = os.path.join(cwd, "target", "cp.txt")
    if not os.path.isfile(cp_file):
        return {"error": True, "message": f"missing {cp_file} — run mvn dependency:build-classpath first"}
    with open(cp_file, encoding="utf-8") as f:
        entries = ["target/classes"] + [e for e in f.read().strip().split(os.pathsep) if e]
    lib_dir = os.path.join(cwd, "src", "main", "resources", "lib")
    if os.path.isdir(lib_dir):
        known = {os.path.basename(e) for e in entries}
        entries += [f"src/main/resources/lib/{f}" for f in sorted(os.listdir(lib_dir))
                    if f.endswith(".jar") and f not in known]
    argfile = os.path.join(cwd, "target", "local_run.args")
    with open(argfile, "w", encoding="utf-8") as f:
        f.write('-cp "' + ";".join(e.replace("\\", "/") for e in entries) + '"')
    return {"ok": True, "argfile": argfile, "entries": len(entries)}


def cmd_prep_java(args) -> dict:
    """Generate the argfile + suggest a run command (no jar packaging needed).

    Re-run when pom dependencies change; code changes only need `mvn -q compile`.
    """
    block = project_config.load(args.project) if args.project else {}
    cwd = os.path.abspath(args.cwd or block.get("clone_dir") or os.getcwd())
    if not os.path.isfile(os.path.join(cwd, "pom.xml")):
        return {"error": True, "message": f"no pom.xml in {cwd}"}
    proc = subprocess.run(
        "mvn -q dependency:build-classpath -Dmdep.outputFile=target/cp.txt",
        cwd=cwd, shell=True, capture_output=True, text=True, timeout=300)
    if proc.returncode != 0:
        return {"error": True, "message": f"build-classpath failed: {(proc.stderr or proc.stdout or '')[-400:]}"}
    out = _write_argfile(cwd)
    if out.get("error"):
        return out
    profile = f" -Dspring.profiles.active={args.profile}" if args.profile else ""
    out["run_cmd"] = (f"java -XX:TieredStopAtLevel=1{profile} -Dfile.encoding=UTF-8 "
                      f"@target/local_run.args {args.main}")
    out["note"] = ("đặt run_cmd này vào projects.json `app_run_cmd`; code đổi -> `mvn -q compile`; "
                   "pom đổi dependency -> chạy lại prep-java")
    return out


def _resolve_cmd(args, block: dict, cwd: str):
    """Run-command precedence: --cmd flag > registry `app_run_cmd` > mvn default.

    `app_run_cmd` in work/projects.json captures the ONE command known to boot this
    app on this machine (e.g. atask needs jar + PropertiesLauncher: `mvn spring-boot:run`
    dies with Windows error=206, and a system-scope lib is missing from the fat jar) —
    so nobody has to rediscover it. Returns (cmd|None, source).
    """
    if args.cmd:
        return args.cmd, "flag"
    reg = (block or {}).get("app_run_cmd")
    if reg:
        return reg, "registry:app_run_cmd"
    if os.path.isfile(os.path.join(cwd, "pom.xml")):
        return "mvn -q spring-boot:run", "default:pom.xml"
    return None, "none"


def cmd_start(args) -> dict:
    block = project_config.load(args.project) if args.project else {}
    cwd = args.cwd
    if not cwd and args.project:
        cwd = block.get("clone_dir")
    cwd = os.path.abspath(os.path.expanduser(cwd or os.getcwd()))
    if not os.path.isdir(cwd):
        return {"error": True, "message": f"cwd does not exist: {cwd}"}

    cmd, cmd_source = _resolve_cmd(args, block, cwd)
    if not cmd:
        return {"error": True,
                "message": "no --cmd, no `app_run_cmd` in registry, and no pom.xml to default from"}

    existing = _load_state(args.name)
    if existing and _pid_alive(existing.get("pid", -1)):
        return {"error": True, "message": f"app '{args.name}' already running (pid {existing['pid']}); "
                                          f"stop it first or use a different --name"}

    env = dict(os.environ)
    for kv in args.env or []:
        if "=" in kv:
            k, v = kv.split("=", 1)
            env[k] = v

    log_path = args.log or os.path.join(_state_dir(), f"{args.name}.log")
    log_f = open(log_path, "w", encoding="utf-8", errors="replace")
    # Detach into its own process group so stop can kill the whole tree (mvn -> java).
    popen_kwargs = dict(cwd=cwd, env=env, shell=True,
                        stdout=log_f, stderr=subprocess.STDOUT)
    if IS_WINDOWS:
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True
    proc = subprocess.Popen(cmd, **popen_kwargs)
    state = {"name": args.name, "pid": proc.pid, "cmd": cmd, "cwd": cwd, "log": log_path}
    _save_state(args.name, state)
    return {"started": True, **state, "cmd_source": cmd_source,
            "note": "detached; use wait-health then run your scenario, then 'stop'"}


def _ssl_ctx():
    if config.get("SSL_VERIFY", "true").lower() in ("false", "0", "no"):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    return ssl.create_default_context()


def cmd_wait_health(args) -> dict:
    state = _load_state(args.name) if args.name else None
    url = args.url
    if not url:
        return {"error": True, "message": "provide --url <health endpoint>"}
    deadline = time.time() + args.timeout
    last = None
    attempts = 0
    while time.time() < deadline:
        attempts += 1
        # If we own the process and it already died, fail fast with a log hint.
        if state and not _pid_alive(state.get("pid", -1)):
            return {"error": True, "passed": False, "message": "app process exited before becoming healthy",
                    "log": state.get("log"), "hint": "check 'local_app.py logs' for the boot error"}
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, context=_ssl_ctx(), timeout=10) as resp:
                body = resp.read().decode(errors="replace")
                if 200 <= resp.status < 300 and (not args.expect_text or args.expect_text in body):
                    return {"passed": True, "healthy": True, "status": resp.status,
                            "attempts": attempts, "body": body[:300]}
                last = f"HTTP {resp.status}: {body[:120]}"
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
        except (urllib.error.URLError, OSError) as e:
            last = f"{e}"
        time.sleep(args.interval)
    return {"error": True, "passed": False, "healthy": False,
            "message": f"not healthy within {args.timeout}s ({attempts} attempts)", "last": last}


def cmd_status(args) -> dict:
    state = _load_state(args.name)
    if not state:
        return {"error": True, "message": f"no app named '{args.name}' (never started?)"}
    return {"name": args.name, "pid": state["pid"], "running": _pid_alive(state["pid"]),
            "cmd": state["cmd"], "cwd": state["cwd"], "log": state.get("log")}


def cmd_logs(args) -> dict:
    state = _load_state(args.name)
    if not state or not state.get("log") or not os.path.isfile(state["log"]):
        return {"error": True, "message": f"no log for '{args.name}'"}
    with open(state["log"], encoding="utf-8", errors="replace") as f:
        lines = f.read().splitlines()
    tail = lines[-args.tail:] if len(lines) > args.tail else lines
    return {"name": args.name, "log": state["log"], "lines_shown": len(tail),
            "tail": "\n".join(tail)}


def cmd_stop(args) -> dict:
    state = _load_state(args.name)
    if not state:
        return {"error": True, "message": f"no app named '{args.name}'"}
    pid = state["pid"]
    if not _pid_alive(pid):
        return {"stopped": True, "name": args.name, "note": "was not running"}
    try:
        if IS_WINDOWS:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           capture_output=True, text=True)
        else:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
            for _ in range(10):
                if not _pid_alive(pid):
                    break
                time.sleep(0.5)
            if _pid_alive(pid):
                os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (OSError, ProcessLookupError) as e:
        return {"error": True, "message": f"stop failed: {e}"}
    return {"stopped": True, "name": args.name, "pid": pid}


def main() -> int:
    parser = argparse.ArgumentParser(description="Local app-under-test lifecycle (start/wait-health/stop)")
    sub = parser.add_subparsers(dest="action", required=True)

    s = sub.add_parser("start", help="start an app detached, capturing logs")
    s.add_argument("--name", required=True, help="logical id for this app instance")
    s.add_argument("--project", help="resolve cwd from clone_dir in work/projects.json")
    s.add_argument("--cwd", help="working directory (overrides --project)")
    s.add_argument("--cmd", help="run command (default 'mvn -q spring-boot:run' if pom.xml present)")
    s.add_argument("--log", help="log file (default temp/local_apps/<name>.log)")
    s.add_argument("--env", action="append", help="extra env KEY=VALUE (repeatable)")

    w = sub.add_parser("wait-health", help="poll a health URL until UP or timeout")
    w.add_argument("--name", help="app instance (to fail fast if it crashed)")
    w.add_argument("--url", required=True, help="health endpoint, e.g. http://localhost:8271/management/health")
    w.add_argument("--timeout", type=int, default=180)
    w.add_argument("--interval", type=int, default=3)
    w.add_argument("--expect-text", help="substring that must appear in the body (e.g. UP)")

    for name in ("status", "stop"):
        sp = sub.add_parser(name)
        sp.add_argument("--name", required=True)

    lg = sub.add_parser("logs")
    lg.add_argument("--name", required=True)
    lg.add_argument("--tail", type=int, default=60)

    pj = sub.add_parser("prep-java",
                        help="sinh target/local_run.args (java @argfile) — chạy app từ classes, "
                             "không cần build jar, né error=206")
    pj.add_argument("--project", help="clone_dir từ registry")
    pj.add_argument("--cwd", help="project root (đè --project)")
    pj.add_argument("--main", required=True, help="main class, vd com.example.atask.AtaskApp")
    pj.add_argument("--profile", default=None, help="spring profile (vd dev)")

    args = parser.parse_args()
    handlers = {"start": cmd_start, "wait-health": cmd_wait_health, "status": cmd_status,
                "logs": cmd_logs, "stop": cmd_stop, "prep-java": cmd_prep_java}
    return pc.emit(handlers[args.action](args))


if __name__ == "__main__":
    sys.exit(main())
