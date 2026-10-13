"""Architecture ratchet (design §12.3 "bánh cóc", checklist F.10): rules + a list of known violations
that may only SHRINK. "142 -> 0" is the progress bar of a large refactor.

    .agent/architecture.json   {"rules": [{"id": "web-no-repo", "files": "(^|/)web/.*\\\\.java$",
                                           "package": "\\\\.web(\\\\.|$)",          # optional
                                           "deny": "^import\\\\s+.*\\\\.repository\\\\.", "why": "..."}]}
    .agent/ratchet-baseline.json   {"violations": {"<rule>|<file>|<statement>": count}}

Both live under .agent/ (protected: a fixer may not touch them); the ladder rung reads them from the
BASE ref and refuses a HEAD that weakens them (Codex F.10a #1). Matching is on LOGICAL statements
(comments stripped, whitespace and line breaks collapsed, split on `;`/braces for C-like files, `ast`
imports for Python), so reformatting cannot hide a violation (#3). A rule covers a file by path
(`files`) OR by declared package (`package`), so moving a file elsewhere does not launder it (#4).
A covered file that cannot be scanned (too big, symlink, unreadable) FAILS the check (#2). Scans run
in a child process with a time limit (a pathological rule regex cannot hang the ladder, #8).
"""

import hashlib
import json
import os
import re
import subprocess
import sys

RULES_FILE = ".agent/architecture.json"
BASELINE_FILE = ".agent/ratchet-baseline.json"
MAX_FILE = 2 * 2 ** 20
C_LIKE = (".java", ".kt", ".kts", ".scala", ".groovy", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".cs",
          ".go", ".php", ".c", ".cc", ".cpp", ".h", ".hpp", ".swift", ".dart", ".rs")
PKG_RX = re.compile(r"^\s*package\s+([\w.]+)", re.M)


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
            raise ValueError(f"{RULES_FILE}: every rule needs string `id`, `files` (path regex), `deny` (statement regex)")
        if r.get("package") is not None and not (isinstance(r["package"], str) and r["package"]):
            raise ValueError(f"{RULES_FILE}: rule {r['id']!r}: `package` must be a non-empty regex")
        if r["id"] in seen or "|" in r["id"]:
            raise ValueError(f"{RULES_FILE}: rule id {r['id']!r} is duplicated or contains '|'")
        seen.add(r["id"])
        try:
            out.append({**r, "_files": re.compile(r["files"]), "_deny": re.compile(r["deny"]),
                        "_pkg": re.compile(r["package"]) if r.get("package") else None})
        except re.error as e:
            raise ValueError(f"{RULES_FILE}: rule {r['id']!r} has a bad regex: {e}")
    return out


def rule_spec(r: dict) -> dict:
    return {k: r.get(k) for k in ("files", "deny", "package")}


def parse_baseline(text: str) -> dict:
    if not text:
        return {}
    try:
        doc = json.loads(text)
    except ValueError as e:
        raise ValueError(f"{BASELINE_FILE}: not JSON ({e})")
    v = doc.get("violations") if isinstance(doc, dict) else None
    if not isinstance(v, dict) or not all(isinstance(k, str) and isinstance(n, int) and n > 0 for k, n in v.items()):
        raise ValueError(f"{BASELINE_FILE}: `violations` must map 'rule|file|statement' -> positive count")
    return v


def policy_weakened(base_rules: list, head_rules: list, base_bl: dict, head_bl: dict) -> list:
    """Why HEAD's policy is weaker than BASE's ([] = not weaker). Rules may be ADDED; an existing rule
    may not be removed or changed; the baseline may only shrink (Codex F.10a #1)."""
    why = []
    head = {r["id"]: rule_spec(r) for r in head_rules}
    for r in base_rules:
        if r["id"] not in head:
            why.append(f"rule {r['id']!r} removed")
        elif head[r["id"]] != rule_spec(r):
            why.append(f"rule {r['id']!r} changed")
    grown = [k for k, n in head_bl.items() if n > base_bl.get(k, 0)]
    if grown:
        why.append(f"baseline grew by {len(grown)} entr(y/ies), e.g. {grown[0][:120]}")
    return why


# ----------------------------------------------------------------------------- statements

def _strip_c_comments(text: str) -> str:
    out, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if c in "\"'`":                                   # keep string literals intact
            j = i + 1
            while j < n and text[j] != c:
                j += 2 if text[j] == "\\" else 1
            out.append(text[i:j + 1])
            i = j + 1
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
            out.append(" ")
        else:
            out.append(c)
            i += 1
    return "".join(out)


