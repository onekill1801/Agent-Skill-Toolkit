#!/usr/bin/env python3
"""Pipeline run-log / state machine for the auto-dev orchestrator.

A long pipeline (Plan -> Implement -> Test -> Deliver) may span many turns and
human checkpoints. This tool persists the run state to a JSON file so the
orchestrator can resume after an interrupt and so a human can inspect progress.

State lives in:  <repo>/temp/runs/<run_id>.json   (temp/ is gitignored)

Stages are fixed: plan, implement, test, deliver.
Stage status:    pending | active | done | failed | skipped
Checkpoints:     after_plan, before_mr, before_notify  (recorded as approved/pending)

Zero external dependencies — Python stdlib only.

Evidence-gated transitions (Hybrid autonomy):
    Each stage can only be advanced to "done" once the gate evidence it requires
    has been recorded. The gate guards the state machine instead of sitting beside
    it. `advance` reads the run's mode (auto|checkpoint) to decide block-vs-inform.
    The legacy `stage <id> <stage> done` command is kept untouched as an escape
    hatch / for backward compatibility — it still trusts the caller blindly.

Usage:
    python run_log.py init <run_id> --task 12345 --project atask --type bugfix --title "..." \
                       [--tier trivial|standard|complex] [--mode auto|checkpoint]
    python run_log.py stage <run_id> plan active
    python run_log.py stage <run_id> test failed
    python run_log.py checkpoint <run_id> after_plan approved
    python run_log.py record-gate <run_id> test --verdict pass --json result.json
    python run_log.py record-gate <run_id> lint --verdict waived --summary "no linter"
    python run_log.py advance <run_id> test [--force]
    python run_log.py ac-add <run_id> --text "GET /users returns 200" [--id AC1]
    python run_log.py ac-map <run_id> AC1 --evidence "probe_api result"
    python run_log.py ac-waive <run_id> AC1 --note "out of scope"
    python run_log.py note <run_id> "branch bugfix/12345-... created"
    python run_log.py field <run_id> branch bugfix/12345-foo
    python run_log.py get <run_id>
    python run_log.py list [--open]

Output: a single JSON object/array on stdout.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

# Cross-platform: force UTF-8 stdout so JSON output doesn't crash on a Windows
# cp1252/cp437 console when notes/titles contain non-ASCII. No-op elsewhere.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

STAGES = ["plan", "implement", "test", "deliver"]
STAGE_STATUS = ["pending", "active", "done", "failed", "skipped"]
CHECKPOINTS = ["after_plan", "before_mr", "before_notify"]
CHECKPOINT_STATUS = ["pending", "approved", "rejected"]

# --- Evidence-gated transitions (Hybrid autonomy) ---
GATE_VERDICTS = ["pass", "fail", "error", "waived"]
AC_STATUS = ["open", "met", "waived"]
TIERS = ["trivial", "standard", "complex"]
MODES = ["auto", "checkpoint"]

# Gates that MUST be pass|waived before a stage can advance to done.
# Everything else is advisory: it is reported but never blocks on its own.
REQUIRED_GATES = {
    "plan": ["clarity"],             # requirement must be unambiguous before debate (see clarify.py)
    "implement": ["grounding"],
    "test": ["test", "lint"],        # lint mandatory but waivable (no linter -> waive)
    "deliver": ["review", "ac"],     # 'ac' is derived from the AC ledger (see _gate_verdict)
}
ADVISORY_GATES = {"build", "integration"}


def _repo_root() -> str:
    """Walk up from this file to find the repo root (where temp/ lives)."""
    here = os.path.dirname(os.path.abspath(__file__))
    search = here
    for _ in range(6):
        if os.path.isdir(os.path.join(search, ".git")) or os.path.isfile(os.path.join(search, "CLAUDE.md")):
            return search
        search = os.path.dirname(search)
    return here


def _runs_dir() -> str:
    d = os.path.join(_repo_root(), "temp", "runs")
    os.makedirs(d, exist_ok=True)
    return d


def _path(run_id: str) -> str:
    safe = "".join(c for c in run_id if c.isalnum() or c in "-_.")
    return os.path.join(_runs_dir(), f"{safe}.json")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize(state: dict) -> dict:
    """Fill in keys added by the evidence-gating feature for older run files.

    In-memory only: the file is not rewritten unless the command itself writes.
    The safe default falls out naturally — an old run reads as mode=checkpoint,
    so it informs and never silently auto-advances.
    """
    state.setdefault("gates", {})
    state.setdefault("acceptance_criteria", [])
    state.setdefault("tier", "standard")
    state.setdefault("mode", "checkpoint")
    state.setdefault("notes", [])
    state.setdefault("test_attempts", 0)
    state.setdefault("required_extra", {})   # per-run extra required gates, e.g. {"test": ["verify"]}
    return state


def _read(run_id: str) -> dict:
    p = _path(run_id)
    if not os.path.isfile(p):
        raise ValueError(f"run not found: {run_id}")
    with open(p, encoding="utf-8") as f:
        return _normalize(json.load(f))


def _write(state: dict) -> None:
    with open(_path(state["run_id"]), "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def cmd_init(args) -> dict:
    tier = args.tier or "standard"
    mode = args.mode or "checkpoint"
    if tier not in TIERS:
        raise ValueError(f"unknown tier '{tier}', expected one of {TIERS}")
    if mode not in MODES:
        raise ValueError(f"unknown mode '{mode}', expected one of {MODES}")
    state = {
        "run_id": args.run_id,
        "task_id": args.task,
        "project": args.project,
        "type": args.type,
        "title": args.title,
        "tier": tier,
        "mode": mode,
        "branch": None,
        "mr_url": None,
        "stages": {s: "pending" for s in STAGES},
        "checkpoints": {c: "pending" for c in CHECKPOINTS},
        "gates": {},
        "acceptance_criteria": [],
        "notes": [],
        "test_attempts": 0,
    }
    _write(state)
    return state


def cmd_stage(args) -> dict:
    state = _read(args.run_id)
    if args.stage not in STAGES:
        raise ValueError(f"unknown stage '{args.stage}', expected one of {STAGES}")
    if args.status not in STAGE_STATUS:
        raise ValueError(f"unknown status '{args.status}', expected one of {STAGE_STATUS}")
    state["stages"][args.stage] = args.status
    if args.stage == "test" and args.status in ("done", "failed"):
        state["test_attempts"] = state.get("test_attempts", 0) + 1
    _write(state)
    return state


def cmd_checkpoint(args) -> dict:
    state = _read(args.run_id)
    if args.name not in CHECKPOINTS:
        raise ValueError(f"unknown checkpoint '{args.name}', expected one of {CHECKPOINTS}")
    if args.status not in CHECKPOINT_STATUS:
        raise ValueError(f"unknown status '{args.status}', expected one of {CHECKPOINT_STATUS}")
    state["checkpoints"][args.name] = args.status
    _write(state)
    return state


def cmd_note(args) -> dict:
    state = _read(args.run_id)
    state["notes"].append(args.text)
    _write(state)
    return state


def cmd_field(args) -> dict:
    state = _read(args.run_id)
    state[args.key] = args.value
    _write(state)
    return state


def cmd_get(args) -> dict:
    return _read(args.run_id)


def cmd_list(args) -> list:
    out = []
    for fn in sorted(os.listdir(_runs_dir())):
        if not fn.endswith(".json"):
            continue
        with open(os.path.join(_runs_dir(), fn), encoding="utf-8") as f:
            st = _normalize(json.load(f))
        is_open = st.get("stages", {}).get("deliver") != "done"
        if args.open and not is_open:
            continue
        out.append({
            "run_id": st.get("run_id"),
            "task_id": st.get("task_id"),
            "project": st.get("project"),
            "type": st.get("type"),
            "tier": st.get("tier"),
            "mode": st.get("mode"),
            "stages": st.get("stages"),
            "open": is_open,
        })
    return out


# --- Evidence gates & guarded transitions ---

def _summarize_runner(data: dict) -> str:
    """One-line fallback summary when a runner JSON has no 'summary' field."""
    if data.get("error"):
        return data.get("message") or "error"
    return f"{data.get('kind', '?')} exit={data.get('exit_code')}"


def _file_verdict(data: dict) -> str:
    """Map a test_runner / probe JSON result to a gate verdict."""
    if data.get("error"):
        return "error"
    if data.get("timed_out"):
        return "fail"
    return "pass" if data.get("passed") else "fail"


def _gate_verdict(state: dict, gate: str) -> str:
    """Effective verdict for a gate: pass|fail|error|waived|missing.

    The 'ac' gate is DERIVED from the acceptance-criteria ledger rather than
    recorded directly: empty ledger -> pass (nothing to prove); all met/waived
    -> pass; any still open -> fail.
    """
    if gate == "ac":
        acs = state.get("acceptance_criteria", [])
        if not acs:
            return "pass"
        return "pass" if all(a.get("status") in ("met", "waived") for a in acs) else "fail"
    g = state.get("gates", {}).get(gate)
    if not g:
        return "missing"
    return g.get("verdict", "missing")


def policy(state: dict, stage: str) -> dict:
    """Decide whether <stage> may advance to done. Pure: never writes.

    Returns {allowed, missing, advisory, reason}. The single Hybrid fork is the
    `if mode == "auto"` branch: in auto an unmet gate blocks; in checkpoint it is
    surfaced (informs the human approver) but does not block.
    """
    if stage not in STAGES:
        return {"allowed": False, "missing": [], "advisory": [],
                "reason": f"unknown stage '{stage}', expected one of {STAGES}"}

    mode = state.get("mode", "checkpoint")

    # (a) order guard — every earlier stage must be done or skipped
    idx = STAGES.index(stage)
    for prior in STAGES[:idx]:
        st = state.get("stages", {}).get(prior)
        if st not in ("done", "skipped"):
            return {"allowed": False, "missing": [], "advisory": [],
                    "reason": f"prior stage '{prior}' not done (is '{st}')"}

    # (b) required gates must be pass|waived — static per-stage set, plus any
    # per-run extras (e.g. `require <RID> verify` when the task touches runtime).
    missing = []
    extra = state.get("required_extra", {}).get(stage, [])
    for gate in list(REQUIRED_GATES.get(stage, [])) + [g for g in extra
                                                       if g not in REQUIRED_GATES.get(stage, [])]:
        v = _gate_verdict(state, gate)
        if v not in ("pass", "waived"):
            missing.append(f"{gate}:{v}")

    # advisory gates never block; just report any that are not pass|waived
    advisory = [
        f"{g}:{info.get('verdict')}"
        for g, info in state.get("gates", {}).items()
        if g in ADVISORY_GATES and info.get("verdict") not in ("pass", "waived")
    ]

    # (c) the single Hybrid fork
    if missing and mode == "auto":
        return {"allowed": False, "missing": missing, "advisory": advisory,
                "reason": f"auto mode: blocked by {len(missing)} unmet gate(s)"}
    if missing:
        reason = (f"checkpoint mode: advancing with {len(missing)} unmet gate(s) "
                  f"-> surface for human review")
    else:
        reason = "all required gates satisfied"
    return {"allowed": True, "missing": missing, "advisory": advisory, "reason": reason}


def cmd_record_gate(args) -> dict:
    if args.verdict not in GATE_VERDICTS:
        raise ValueError(f"unknown verdict '{args.verdict}', expected one of {GATE_VERDICTS}")
    state = _read(args.run_id)
    summary = args.summary
    detail_ref = None
    kind = args.kind
    warning = None
    if args.json:
        if not os.path.isfile(args.json):
            raise ValueError(f"json file not found: {args.json}")
        with open(args.json, encoding="utf-8") as f:
            data = json.load(f)
        detail_ref = os.path.abspath(args.json)
        if summary is None:
            summary = data.get("summary") or _summarize_runner(data)
        if kind is None:
            kind = data.get("kind")
        fv = _file_verdict(data)
        if args.verdict != fv:
            warning = f"recorded verdict '{args.verdict}' differs from json result '{fv}'"
    state.setdefault("gates", {})[args.gate] = {
        "verdict": args.verdict,
        "ts": _now(),
        "summary": summary,
        "detail_ref": detail_ref,
        "kind": kind,
    }
    _write(state)
    out = dict(state["gates"][args.gate])
    out["run_id"] = args.run_id
    out["gate"] = args.gate
    if warning:
        out["warning"] = warning
    return out


def cmd_advance(args) -> dict:
    state = _read(args.run_id)
    stage = args.stage
    if stage not in STAGES:
        raise ValueError(f"unknown stage '{stage}', expected one of {STAGES}")
    decision = policy(state, stage)
    from_status = state.get("stages", {}).get(stage)
    result = {
        "run_id": args.run_id,
        "stage": stage,
        "from": from_status,
        "to": from_status,
        "mode": state.get("mode"),
        "tier": state.get("tier"),
        "allowed": decision["allowed"],   # the true policy verdict (kept for audit)
        "forced": False,
        "written": False,
        "missing": decision["missing"],
        "advisory": decision["advisory"],
        "reason": decision["reason"],
    }
    write = decision["allowed"]
    if not write and args.force:
        write = True
        result["forced"] = True
        result["reason"] = "forced (policy denied): " + decision["reason"]
        state["notes"].append(f"advance {stage} forced; unmet gates: {decision['missing']}")
    if write:
        state["stages"][stage] = "done"
        if stage == "test":
            state["test_attempts"] = state.get("test_attempts", 0) + 1
        result["to"] = "done"
        result["written"] = True
        _write(state)
    return result


def cmd_require(args) -> dict:
    """Promote a gate to REQUIRED for this run (per-run, per-stage).

    Use case: verify_gen decides the task touches runtime behaviour -> the
    `verify` gate (flow_check result) must pass before the Test stage advances,
    even though globally it stays advisory for tasks that never run code.
    """
    state = _read(args.run_id)
    stage = args.stage
    if stage not in STAGES:
        raise ValueError(f"unknown stage '{stage}', expected one of {STAGES}")
    extra = state.setdefault("required_extra", {}).setdefault(stage, [])
    if args.gate not in extra:
        extra.append(args.gate)
        state["notes"].append(f"gate '{args.gate}' promoted to REQUIRED on stage '{stage}'")
        _write(state)
    return {"run_id": args.run_id, "stage": stage, "required_extra": extra}


def cmd_ac_add(args) -> dict:
    state = _read(args.run_id)
    acs = state.setdefault("acceptance_criteria", [])
    ac_id = args.id or f"AC{len(acs) + 1}"
    if any(a.get("id") == ac_id for a in acs):
        raise ValueError(f"acceptance criterion id already exists: {ac_id}")
    acs.append({"id": ac_id, "text": args.text, "evidence": None, "status": "open"})
    _write(state)
    return state


def _evidence_from_verify(ac_id: str, verify_json: str) -> str:
    """Pull hard evidence for an AC from a flow_check result: the scenario step
    named '<AC_ID>: ...' must exist AND have passed. A failed/missing step is an
    error — hand-waving 'unit test passed' must not close a data/behaviour AC."""
    import re as _re
    with open(verify_json, encoding="utf-8") as f:
        data = json.load(f)
    # 'AC1:' / 'AC1-' / 'AC1 ' đều tính — agent sinh scenario hay đổi dấu phân cách.
    rx = _re.compile(rf"^\s*{_re.escape(str(ac_id))}\s*[:\-_ ]", _re.IGNORECASE)
    hits = [s for s in data.get("steps", []) if rx.match(str(s.get("name", "")))]
    if not hits:
        raise ValueError(f"no verify step named '{ac_id}: ...' in {verify_json} — "
                         f"regenerate the scenario (verify_gen) to cover this AC")
    failed = [s for s in hits if not s.get("passed")]
    if failed:
        raise ValueError(f"verify step for {ac_id} FAILED ('{failed[0].get('name')}') — "
                         f"fix the code, re-run flow_check; do not map this AC")
    names = "; ".join(s.get("name", "") for s in hits)
    return f"verify: {names} [passed, {os.path.basename(verify_json)}]"


def cmd_ac_map(args) -> dict:
    evidence = args.evidence
    if getattr(args, "verify_json", None):
        evidence = _evidence_from_verify(args.id, args.verify_json)
    if not evidence:
        raise ValueError("pass --evidence \"...\" or --verify-json <flow_check result>")
    state = _read(args.run_id)
    for a in state.get("acceptance_criteria", []):
        if a.get("id") == args.id:
            a["evidence"] = evidence
            a["status"] = "met"
            _write(state)
            return state
    raise ValueError(f"acceptance criterion not found: {args.id}")


def cmd_ac_waive(args) -> dict:
    state = _read(args.run_id)
    for a in state.get("acceptance_criteria", []):
        if a.get("id") == args.id:
            a["status"] = "waived"
            state["notes"].append(f"AC {args.id} waived: {args.note}")
            _write(state)
            return state
    raise ValueError(f"acceptance criterion not found: {args.id}")


def main() -> int:
    parser = argparse.ArgumentParser(description="auto-dev pipeline run-log / state machine")
    sub = parser.add_subparsers(dest="action", required=True)

    p = sub.add_parser("init")
    p.add_argument("run_id")
    p.add_argument("--task", default=None)
    p.add_argument("--project", default=None)
    p.add_argument("--type", default=None, help="bugfix | feature | ...")
    p.add_argument("--title", default=None)
    p.add_argument("--tier", default=None, help="trivial | standard | complex (default standard)")
    p.add_argument("--mode", default=None, help="auto | checkpoint (default checkpoint)")

    p = sub.add_parser("stage")
    p.add_argument("run_id")
    p.add_argument("stage")
    p.add_argument("status")

    p = sub.add_parser("checkpoint")
    p.add_argument("run_id")
    p.add_argument("name")
    p.add_argument("status")

    p = sub.add_parser("note")
    p.add_argument("run_id")
    p.add_argument("text")

    p = sub.add_parser("field")
    p.add_argument("run_id")
    p.add_argument("key")
    p.add_argument("value")

    p = sub.add_parser("record-gate", help="record a gate result (evidence) against a run")
    p.add_argument("run_id")
    p.add_argument("gate", help="test | lint | build | grounding | review | integration | ...")
    p.add_argument("--verdict", required=True, choices=GATE_VERDICTS)
    p.add_argument("--summary", default=None)
    p.add_argument("--json", default=None, help="path to a runner/probe JSON result to derive summary from")
    p.add_argument("--kind", default=None)

    p = sub.add_parser("advance", help="advance a stage to done iff its gates are satisfied (policy-guarded)")
    p.add_argument("run_id")
    p.add_argument("stage")
    p.add_argument("--force", action="store_true", help="write done even if policy denies (kept in audit trail)")

    p = sub.add_parser("ac-add", help="add an acceptance criterion to the ledger")
    p.add_argument("run_id")
    p.add_argument("--text", required=True)
    p.add_argument("--id", default=None, help="auto-assigned (AC<n>) if omitted")

    p = sub.add_parser("require", help="promote a gate to REQUIRED for this run (e.g. verify)")
    p.add_argument("run_id")
    p.add_argument("gate")
    p.add_argument("--stage", default="test", help="stage the gate must pass on (default test)")

    p = sub.add_parser("ac-map", help="mark an acceptance criterion met, with evidence")
    p.add_argument("run_id")
    p.add_argument("id")
    p.add_argument("--evidence", default=None)
    p.add_argument("--verify-json", default=None,
                   help="flow_check result: evidence = step '<AC_ID>: ...' (must have PASSED)")

    p = sub.add_parser("ac-waive", help="waive an acceptance criterion with a note")
    p.add_argument("run_id")
    p.add_argument("id")
    p.add_argument("--note", default="")

    p = sub.add_parser("get")
    p.add_argument("run_id")

    p = sub.add_parser("list")
    p.add_argument("--open", action="store_true", help="only runs whose deliver stage is not done")

    args = parser.parse_args()
    handlers = {
        "init": cmd_init, "stage": cmd_stage, "checkpoint": cmd_checkpoint,
        "note": cmd_note, "field": cmd_field, "get": cmd_get, "list": cmd_list,
        "record-gate": cmd_record_gate, "advance": cmd_advance, "require": cmd_require,
        "ac-add": cmd_ac_add, "ac-map": cmd_ac_map, "ac-waive": cmd_ac_waive,
    }
    try:
        out = handlers[args.action](args)
    except ValueError as e:
        out = {"error": True, "message": str(e)}

    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 1 if isinstance(out, dict) and out.get("error") else 0


if __name__ == "__main__":
    sys.exit(main())
