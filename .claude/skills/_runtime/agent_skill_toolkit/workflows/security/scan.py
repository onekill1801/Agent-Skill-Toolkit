"""Offline security scan of a product repo (checklist F.23) — rules.py + spring.py + a baseline.

    scan(repo, history=False) -> {"findings": [...], "endpoints": [...], "stats": {...}}
    apply_baseline(findings, baseline) -> marks each finding new / known
    baseline_doc(findings) -> the JSON document to store as the accepted state

A finding: {rule, severity, category, title, file, line, text (masked when it is a credential), fix, fp}.
`fp` (fingerprint) = rule + file + the normalized line text — it survives the line moving, so a baseline
keeps working while code is edited around it. An inline `security-audit: ignore [RULE-ID] <reason>` on the
line (or the line above) suppresses it there.

Never sends anything anywhere; reads files and (with history=True) `git log -p`.
"""

import fnmatch
import hashlib
import os
import re
import subprocess
import time

from . import rules as R
from . import spring

SKIP_DIRS = {".git", "node_modules", "target", "build", "dist", "out", ".gradle", ".idea", ".mvn", ".vscode",
             "coverage", "vendor", ".angular", "__pycache__", ".next", ".nuxt", "bin", "obj", ".codegraph",
             ".claude", ".agents", ".agent"}
MAX_FILE = 2 * 1024 * 1024
SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2, "info": 3}
_IGNORE = re.compile(r"security-audit:\s*ignore\b\s*([A-Z]+-\d{3})?", re.I)
_TEST_PATH = re.compile(r"(?:^|/)(?:src/test|src/it|test|tests|__tests__|spec|fixtures?|testdata|e2e)(?:/|$)|"
                        r"(?:Test|Tests|IT|Spec)\.(?:java|kt|ts|js)$|\.(?:spec|test)\.[jt]sx?$")


def is_test(rel: str) -> bool:
    return bool(_TEST_PATH.search(rel))


def _match(rel: str, globs) -> bool:
    name = rel.rsplit("/", 1)[-1]
    return any(g == "*" or fnmatch.fnmatch(name, g) for g in globs)


def mask(text: str, spans=None) -> str:
    """A credential shown without its secret. `spans` [(start, end)] = the secrets themselves: only the
    first 3 characters of EACH stay. Then every known token shape left anywhere on the line is masked too
    (a line can carry several credentials). Without spans, every run of 4+ non-space chars keeps 3."""
    if spans is None:
        return re.sub(r"(\S{3})\S+", r"\1…", text)
    out, pos = [], 0
    for a, b in sorted(spans):
        if a < pos:
            continue
        out += [text[pos:a], text[a:a + 3], "…"]
        pos = b
    out.append(text[pos:])
    shown = "".join(out)
    for rid in ("SEC-001", "SEC-002"):
        shown = R.BY_ID[rid].pattern.sub(lambda m: m.group(0)[:3] + "…", shown)
    return shown


def fingerprint(rule: str, rel: str, text: str) -> str:
    norm = re.sub(r"\s+", " ", text.strip())
    return hashlib.sha256(f"{rule}|{rel}|{norm}".encode("utf-8")).hexdigest()[:16]


def _finding(rule, rel, line_no, text, secret=False, detail="", spans=None) -> dict:
    if secret:
        stripped = text.lstrip()
        off = len(text) - len(stripped)
        shown = mask(stripped.rstrip(), [(x - off, y - off) for x, y in spans] if spans is not None else None)
    else:
        shown = text.strip()
    return {"rule": rule.id if hasattr(rule, "id") else rule["id"],
            "severity": rule.severity if hasattr(rule, "severity") else rule["severity"],
            "category": rule.category if hasattr(rule, "category") else rule["category"],
            "title": rule.title if hasattr(rule, "title") else rule["title"],
            "fix": rule.fix if hasattr(rule, "fix") else rule["fix"],
            "file": rel, "line": line_no, "text": shown[:200], "detail": detail,
            "fp": fingerprint(rule.id if hasattr(rule, "id") else rule["id"], rel, text)}


def iter_files(repo: str):
    """(relpath with /, absolute path) of every text file worth scanning."""
    for d, dirs, files in os.walk(repo):
        dirs[:] = [x for x in dirs if x not in SKIP_DIRS and not x.startswith(".git")]
        for f in files:
            p = os.path.join(d, f)
            rel = os.path.relpath(p, repo).replace(os.sep, "/")
            try:
                if os.path.getsize(p) > MAX_FILE or os.path.islink(p):
                    continue
            except OSError:
                continue
            yield rel, p


