"""Architecture ratchet (design §12.3 "bánh cóc", checklist F.10): rules + a list of known violations
that may only SHRINK. "142 -> 0" is the progress bar of a large refactor.

    .agent/architecture.json   {"rules": [{"id": "web-no-repo", "files": "(^|/)web/.*\\\\.java$",
                                           "deny": "^\\\\s*import\\\\s+.*\\\\.repository\\\\.", "why": "..."}]}
    .agent/ratchet-baseline.json   {"violations": {"<rule>|<file>|<normalized line>": count}}

Both live under .agent/ (protected: a fixer may not touch them) and the ladder rung reads them from
the BASE ref, so a branch cannot loosen its own rules. A violation is identified by rule + file +
normalized line text (no line numbers: moving code does not create "new" violations).
"""

import json
import re
import subprocess

RULES_FILE = ".agent/architecture.json"
BASELINE_FILE = ".agent/ratchet-baseline.json"
MAX_FILE = 2 * 2 ** 20


def parse_rules(text: str) -> list:
    try:
        doc = json.loads(text)
    except ValueError as e:
        raise ValueError(f"{RULES_FILE}: not JSON ({e})")
    rules = doc.get("rules") if isinstance(doc, dict) else None
    if not isinstance(rules, list) or not rules:
        raise ValueError(f"{RULES_FILE}: needs a non-empty `rules` list")
    out, seen = [], set()
    for r in rules:
        if not isinstance(r, dict) or not all(isinstance(r.get(k), str) and r.get(k) for k in ("id", "files", "deny")):
            raise ValueError(f"{RULES_FILE}: every rule needs string `id`, `files` (path regex), `deny` (line regex)")
        if r["id"] in seen or "|" in r["id"]:
            raise ValueError(f"{RULES_FILE}: rule id {r['id']!r} is duplicated or contains '|'")
        seen.add(r["id"])
        try:
            out.append({**r, "_files": re.compile(r["files"]), "_deny": re.compile(r["deny"])})
        except re.error as e:
            raise ValueError(f"{RULES_FILE}: rule {r['id']!r} has a bad regex: {e}")
    return out


def parse_baseline(text: str) -> dict:
    if not text:
        return {}
    try:
        doc = json.loads(text)
    except ValueError as e:
        raise ValueError(f"{BASELINE_FILE}: not JSON ({e})")
    v = doc.get("violations") if isinstance(doc, dict) else None
    if not isinstance(v, dict) or not all(isinstance(k, str) and isinstance(n, int) and n > 0 for k, n in v.items()):
        raise ValueError(f"{BASELINE_FILE}: `violations` must map 'rule|file|line' -> positive count")
    return v


def _norm_line(line: str) -> str:
    return " ".join(line.strip().split())[:300]


def tracked_files(repo: str) -> list:
    p = subprocess.run(["git", "-C", repo, "ls-files", "-z"], capture_output=True, timeout=120)
    if p.returncode != 0:
        raise RuntimeError(f"git ls-files failed: {p.stderr.decode('utf-8', 'replace')[-200:]}")
    return [f for f in p.stdout.decode("utf-8", "replace").split("\0") if f]


def violations(repo: str, rules: list, read=None) -> dict:
    """{"rule|file|line": count} over the tracked files. `read(path)` -> text (default: working tree)."""
    import os
    if read is None:
        def read(rel):
            path = os.path.join(repo, rel)
            if os.path.islink(path) or os.path.getsize(path) > MAX_FILE:
                return ""
            with open(path, encoding="utf-8", errors="replace") as f:
                return f.read()
    out = {}
    for rel in tracked_files(repo):
        applicable = [r for r in rules if r["_files"].search(rel)]
        if not applicable:
            continue
        try:
            text = read(rel)
        except OSError:
            continue
        for line in text.splitlines():
            for r in applicable:
                if r["_deny"].search(line):
                    k = f"{r['id']}|{rel}|{_norm_line(line)}"
                    out[k] = out.get(k, 0) + 1
    return out


def compare(baseline: dict, now: dict) -> dict:
    """new = violations beyond the baseline (per identity); fixed = baseline entries gone."""
    new = {k: n - baseline.get(k, 0) for k, n in now.items() if n > baseline.get(k, 0)}
    fixed = {k: n - now.get(k, 0) for k, n in baseline.items() if n > now.get(k, 0)}
    return {"baseline": sum(baseline.values()), "now": sum(now.values()), "new": new, "fixed": fixed,
            "passed": not new}


def by_rule(counts: dict) -> dict:
    out = {}
    for k, n in counts.items():
        rid = k.split("|", 1)[0]
        out[rid] = out.get(rid, 0) + n
    return out


def baseline_doc(counts: dict) -> str:
    return json.dumps({"violations": dict(sorted(counts.items()))}, ensure_ascii=False, indent=1) + "\n"
