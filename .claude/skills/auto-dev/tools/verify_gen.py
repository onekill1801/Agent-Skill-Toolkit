#!/usr/bin/env python3
"""Generate a runtime-verify scenario for a run: AC + plan -> flow_check JSON.

Gap this closes: the pipeline had every tool to "run the app -> exercise the changed
flow -> assert the response AND the DB row" (local_app, probe_*, flow_check), but no
step ever produced the scenario — so runtime verification was silently skipped and
tasks shipped on unit tests alone. This tool turns the plan's <target_files> +
<test_strategy> and the AC ledger into `temp/runs/<RID>_verify.json`, one step per
provable AC, named "ACn: ..." so `run_log.py ac-map --verify-json` can later demand
a PASSED step as the AC's evidence.

It also answers "does this task touch runtime behaviour at all?" (touches_runtime):
if yes, the caller promotes the verify gate to required — `run_log.py require <RID>
verify` — so in auto mode the Test stage cannot advance without a green flow_check.

The scenario is agent-generated (headless, subscription CLI) and MUST be reviewed by
the human together with the plan at checkpoint `after_plan`. DB connection is NOT
embedded in the scenario — flow_check resolves it via --project/--env (registry,
gap-filled from the Spring app's application-<env>.yml by project_config).

Usage:
    python verify_gen.py run --run <RID> --plan ../../../../temp/runs/<task>_plan.xml \\
        [--context-file <pack/scout>] [--backend claude] [--base-url http://localhost:8080]
    # tests: --backend dry-run --dry-run-text '<json>'

Output: one JSON object {ok, scenario_path, step_count, acs_covered, acs_uncovered,
touches_runtime, verdict}.
"""

import argparse
import json
import os
import re
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

_HERE = os.path.dirname(os.path.abspath(__file__))
_SKILLS = os.path.dirname(os.path.dirname(_HERE))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(_SKILLS, "dev-automation", "tools"))

import agent_runner   # noqa: E402
import run_log        # noqa: E402

STEP_TYPES = {"api", "db", "kafka", "redis", "wait"}

# Files whose change implies observable runtime behaviour (API/DB/messaging).
_RUNTIME_HINT = re.compile(
    r"controller|service|repository|resource|endpoint|handler|listener|consumer|"
    r"producer|entity|dao|mapper|\.sql|migration|changelog|route|api",
    re.IGNORECASE)

# Spring mapping annotations -> (HTTP method, path). @RequestMapping ở cấp class là prefix.
_MAPPING = re.compile(
    r"@(Get|Post|Put|Delete|Patch|Request)Mapping\s*"
    r"(?:\(\s*(?:value\s*=\s*)?\{?\s*\"([^\"]*)\")?",
    re.IGNORECASE)

_SYSTEM = (
    "You write end-to-end verification scenarios for a Java/Spring backend pipeline. "
    "Input: a task's final plan (<final_specification>) and its acceptance criteria (AC list). "
    "Output: ONE flow_check scenario as pure JSON — no markdown, no prose, nothing outside "
    "the JSON object. Schema: {\"name\": str, \"vars\": {..}, \"steps\": [step...]}. "
    "Step types: "
    "api  = {\"type\":\"api\",\"name\":..,\"method\":..,\"url\":\"/path\",\"body\":{},"
    "\"expect\":{\"status\":200,\"json\":{\"$.field\":\"value\"}},\"saveFrom\":{\"var\":\"$.id\"}} · "
    "db   = {\"type\":\"db\",\"name\":..,\"engine\":\"postgres|mysql\",\"sql\":\"select ...\","
    "\"expect\":{\"rows\":1,\"value\":\"X\"}} · "
    "kafka= {\"type\":\"kafka\",\"op\":\"consume\",\"topic\":..,\"timeout\":15,"
    "\"expect\":{\"contains\":..}} · redis similar. "
    "RULES: (1) every AC that describes observable behaviour or data MUST have at least one "
    "step whose name starts with its id, e.g. \"AC1: ...\" — that step IS the AC's evidence; "
    "(2) after any state-changing api step, add a db step asserting the row really changed; "
    "(3) use ONLY endpoints/tables/columns grounded in the plan or provided context — if the "
    "plan lacks the concrete table/endpoint, write the step with your best grounded guess and "
    "add \"_review\": \"<what to double-check>\" inside that step; (4) do NOT invent auth "
    "headers or credentials — flow_check injects connection config; (5) 3-10 steps, ordered "
    "as a story: act -> assert output -> assert DB."
)


def _plan_targets(plan_text):
    m = re.search(r"<target_files>(.*?)</target_files>", plan_text, re.S | re.I)
    if not m:
        return []
    body = m.group(1)
    files = re.findall(r"<file>(.*?)</file>", body, re.S | re.I) or \
        [ln.strip(" -*\t") for ln in body.splitlines()]
    return [f.strip() for f in files if f.strip()]