def read_text(path: str):
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return None
    if b"\0" in raw[:8192]:
        return None                                       # binary
    return raw.decode("utf-8", errors="replace")


def _ignored(lines: list, i: int, rule_id: str) -> bool:
    for j in (i, i - 1):
        if 0 <= j < len(lines):
            m = _IGNORE.search(lines[j])
            if m and (not m.group(1) or m.group(1).upper() == rule_id):
                return True
    return False


# ----------------------------------------------------------------------------- config flattening

_YAML_KEY = re.compile(r"^(\s*)(-\s+)?([\w.\-\"'\[\]]+)\s*:(?:\s+|$)(.*)$")


def flatten_config(rel: str, text: str) -> list:
    """[(line_no, dotted.key, value)] of a YAML (indentation-based, the common subset) or .properties
    file. List items keep their parent key. Multi-document YAML (---) restarts the path."""
    out = []
    if rel.endswith(".properties"):
        for n, line in enumerate(text.splitlines(), 1):
            s = line.strip()
            if not s or s[0] in "#!":
                continue
            m = re.match(r"([^=:\s]+)\s*[=:]\s*(.*)$", s)
            if m:
                out.append((n, m.group(1), m.group(2).strip()))
        return out
    stack = []                                            # [(indent, key)]
    for n, line in enumerate(text.splitlines(), 1):
        if line.strip() in ("---", "...") or line.startswith("---"):
            stack = []
            continue
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = _YAML_KEY.match(line)
        if not m:
            continue
        indent = len(m.group(1)) + (len(m.group(2)) if m.group(2) else 0)
        key = m.group(3).strip("\"'")
        value = re.sub(r"\s+#.*$", "", m.group(4)).strip()
        while stack and stack[-1][0] >= indent:
            stack.pop()
        path = ".".join([k for _, k in stack] + [key])
        if value and value not in ("|", ">", "|-", ">-"):
            out.append((n, path, value.strip("\"'")))
        else:
            stack.append((indent, key))
    return out


def _is_prod_config(rel: str) -> bool:
    name = rel.rsplit("/", 1)[-1].lower()
    if "/test/" in f"/{rel}" or "-dev" in name or "-local" in name or "-test" in name or "-e2e" in name:
        return False
    return bool(re.match(r"(?:application|bootstrap)(?:-prod\w*)?\.(?:ya?ml|properties)$", name))


def _key_findings(rel: str, text: str) -> list:
    out = []
    lines = text.splitlines()
    for n, key, value in flatten_config(rel, text):
        for r in R.KEY_RULES:
            if r.prod_only and not _is_prod_config(rel):
                continue
            if not r.key.search(key) or not r.value.search(value):
                continue
            if r.id == "SEC-003" and (R.NOT_A_SECRET.search(value) or is_test(rel)):
                continue
            if _ignored(lines, n - 1, r.id):
                continue
            shown = f"{key}: {value}"
            out.append(_finding(r, rel, n, shown, secret=r.secret, spans=[(len(key) + 2, len(shown))]))
    return out


# ----------------------------------------------------------------------------- the scan

def scan(repo: str, history: bool = False, max_commits: int = 300) -> dict:
    t0 = time.time()
    findings, java, files_n = [], {}, 0
    for rel, path in iter_files(repo):
        text = read_text(path)
        if text is None:
            continue
        files_n += 1
        test = is_test(rel)
        if rel.endswith((".java", ".kt")) and not test:
            java[rel] = text
        lines = text.splitlines()
        if rel.endswith((".yml", ".yaml", ".properties")):
            findings += _key_findings(rel, text)
        code = None
        for r in R.RULES:
            if (r.main_only and test) or not _match(rel, r.files):
                continue
            if r.unless and r.unless.search(text):
                continue
            if code is None:                              # comments do not run: code rules see code only
                code = spring.strip_comments(text).splitlines() if rel.endswith((".java", ".kt", ".js", ".ts",
                                                                                 ".tsx", ".jsx")) else lines
            src = lines if r.category in ("secret", "leftover") else code
            for i, line in enumerate(src):
                m = r.pattern.search(line) if len(line) <= 4000 else None
                if not m:
                    continue
                if _ignored(lines, i, r.id):
                    continue
                raw = lines[i] if i < len(lines) else line
                spans = [x.span() for x in r.pattern.finditer(raw)] if r.secret else None
                f = _finding(r, rel, i + 1, raw, secret=r.secret, spans=spans)
                if r.secret and test:
                    f["severity"], f["detail"] = "low", "trong code test"
                findings.append(f)
    eps, ep_findings = spring.classify(spring.endpoints(java), spring.url_rules(java))
    by_id = {r["id"]: r for r in R.ENDPOINT_RULES}
    for e in ep_findings:
        findings.append(_finding(by_id[e["rule"]], e["file"], e["line"], e["text"], detail=e["detail"]))
    hist = history_secrets(repo, max_commits) if history else {"scanned": 0, "findings": []}
    findings += hist["findings"]
    findings.sort(key=lambda f: (SEVERITY_ORDER.get(f["severity"], 9), f["rule"], f["file"], f["line"]))
    return {"repo": os.path.abspath(repo), "findings": findings, "endpoints": eps,
            "stats": {"files": files_n, "java_controllers": len({e["file"] for e in eps}), "endpoints": len(eps),
                      "url_rules": len(spring.url_rules(java)), "history_commits": hist["scanned"],
                      "seconds": round(time.time() - t0, 1)}}


