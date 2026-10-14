"""Safety net for large refactors (design §12.3 ②, checklist F.10c): pin BEHAVIOUR and NUMBERS before
the code moves, then prove every slice keeps them.

    snapshot   a command's output (API contract dump, golden scenario output, CLI help, ...) must equal
               the snapshot recorded on the BASE ref — any difference stops for a human (a contract change
               is a decision, not a fix)
    metrics    a command prints {"metric": number}; each must stay within tolerance of the baseline
               recorded on the BASE ref (`higher_is_better`, `tolerance`) — "performance >= baseline"

Spec (verify: ladder:), commands declare `protects` like every rung that runs something:
    contract:
      kind: snapshot
      run: [python, scripts/dump_api.py]      # stdout, or `output: build/api.json`
      snapshot: .agent/snapshots/contract.json
      format: json                            # json (key order / spacing ignored) | text
      protects: [scripts/dump_api.py]
    perf:
      kind: metrics
      run: [python, scripts/bench.py]
      baseline: .agent/baseline.json
      protects: [scripts/bench.py]

Record / refresh the files on purpose: `refactor.py snapshot record` / `refactor.py metrics record`
(then review and commit them — they live under .agent/, which the fixer may not touch).
"""

import difflib
import json


def normalize(text: str, fmt: str = "text") -> str:
    if fmt == "json":
        return json.dumps(json.loads(text), sort_keys=True, indent=1, ensure_ascii=False) + "\n"
    lines = [ln.rstrip() for ln in (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines) + "\n"


def compare_snapshot(expected: str, actual: str, fmt: str = "text", context: int = 2, limit: int = 60) -> dict:
    """{"equal": bool, "diff": [...lines]} — both sides normalized the same way."""
    try:
        a, b = normalize(expected, fmt), normalize(actual, fmt)
    except ValueError as e:
        return {"equal": False, "diff": [f"not valid {fmt}: {e}"]}
    if a == b:
        return {"equal": True, "diff": []}
    diff = list(difflib.unified_diff(a.splitlines(), b.splitlines(), "base snapshot", "this branch", n=context,
                                     lineterm=""))
    return {"equal": False, "diff": diff[:limit] + ([f"... {len(diff) - limit} more lines"] if len(diff) > limit else [])}


def parse_baseline(text: str) -> dict:
    """{"metrics": {name: {"value": number, "higher_is_better": bool, "tolerance": fraction}}}"""
    doc = json.loads(text)
    m = doc.get("metrics") if isinstance(doc, dict) else None
    if not isinstance(m, dict) or not m:
        raise ValueError("baseline needs a non-empty `metrics` object")
    out = {}
    for name, spec in m.items():
        if not isinstance(spec, dict) or not isinstance(spec.get("value"), (int, float)) or isinstance(spec.get("value"), bool):
            raise ValueError(f"baseline metric {name!r} needs a numeric `value`")
        tol = spec.get("tolerance", 0.05)
        if not isinstance(tol, (int, float)) or not 0 <= tol < 1:
            raise ValueError(f"baseline metric {name!r}: tolerance must be a fraction in [0, 1)")
        out[name] = {"value": float(spec["value"]), "higher_is_better": bool(spec.get("higher_is_better", False)),
                     "tolerance": float(tol)}
    return out


def parse_metrics(text: str) -> dict:
    doc = json.loads(text)
    if not isinstance(doc, dict):
        raise ValueError("metrics output must be a JSON object {name: number}")
    out = {}
    for k, v in doc.items():
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError(f"metric {k!r} is not a number: {v!r}")
        out[k] = float(v)
    return out


def compare_metrics(baseline: dict, current: dict) -> dict:
    """{"passed", "rows": [{name, base, now, limit, ok}], "missing": [...]}"""
    rows, missing = [], []
    for name, b in sorted(baseline.items()):
        if name not in current:
            missing.append(name)
            continue
        v, now, tol = b["value"], current[name], b["tolerance"]
        margin = abs(v) * tol if v else tol
        limit = v - margin if b["higher_is_better"] else v + margin
        ok = now >= limit if b["higher_is_better"] else now <= limit
        rows.append({"name": name, "base": v, "now": now, "limit": round(limit, 6), "ok": ok,
                     "better": "higher" if b["higher_is_better"] else "lower"})
    return {"passed": not missing and all(r["ok"] for r in rows), "rows": rows, "missing": missing}


def baseline_doc(current: dict, previous: dict = None, higher_is_better: dict = None, tolerance: float = 0.05) -> str:
    """A baseline from measured values; settings (direction / tolerance) kept from the previous baseline."""
    previous, higher_is_better = previous or {}, higher_is_better or {}
    out = {}
    for name, value in sorted(current.items()):
        p = previous.get(name, {})
        out[name] = {"value": value, "higher_is_better": bool(p.get("higher_is_better", higher_is_better.get(name, False))),
                     "tolerance": p.get("tolerance", tolerance)}
    return json.dumps({"metrics": out}, indent=1) + "\n"
