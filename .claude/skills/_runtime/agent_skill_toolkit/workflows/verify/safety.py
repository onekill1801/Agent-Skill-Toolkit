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
import math
import os
import re

# what must never reach a task error / an agent prompt from a command's output (Codex F.10c #5)
_SECRET = re.compile(r"(glpat-[\w-]{10,}|gh[pousr]_[A-Za-z0-9]{20,}|xox[abp]-[\w-]{10,}|AKIA[0-9A-Z]{16}|"
                     r"eyJ[\w-]{10,}\.[\w-]{10,}\.[\w-]{5,}|(?i:bearer)\s+[\w.~+/=-]{16,}|"
                     r"(?i:(?:password|passwd|pwd|secret|token|api[_-]?key|private[_-]?key)[\"']?\s*[:=]\s*)[^\s,;\"']{4,}|"
                     r"[A-Za-z0-9+/_-]{40,}={0,2})")


def redact(text: str) -> str:
    return _SECRET.sub("[REDACTED]", text or "")


def repo_path(repo: str, rel: str) -> str:
    """Absolute path of a repo-relative file that PHYSICALLY stays inside the repo: no absolute / `..`
    paths, no symlink or junction anywhere on the way (Codex F.10c #2/#3). Raises ValueError."""
    raw = str(rel or "").replace("\\", "/")
    if not raw or raw.startswith("/") or re.match(r"^[A-Za-z]:", raw) or ".." in raw.split("/"):
        raise ValueError(f"path {rel!r} must be relative to the repo, without '..'")
    root = os.path.realpath(repo)
    isjunction = getattr(os.path, "isjunction", lambda p: False)

    def within(p):
        try:
            return os.path.commonpath([os.path.normcase(p), os.path.normcase(root)]) == os.path.normcase(root)
        except ValueError:
            return False
    cur = root
    for part in [p for p in raw.split("/") if p not in ("", ".")]:
        cur = os.path.join(cur, part)
        # every EXISTING component must be a plain entry that resolves inside the repo
        if os.path.islink(cur) or isjunction(cur) or (os.path.lexists(cur) and not within(os.path.realpath(cur))):
            raise ValueError(f"path {rel!r} goes through a link / junction ({cur})")
    full = os.path.normpath(cur)
    if not within(full):
        raise ValueError(f"path {rel!r} leaves the repo")
    return full


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


def _strict_json(text: str):
    """JSON without NaN / Infinity (Python accepts them by default — Codex F.10c #6)."""
    def bad(c):
        raise ValueError(f"non-finite number {c} is not allowed")
    return json.loads(text, parse_constant=bad)


def _finite(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def parse_baseline(text: str) -> dict:
    """{"metrics": {name: {"value": number, "higher_is_better": bool, "tolerance": fraction}}}"""
    doc = _strict_json(text)
    m = doc.get("metrics") if isinstance(doc, dict) else None
    if not isinstance(m, dict) or not m:
        raise ValueError("baseline needs a non-empty `metrics` object")
    out = {}
    for name, spec in m.items():
        if not isinstance(spec, dict) or not _finite(spec.get("value")):
            raise ValueError(f"baseline metric {name!r} needs a finite numeric `value`")
        tol = spec.get("tolerance", 0.05)
        if not isinstance(tol, (int, float)) or not 0 <= tol < 1:
            raise ValueError(f"baseline metric {name!r}: tolerance must be a fraction in [0, 1)")
        out[name] = {"value": float(spec["value"]), "higher_is_better": bool(spec.get("higher_is_better", False)),
                     "tolerance": float(tol)}
    return out


def parse_metrics(text: str) -> dict:
    doc = _strict_json(text)
    if not isinstance(doc, dict):
        raise ValueError("metrics output must be a JSON object {name: number}")
    out = {}
    for k, v in doc.items():
        if not _finite(v):
            raise ValueError(f"metric {k!r} is not a finite number: {v!r}")
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