def history_secrets(repo: str, max_commits: int = 300) -> dict:
    """Credentials in lines ADDED by the last `max_commits` commits (a secret deleted later is still in
    history). Streams `git log -p`; reports the commit and file, never the value."""
    line_rules = [R.BY_ID[i] for i in R.HISTORY_RULES]
    key_rule = R.KEY_BY_ID["SEC-003"]
    cmd = ["git", "-C", repo, "log", "--all", "-p", "--no-color", "-U0", f"-n{int(max_commits)}",
           "--pretty=format:@@COMMIT %H %ad", "--date=short"]
    out, seen, scanned, commit, rel = [], set(), 0, "", ""
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                             encoding="utf-8", errors="replace")
    except OSError:
        return {"scanned": 0, "findings": [], "error": "git not available"}
    try:
        for line in p.stdout:
            if line.startswith("@@COMMIT "):
                commit, scanned = line.split()[1][:10] + " " + line.split()[-1], scanned + 1
                continue
            if line.startswith("+++ "):
                rel = line[6:].strip() if line.startswith("+++ b/") else ""
                continue
            if not line.startswith("+") or not rel or len(line) > 4000:
                continue
            added = line[1:].rstrip("\n")
            hits = [r for r in line_rules if r.pattern.search(added)]
            if rel.endswith((".yml", ".yaml", ".properties", ".env")) or "/.env" in rel:
                m = re.match(r"^\s*([\w.\-]+)\s*[:=]\s*['\"]?([^'\"#\s]+)", added)
                if m and key_rule.key.search(m.group(1)) and not R.NOT_A_SECRET.search(m.group(2)) \
                        and len(m.group(2)) >= 4:
                    hits.append(key_rule)
            for r in hits:
                fp = fingerprint("HIST-" + r.id, rel, added)
                if fp in seen:
                    continue
                seen.add(fp)
                spans = [x.span() for x in r.pattern.finditer(added)] if hasattr(r, "pattern") else None
                f = _finding(r, rel, 0, added, secret=True, detail=f"trong lịch sử git: commit {commit}",
                             spans=spans)
                f.update(rule="HIST-" + r.id, fp=fp, title="(lịch sử git) " + r.title,
                         fix="Đổi/thu hồi secret (đã lộ trong lịch sử); xoá khỏi lịch sử nếu repo được chia sẻ "
                             "(git filter-repo) — xoá ở commit mới KHÔNG đủ.")
                out.append(f)
    finally:
        p.stdout.close()
        try:
            p.wait(timeout=30)
        except subprocess.TimeoutExpired:
            p.kill()
    return {"scanned": scanned, "findings": out}


# ----------------------------------------------------------------------------- baseline

def apply_baseline(findings: list, baseline: dict) -> list:
    known = set(((baseline or {}).get("accepted") or {}).keys())
    for f in findings:
        f["status"] = "known" if f["fp"] in known else "new"
    return findings


def baseline_doc(findings: list, previous: dict = None, note: str = "") -> dict:
    """Every current finding (except info) accepted; reasons / dates of entries already there are kept."""
    old = (previous or {}).get("accepted") or {}
    acc = {}
    for f in findings:
        if f["severity"] == "info":
            continue
        acc[f["fp"]] = old.get(f["fp"]) or {"rule": f["rule"], "file": f["file"], "accepted_at":
                                            time.strftime("%Y-%m-%d"), "note": note}
    return {"version": 1, "accepted": dict(sorted(acc.items()))}


def gate(findings: list, fail_on: str = "high") -> list:
    """New findings at or above `fail_on` (info never fails)."""
    limit = SEVERITY_ORDER.get(fail_on, 0)
    return [f for f in findings if f.get("status", "new") == "new" and f["severity"] != "info"
            and SEVERITY_ORDER.get(f["severity"], 9) <= limit]