def statements(rel: str, text: str) -> list:
    """Logical statements a rule is matched against (whitespace collapsed)."""
    if rel.endswith(".py"):
        import ast
        try:
            tree = ast.parse(text)
        except SyntaxError:
            tree = None
        out = []
        if tree is not None:
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    out += [f"import {a.name}" for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    out += [f"from {'.' * node.level}{node.module or ''} import {a.name}" for a in node.names]
        body = re.sub(r"\\\n", " ", text)                  # joined continuation lines
        out += [" ".join(ln.split()) for ln in body.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
        return out
    if rel.endswith(C_LIKE):
        flat = " ".join(_strip_c_comments(text).split())
        flat = re.sub(r"\s*\.\s*", ".", flat)            # `a . b` is `a.b` in these languages
        return [s.strip() for s in re.split(r"[;{}]", flat) if s.strip()]
    return [" ".join(ln.split()) for ln in text.splitlines() if ln.strip()]


def identity(rule_id: str, rel: str, stmt: str) -> str:
    if len(stmt) <= 300:
        return f"{rule_id}|{rel}|{stmt}"
    return f"{rule_id}|{rel}|{stmt[:200]}…#{hashlib.sha256(stmt.encode('utf-8')).hexdigest()[:16]}"   # no collisions (#5)


def tracked_files(repo: str) -> list:
    p = subprocess.run(["git", "-C", repo, "ls-files", "-z"], capture_output=True, timeout=120)
    if p.returncode != 0:
        raise RuntimeError(f"git ls-files failed: {p.stderr.decode('utf-8', 'replace')[-200:]}")
    return [f for f in p.stdout.decode("utf-8", "replace").split("\0") if f]


def scan(repo: str, rules: list) -> dict:
    """{"violations": {identity: count}, "unscanned": [{"file", "why"}]} over the tracked files."""
    out, unscanned = {}, []
    for rel in tracked_files(repo):
        by_path = [r for r in rules if r["_files"].search(rel)]
        maybe_pkg = [r for r in rules if r["_pkg"] is not None and r not in by_path]
        if not by_path and not maybe_pkg:
            continue
        path = os.path.join(repo, rel)
        try:
            if os.path.islink(path):
                raise OSError("symlink")
            if os.path.getsize(path) > MAX_FILE:
                raise OSError(f"larger than {MAX_FILE} bytes")
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError as e:
            if by_path:                                    # a covered file we cannot read: fail closed
                unscanned.append({"file": rel, "why": str(e)[:120]})
            continue
        applicable = list(by_path)
        if maybe_pkg:
            m = PKG_RX.search(text)
            pkg = m.group(1) if m else ""
            if rel.endswith(".py"):
                pkg = os.path.splitext(rel)[0].replace("/", ".")
            applicable += [r for r in maybe_pkg if pkg and r["_pkg"].search(pkg)]
        if not applicable:
            continue
        for stmt in statements(rel, text):
            for r in applicable:
                if r["_deny"].search(stmt):
                    k = identity(r["id"], rel, stmt)
                    out[k] = out.get(k, 0) + 1
    return {"violations": out, "unscanned": unscanned}


def scan_isolated(repo: str, rules_text: str, timeout: float = 300) -> dict:
    """scan() in a child process killed at `timeout` (regex DoS in a rule cannot hang the caller).
    Raises TimeoutError / RuntimeError — callers fail closed."""
    from agent_skill_toolkit.core import proc
    try:
        p = proc.run([sys.executable, os.path.abspath(__file__), "--scan"],
                     input=json.dumps({"repo": repo, "rules": rules_text}), timeout=timeout)
    except subprocess.TimeoutExpired:
        raise TimeoutError(f"ratchet scan exceeded {timeout:.0f}s (a rule regex may be too expensive)")
    if p.returncode != 0:
        raise RuntimeError(f"ratchet scan failed: {(p.stderr or p.stdout or '')[-300:]}")
    return json.loads((p.stdout or "").strip().splitlines()[-1])


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


if __name__ == "__main__" and sys.argv[1:] == ["--scan"]:
    req = json.loads(sys.stdin.read())
    res = scan(req["repo"], parse_rules(req["rules"]))
    sys.stdout.write(json.dumps(res, ensure_ascii=True) + "\n")      # ASCII: any console encoding
