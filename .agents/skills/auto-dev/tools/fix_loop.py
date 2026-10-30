#!/usr/bin/env python3
"""Fix loop — when the local run / verify / unit test fails, diagnose and REPAIR the code.

Closes the last gap in "run it for real": the pipeline could start the app, call the
API and assert the DB — but a red result just stopped with "sửa code, chạy lại" and no
one to do it. This tool is that someone:

    run target ─fail→ extract ROOT CAUSE ─→ headless fix-agent EDITS code in clone_dir
        ▲                                          │ (git diff captured)
        └── mvn compile ◀── mode fork ─────────────┘
             auto:       apply + retry internally (up to --max-attempts)
             checkpoint: STOP after each proposed fix -> human reviews the diff,
                         re-invoking fix_loop compiles + retests (1 cycle / call)

Failure classes it understands:
    unit  — test_runner result (<error_context> đã có sẵn)
    boot  — app process exited before healthy (local_app log -> last 'Caused by')
    flow  — flow_check step đỏ (API sai status/field, DB sai row)

Every SUCCESSFUL repair is recorded into the feedback ledger (stage="fix",
tags=[<kind>-fail, auto-fix]) so recall stops the same mistake next run. Give-up after
max attempts -> run_log note + full diagnosis history, hand back to the human.

State: temp/runs/<RID>_fixloop.json (attempts + history; survives re-invocations).
Verify result of the LAST green run: temp/runs/<RID>_verify_result.json (for
`run_log.py record-gate <RID> verify --json ...` and `ac-map --verify-json`).

Usage:
    python fix_loop.py run --run <RID> --project <P> --kind verify [--env dev] \\
        [--scenario ../../../../temp/runs/<RID>_verify.json] [--max-attempts 3] [--backend claude]
    python fix_loop.py run --run <RID> --project <P> --kind test
    python fix_loop.py reset --run <RID>          # xoá state vòng lặp (làm lại từ đầu)
"""

import argparse
import json
import os
import re
import subprocess
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

_HERE = os.path.dirname(os.path.abspath(__file__))
_SKILLS = os.path.dirname(os.path.dirname(_HERE))
_DEV_TOOLS = os.path.join(_SKILLS, "dev-automation", "tools")
sys.path.insert(0, _HERE)
sys.path.insert(0, _DEV_TOOLS)

import agent_runner    # noqa: E402
import run_log         # noqa: E402

MAX_CTX = 4000          # chars of failure context handed to the fix agent
FIX_TIMEOUT = 1200      # seconds for one fix-agent turn (it edits real files)

_FIX_SYSTEM = (
    "You are a repair agent inside a CI-like loop for a Java/Spring codebase. You are "
    "given ONE concrete failure (boot error, failing e2e step, or failing unit test) "
    "plus the task's plan/grounding. You are running INSIDE the project working copy: "
    "read the relevant files, find the root cause, and EDIT the code to fix it. Rules: "
    "(1) minimal change — fix the cause, never refactor around it; (2) no new "
    "dependencies; (3) do not touch files unrelated to the failure; (4) do not change "
    "test expectations to make them pass unless the test itself is provably wrong — "
    "say so explicitly if you do; (5) finish your reply with a short summary: root "
    "cause, files changed, why the fix is correct."
)


# --- state -------------------------------------------------------------------------

def _state_path(run_id):
    safe = "".join(c for c in str(run_id) if c.isalnum() or c in "-_.")
    return os.path.join(run_log._runs_dir(), f"{safe}_fixloop.json")