def touches_runtime(plan_text, task_type=None):
    """Heuristic: the change lands in code whose effect is observable at runtime."""
    targets = _plan_targets(plan_text)
    if any(_RUNTIME_HINT.search(t) for t in targets):
        return True
    # No parseable targets: fall back to the whole plan text for strong hints.
    if not targets and _RUNTIME_HINT.search(plan_text or ""):
        return True
    return False


def _find_file(root, rel):
    """Locate a plan target inside clone_dir: direct join, else search by basename."""
    direct = os.path.join(root, rel.replace("/", os.sep))
    if os.path.isfile(direct):
        return direct
    base = os.path.basename(rel)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d not in {".git", "node_modules", "target", "build"}]
        if base in filenames:
            return os.path.join(dirpath, base)
    return None


def affected_endpoints(root, targets):
    """The task's OUTPUT SURFACE: endpoints served by the changed controllers.

    Reads each target file (plus, for a changed Service, the controller with the
    matching name) and combines the class-level @RequestMapping prefix with each
    method-level mapping. These are the APIs the scenario MUST call — "sửa logic
    của API nào thì phải chạy và test đúng API đó".
    """
    if not root or not os.path.isdir(root):
        return []
    names = set()
    for t in targets:
        names.add(os.path.basename(t))
        # XxxService / XxxServiceImpl đổi -> đầu ra thường lộ qua XxxController.
        m = re.match(r"(\w+?)(Service(Impl)?|Repository)\.java$", os.path.basename(t))
        if m:
            names.add(f"{m.group(1)}Controller.java")
            names.add(f"{m.group(1)}Resource.java")   # kiểu JHipster
    out, seen = [], set()
    for name in sorted(names):
        if not re.search(r"(Controller|Resource)\.java$", name):
            continue
        path = _find_file(root, name)
        if not path:
            continue
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            continue
        if "@RestController" not in text and "@Controller" not in text:
            continue
        prefix = ""
        hits = list(_MAPPING.finditer(text))
        cls = re.search(r"class\s+\w+", text)
        for h in hits:  # @RequestMapping đứng trước 'class' = prefix cấp class
            if h.group(1).lower() == "request" and cls and h.start() < cls.start():
                prefix = (h.group(2) or "").rstrip("/")
                break
        for h in hits:
            kind, sub = h.group(1).lower(), (h.group(2) or "")
            if kind == "request" and cls and h.start() < cls.start():
                continue
            method = {"get": "GET", "post": "POST", "put": "PUT", "delete": "DELETE",
                      "patch": "PATCH", "request": "ANY"}[kind]
            full = (prefix + ("/" + sub.lstrip("/") if sub else "")) or "/"
            key = f"{method} {full}"
            if key not in seen:
                seen.add(key)
                out.append({"method": method, "path": full, "file": os.path.basename(path)})
    return out


def _parse_scenario(raw):
    """Agent replies should be pure JSON; tolerate fences/preamble by bracket-slicing."""
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text, flags=re.S)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("agent returned no JSON object")
    scenario = json.loads(text[start:end + 1])
    steps = scenario.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ValueError("scenario has no steps")
    for i, s in enumerate(steps):
        st = s.get("type")
        if st not in STEP_TYPES:
            raise ValueError(f"step #{i} has unknown type '{st}' (allowed: {sorted(STEP_TYPES)})")
        if st == "api" and not s.get("url"):
            raise ValueError(f"api step #{i} missing url")
        if st == "db" and not s.get("sql"):
            raise ValueError(f"db step #{i} missing sql")
    return scenario


def _ac_step_re(ac_id):
    """Step name proves an AC when it starts 'AC1:' / 'AC1-' / 'AC1_' / 'AC1 ' —
    agents alternate separators no matter how firmly the prompt says ':'."""
    return re.compile(rf"^\s*{re.escape(str(ac_id))}\s*[:\-_ ]", re.IGNORECASE)


def _ac_coverage(scenario, acs):
    step_names = [str(s.get("name", "")) for s in scenario.get("steps", [])]
    covered, uncovered = [], []
    for a in acs:
        rx = _ac_step_re(a.get("id", ""))
        (covered if any(rx.match(n) for n in step_names) else uncovered).append(a.get("id"))
    return covered, uncovered