def _load_state(run_id):
    p = _state_path(run_id)
    if os.path.isfile(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return {"run_id": run_id, "attempts": 0, "pending_fix": False, "history": []}


def _save_state(st):
    with open(_state_path(st["run_id"]), "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)


# --- diagnosis ---------------------------------------------------------------------

def extract_boot_cause(log_text):
    """Root cause from a Spring boot log: last 'Caused by' chain + ERROR lines."""
    lines = log_text.splitlines()
    caused = [l for l in lines if "Caused by" in l]
    errors = [l for l in lines if "ERROR" in l or "APPLICATION FAILED TO START" in l]
    desc_i = next((i for i, l in enumerate(lines) if "Description:" in l), None)
    desc = "\n".join(lines[desc_i:desc_i + 6]) if desc_i is not None else ""
    parts = []
    if caused:
        parts.append("\n".join(caused[-3:]))
    if desc:
        parts.append(desc)
    if errors:
        parts.append("\n".join(errors[-5:]))
    return ("\n".join(parts) or log_text[-1500:])[:MAX_CTX]


def summarize_flow_fail(result):
    """First red step of a flow_check result -> compact expect-vs-actual context."""
    for s in result.get("steps", []):
        if not s.get("passed"):
            detail = {k: s.get(k) for k in ("name", "type") if s.get(k)}
            raw = s.get("result") or {}
            detail["error"] = s.get("error") or raw.get("error")
            detail["checks"] = raw.get("checks")
            detail["actual_sample"] = raw.get("sample") or raw.get("body_excerpt")
            return json.dumps(detail, ensure_ascii=False)[:MAX_CTX]
    return "(no failing step found in flow result)"


# --- target runners (module-level so tests can monkeypatch) -------------------------

def _tool(script, cli_args, extra_env=None, timeout=1800):
    env = dict(os.environ)
    env.update(extra_env or {})
    proc = subprocess.run([sys.executable, os.path.join(_DEV_TOOLS, script), *cli_args],
                          cwd=_DEV_TOOLS, capture_output=True, text=True,
                          encoding="utf-8", timeout=timeout, env=env)
    out = (proc.stdout or "").strip()
    start = out.find("{")
    if start == -1:
        return {"error": True, "message": (proc.stderr or out or "no output")[-400:]}
    try:
        return json.loads(out[start:])
    except json.JSONDecodeError:
        return {"error": True, "message": f"non-JSON from {script}: {out[-300:]}"}


def _run_test(ctx):
    r = _tool("test_runner.py", ["run", "--project", ctx["project"], "--kind", "test"])
    if r.get("passed"):
        return {"passed": True, "result": r}
    return {"passed": False, "kind": "unit",
            "context": (r.get("error_context") or json.dumps(r, ensure_ascii=False))[:MAX_CTX],
            "result": r}


def _run_verify(ctx):
    """local_app start -> wait-health -> flow_check scenario. Stops the app afterwards."""
    name = ctx["project"]
    started = _tool("local_app.py", ["start", "--name", name, "--project", name])
    if started.get("error") and "already running" not in str(started.get("message", "")):
        return {"passed": False, "kind": "boot",
                "context": f"local_app start failed: {started.get('message')}"[:MAX_CTX]}
    try:
        health = _tool("local_app.py", ["wait-health", "--name", name,
                                        "--url", ctx["health_url"],
                                        "--timeout", "300", "--expect-text", "UP"])
        if not health.get("passed"):
            log = _tool("local_app.py", ["logs", "--name", name, "--tail", "150"])
            return {"passed": False, "kind": "boot",
                    "context": extract_boot_cause(str(log.get("tail", "")))}
        flow = _tool("flow_check.py",
                     ["--file", ctx["scenario"], "--project", name, "--env", ctx["env"]],
                     extra_env={"API_BASE_URL": ctx["base_url"]})
        if flow.get("error"):
            return {"passed": False, "kind": "flow",
                    "context": f"flow_check could not run: {flow.get('message')}"[:MAX_CTX]}
        # lưu kết quả (xanh hay đỏ) để record-gate / ac-map dùng
        with open(ctx["result_path"], "w", encoding="utf-8") as f:
            json.dump(flow, f, ensure_ascii=False, indent=2)
        if flow.get("passed"):
            return {"passed": True, "result": flow}
        return {"passed": False, "kind": "flow", "context": summarize_flow_fail(flow),
                "result": flow}
    finally:
        _tool("local_app.py", ["stop", "--name", name])


def _compile(ctx):
    if not os.path.isfile(os.path.join(ctx["clone_dir"], "pom.xml")):
        return {"passed": True, "skipped": "no pom.xml"}
    proc = subprocess.run(ctx.get("compile_cmd") or "mvn -q compile -Dcheckstyle.skip=true",
                          cwd=ctx["clone_dir"], shell=True, capture_output=True,
                          text=True, encoding="utf-8", timeout=900)
    if proc.returncode != 0:
        return {"passed": False,
                "context": f"compile failed:\n{(proc.stdout or proc.stderr or '')[-2500:]}"}
    return {"passed": True}


def _spawn_fixer(ctx, failure):
    """Headless agent edits code in clone_dir; returns its summary text."""
    ground = ""
    gpath = os.path.join(run_log._runs_dir(), f"{ctx['run_id']}_grounding.md")
    if os.path.isfile(gpath):
        with open(gpath, encoding="utf-8") as f:
            ground = f.read()[:6000]
    prompt = (f"<failure kind=\"{failure['kind']}\">\n{failure['context']}\n</failure>\n"
              + (f"<grounding>\n{ground}\n</grounding>\n" if ground else "")
              + "Find the root cause in this working copy and FIX the code now.")
    return agent_runner.run_turn(prompt, system=_FIX_SYSTEM, backend=ctx["backend"],
                                 model=ctx.get("model"), timeout=FIX_TIMEOUT,
                                 dry_run_text=ctx.get("dry_run_text"),
                                 cwd=ctx["clone_dir"])


def _git_diff(clone_dir):
    try:
        d = subprocess.run(["git", "-C", clone_dir, "diff"], capture_output=True,
                           text=True, encoding="utf-8", timeout=60)
        s = subprocess.run(["git", "-C", clone_dir, "status", "--short"],
                           capture_output=True, text=True, encoding="utf-8", timeout=60)
        return (s.stdout + "\n" + d.stdout).strip()[:8000]
    except (OSError, subprocess.SubprocessError):
        return "(git diff unavailable)"


def _record_lesson(ctx, failure, fix_summary):
    """Successful repair -> feedback ledger, so recall prevents the repeat."""
    try:
        import feedback
        feedback.cmd_add(argparse.Namespace(
            project=ctx["project"], stage="fix", run_id=ctx["run_id"],
            task_type=ctx.get("task_type"), tier=None, action="edited",
            agent_output=failure["context"][:800],
            correction=fix_summary[:1200],
            reason=f"{failure['kind']} failure caught by fix_loop and repaired",
            tags=f"{failure['kind']}-fail,auto-fix"))
    except Exception as e:  # noqa: BLE001 — learning must never break the loop
        run_log.cmd_note(argparse.Namespace(
            run_id=ctx["run_id"], text=f"fix_loop: cannot record feedback ({e})"))


# --- orchestration -------------------------------------------------------------------

def _ctx_from_args(args):
    import project_config
    block = project_config.load(args.project)
    clone_dir = args.cwd or block.get("clone_dir")
    if not clone_dir or not os.path.isdir(clone_dir):
        raise ValueError(f"no usable clone_dir for project '{args.project}'")
    base_url = args.base_url
    if not base_url:
        import spring_config
        base_url = (spring_config.load(clone_dir, env=args.env) or {}).get("base_url")
    if args.kind == "verify" and not base_url:
        raise ValueError("cannot derive app base_url (no server.port in spring config); "
                         "pass --base-url")
    scenario = args.scenario or os.path.join(run_log._runs_dir(), f"{args.run}_verify.json")
    if args.kind == "verify" and not os.path.isfile(scenario):
        raise ValueError(f"verify scenario not found: {scenario} (run verify_gen first)")
    state = run_log._read(args.run)
    return {
        "run_id": args.run, "project": args.project, "env": args.env,
        "kind": args.kind, "clone_dir": clone_dir, "base_url": base_url,
        "health_url": args.health_url or (base_url.rstrip("/") + "/management/health"
                                          if base_url else None),
        "scenario": scenario,
        "result_path": os.path.join(run_log._runs_dir(), f"{args.run}_verify_result.json"),
        "backend": args.backend, "model": args.model, "dry_run_text": args.dry_run_text,
        "compile_cmd": args.compile_cmd, "task_type": state.get("type"),
        "mode": state.get("mode", "checkpoint"),
    }


def _one_cycle(ctx, st):
    """run target; on fail spawn fixer. Returns (outcome, payload)."""
    runner = _run_verify if ctx["kind"] == "verify" else _run_test
    res = runner(ctx)
    if res["passed"]:
        return "green", res
    if st["attempts"] >= ctx["max_attempts"]:
        return "give_up", res
    st["attempts"] += 1
    fix_summary = _spawn_fixer(ctx, res)
    diff = _git_diff(ctx["clone_dir"])
    st["history"].append({"attempt": st["attempts"], "kind": res["kind"],
                          "cause": res["context"][:800], "fix": fix_summary[:800]})
    st["last_failure"] = {"kind": res["kind"], "context": res["context"]}
    return "fixed", {"failure": res, "fix_summary": fix_summary, "diff": diff}


def cmd_run(args):
    ctx = _ctx_from_args(args)
    ctx["max_attempts"] = args.max_attempts
    st = _load_state(args.run)

    # Nhánh quay lại sau khi người duyệt diff (checkpoint) — compile trước rồi retest.
    if st.get("pending_fix"):
        st["pending_fix"] = False
        comp = _compile(ctx)
        if not comp["passed"]:
            st["last_failure"] = {"kind": "compile", "context": comp["context"]}
            _save_state(st)
            return {"status": "compile_failed", "attempt": st["attempts"],
                    "context": comp["context"],
                    "next": "sửa lỗi compile (hoặc git checkout -- . rồi fix_loop reset)"}

    while True:
        outcome, payload = _one_cycle(ctx, st)
        if outcome == "green":
            _save_state(st)
            if st["attempts"] > 0 and st["history"]:
                last = st["history"][-1]
                _record_lesson(ctx, {"kind": last["kind"], "context": last["cause"]},
                               last["fix"])
                run_log.cmd_note(argparse.Namespace(
                    run_id=ctx["run_id"],
                    text=f"fix_loop: {ctx['kind']} green after {st['attempts']} repair(s)"))
            out = {"status": "green", "kind": ctx["kind"], "attempts": st["attempts"]}
            if ctx["kind"] == "verify":
                out["verify_result"] = ctx["result_path"]
                out["next"] = (f"run_log.py record-gate {ctx['run_id']} verify "
                               f"--json {ctx['result_path']}")
            return out
        if outcome == "give_up":
            run_log.cmd_note(argparse.Namespace(
                run_id=ctx["run_id"],
                text=f"fix_loop: {ctx['kind']} still red after {st['attempts']} repair(s) — "
                     f"handing back to the human"))
            _save_state(st)
            return {"status": "failed", "attempts": st["attempts"],
                    "last_failure": payload.get("context"), "history": st["history"],
                    "next": "cần người: xem history + temp log; sửa tay hoặc reset"}
        # outcome == "fixed"
        if ctx["mode"] == "checkpoint":
            st["pending_fix"] = True
            _save_state(st)
            return {"status": "awaiting_approval", "attempt": st["attempts"],
                    "failure_kind": payload["failure"]["kind"],
                    "cause": payload["failure"]["context"][:1200],
                    "fix_summary": payload["fix_summary"][:1200],
                    "diff": payload["diff"],
                    "next": ("NGƯỜI DUYỆT diff này. Đồng ý -> chạy lại fix_loop (tự "
                             "compile + retest). Từ chối -> `git -C <clone_dir> checkout -- .` "
                             "rồi fix_loop reset + sửa tay")}
        # auto: compile rồi lặp tiếp trong cùng invocation
        comp = _compile(ctx)
        if not comp["passed"]:
            st["history"].append({"attempt": st["attempts"], "kind": "compile",
                                  "cause": comp["context"][:800], "fix": "(chưa)"})
            _save_state(st)
            return {"status": "compile_failed", "attempt": st["attempts"],
                    "context": comp["context"], "history": st["history"]}
        _save_state(st)


def cmd_reset(args):
    p = _state_path(args.run)
    existed = os.path.isfile(p)
    if existed:
        os.remove(p)
    return {"ok": True, "reset": existed, "run_id": args.run}


def main():
    ap = argparse.ArgumentParser(
        description="Diagnose-and-repair loop for local run / verify / unit test failures.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="chạy target; đỏ -> fix-agent sửa code (theo mode của run)")
    r.add_argument("--run", required=True, help="run_id (đọc mode + ghi note/feedback)")
    r.add_argument("--project", required=True)
    r.add_argument("--kind", required=True, choices=["test", "verify"])
    r.add_argument("--env", default="dev")
    r.add_argument("--scenario", default=None, help="mặc định temp/runs/<RID>_verify.json")
    r.add_argument("--base-url", default=None, help="mặc định suy từ spring_config (server.port)")
    r.add_argument("--health-url", default=None)
    r.add_argument("--cwd", default=None, help="đè clone_dir từ registry")
    r.add_argument("--compile-cmd", default=None, help="mặc định mvn -q compile khi có pom.xml")
    r.add_argument("--max-attempts", type=int, default=3)
    r.add_argument("--backend", default="claude")
    r.add_argument("--model", default=None)
    r.add_argument("--dry-run-text", default=None, help="giả lập fix-agent (tests)")

    rs = sub.add_parser("reset", help="xoá state vòng lặp của một run")
    rs.add_argument("--run", required=True)

    args = ap.parse_args()
    try:
        out = cmd_run(args) if args.cmd == "run" else cmd_reset(args)
    except (OSError, ValueError, agent_runner.AgentRunError) as e:
        out = {"error": True, "message": str(e)}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 1 if isinstance(out, dict) and (out.get("error") or out.get("status") == "failed") else 0


if __name__ == "__main__":
    sys.exit(main())