def cmd_run(args):
    with open(args.plan, encoding="utf-8") as f:
        plan_text = f.read()

    try:
        state = run_log._read(args.run)
        acs = [a for a in state.get("acceptance_criteria", []) if a.get("status") != "waived"]
    except ValueError:
        state, acs = None, []

    runtime = touches_runtime(plan_text, task_type=(state or {}).get("type"))
    root = getattr(args, "root", None)
    endpoints = affected_endpoints(root, _plan_targets(plan_text)) if root else []

    context = ""
    if args.context_file:
        try:
            with open(args.context_file, encoding="utf-8") as f:
                context = f.read()[:8000]
        except OSError as e:
            context = ""
            ctx_note = f"context-file unreadable ({e})"
        else:
            ctx_note = None
    else:
        ctx_note = None

    ac_block = "\n".join(f"- {a.get('id')}: {a.get('text')}" for a in acs) or "(no AC recorded)"
    ep_block = "\n".join(f"- {e['method']} {e['path']}  ({e['file']})" for e in endpoints)
    prompt = (f"<plan>\n{plan_text[:12000]}\n</plan>\n"
              f"<acceptance_criteria>\n{ac_block}\n</acceptance_criteria>\n"
              + (f"<affected_endpoints>\nThe changed code serves THESE endpoints — the "
                 f"scenario MUST call each one that is relevant to the change, then assert "
                 f"the DB rows it reads/writes:\n{ep_block}\n</affected_endpoints>\n"
                 if endpoints else "")
              + (f"<context>\n{context}\n</context>\n" if context else "")
              + (f"Base URL of the locally running app: {args.base_url}\n" if args.base_url else "")
              + "Write the flow_check scenario JSON now.")

    raw = agent_runner.run_turn(prompt, system=_SYSTEM, backend=args.backend,
                                model=args.model, dry_run_text=args.dry_run_text)
    scenario = _parse_scenario(raw)
    scenario.setdefault("name", f"verify {args.run}")

    out_path = args.out or os.path.join(run_log._runs_dir(), f"{args.run}_verify.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(scenario, f, ensure_ascii=False, indent=2)

    covered, uncovered = _ac_coverage(scenario, acs)
    reviews = [f"{s.get('name')}: {s['_review']}" for s in scenario["steps"] if s.get("_review")]

    # Endpoint bị ảnh hưởng mà kịch bản KHÔNG gọi -> cảnh báo cho người duyệt
    # (so khớp path đã bỏ biến {id} để '/api/tasks/{id}' khớp '/api/tasks/123').
    api_urls = [str(s.get("url", "")) for s in scenario["steps"] if s.get("type") == "api"]
    untested = []
    for e in endpoints:
        skeleton = re.sub(r"\{[^}]*\}", "", e["path"]).rstrip("/")
        if skeleton and not any(skeleton in re.sub(r"\{[^}]*\}", "", u) for u in api_urls):
            untested.append(f"{e['method']} {e['path']}")
    return {
        "ok": True,
        "run_id": args.run,
        "scenario_path": out_path,
        "step_count": len(scenario["steps"]),
        "acs_covered": covered,
        "acs_uncovered": uncovered,       # trình cho người duyệt: AC nào verify chưa chứng minh
        "needs_review": reviews,          # step agent tự nhận là đoán — người phải soi
        "affected_endpoints": endpoints,  # đầu ra của task: các API mà code sửa phục vụ
        "endpoints_untested": untested,   # API bị ảnh hưởng nhưng kịch bản KHÔNG gọi -> soi lại
        "touches_runtime": runtime,
        "verdict": "pass" if not (uncovered or untested) else "partial",
        "note": ctx_note,
        "next": (f"run_log.py require {args.run} verify   # bắt buộc gate verify" if runtime
                 else "task không chạm runtime -> verify tuỳ chọn"),
    }


def main():
    ap = argparse.ArgumentParser(description="Generate a flow_check verify scenario from plan + AC.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="plan + AC -> temp/runs/<RID>_verify.json")
    r.add_argument("--run", required=True, help="run_id (đọc AC ledger từ run file)")
    r.add_argument("--plan", required=True, help="path tới <task>_plan.xml")
    r.add_argument("--context-file", default=None, help="pack/scout để agent ground endpoint/bảng")
    r.add_argument("--root", default=None,
                   help="clone_dir: bóc endpoint bị ảnh hưởng từ controller trong <target_files>")
    r.add_argument("--base-url", default=None, help="base URL app local (từ spring_config/local_app)")
    r.add_argument("--out", default=None)
    r.add_argument("--backend", default="claude", help="claude|cursor|api|dry-run")
    r.add_argument("--model", default=None)
    r.add_argument("--dry-run-text", default=None, help="canned scenario JSON (tests)")
    args = ap.parse_args()
    try:
        out = cmd_run(args)
    except (OSError, ValueError, agent_runner.AgentRunError) as e:
        out = {"error": True, "message": str(e)}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 1 if isinstance(out, dict) and out.get("error") else 0


if __name__ == "__main__":
    sys.exit(main())
