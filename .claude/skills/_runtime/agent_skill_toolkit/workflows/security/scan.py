"""Offline security scan of a product repo (checklist F.23) — rules.py + spring.py + a baseline.

    scan(repo, history=False) -> {"findings": [...], "endpoints": [...], "stats": {...}, "gaps": [...]}
    apply_baseline(findings, baseline) -> marks each finding new / known
    baseline_doc(findings) -> the JSON document to store as the accepted state

A finding: {rule, severity, category, title, file, line, text, fix, fp}. `text` of EVERY finding goes through
redact() (a credential never leaves the process, whichever rule fired — Codex security R1 #1).
`fp` = rule + file + normalized line + the occurrence index of that same line in the file: a moved line keeps
its fingerprint, a second identical statement gets a new one (R1 #7). `gaps` = what could NOT be analysed
(unreadable / oversized file, git failure): a scan with gaps is never "clean" (R1 #2, #3).
An inline `security-audit: ignore [RULE-ID] <reason>` on the line (or the line above) suppresses it there.

Never sends anything anywhere; reads files and (with history=True) `git log -p`.
"""

import fnmatch
import hashlib
import json
import os
import re
import subprocess
import time

from . import rules as R
from . import spring

SKIP_DIRS = {".git", "node_modules", "target", "build", "dist", "out", ".gradle", ".idea", ".mvn", ".vscode",
             "coverage", "vendor", ".angular", "__pycache__", ".next", ".nuxt", "bin", "obj", ".codegraph",
             ".claude", ".agents", ".agent"}
MAX_FILE = 20 * 1024 * 1024        # larger = a gap (reported), not silently skipped
CHUNK, OVERLAP = 4000, 400         # very long lines are scanned in overlapping chunks (R1 #3)
SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2, "info": 3}
_IGNORE = re.compile(r"security-audit:\s*ignore\b\s*([A-Z]+-\d{3})?", re.I)
_TEST_PATH = re.compile(r"(?:^|/)(?:src/test|src/it|test|tests|__tests__|spec|fixtures?|testdata|e2e)(?:/|$)|"
                        r"(?:Test|Tests|IT|Spec)\.(?:java|kt|ts|js)$|\.(?:spec|test)\.[jt]sx?$")
_LOCKFILES = ("package-lock.json", "npm-shrinkwrap.json", "composer.lock", "yarn.lock", "pnpm-lock.yaml")
_ENV_FILE = re.compile(r"(?:^|/)\.env(?:\.[\w.-]+)?$|\.env$")


def is_test(rel: str) -> bool:
    return bool(_TEST_PATH.search(rel))


def _match(rel: str, globs) -> bool:
    name = rel.rsplit("/", 1)[-1]
    return any(g == "*" or fnmatch.fnmatch(name, g) for g in globs)


# ----------------------------------------------------------------------------- redaction

# `<secret-ish name> = "value"` / `name: value` / `name=value` anywhere on a line
_ASSIGNED = re.compile(r"(?i)(\w*(?:password|passwd|pwd|secret|token|api[-_]?key|access[-_]?key|private[-_]?key|"
                       r"credential)s?\w*[\"']?\s*(?:[:=]|=>)\s*)(\"[^\"]*\"|'[^']*'|[^\s,;)}\]]+)")


def redact(text: str, spans=None) -> str:
    """The line as it may be SHOWN: the matched secret spans keep 3 chars, every known token shape keeps 3,
    every value assigned to a secret-named key/variable keeps 3. Applied to every finding."""
    out = text
    if spans:
        parts, pos = [], 0
        for a, b in sorted(spans):
            if a < pos:
                continue
            parts += [out[pos:a], out[a:a + 3], "…"]
            pos = b
        parts.append(out[pos:])
        out = "".join(parts)
    for rid in ("SEC-001", "SEC-002"):
        out = R.BY_ID[rid].pattern.sub(lambda m: m.group(0)[:3] + "…", out)

    def value(m):
        v = m.group(2)
        q = v[0] if v[:1] in "\"'" else ""
        inner = v[1:-1] if q else v
        if len(inner) <= 3 or inner.startswith("${") and inner.endswith("}") and ":" not in inner:
            return m.group(0)
        return m.group(1) + q + inner[:3] + "…" + q
    return _ASSIGNED.sub(value, out)


def fingerprint(rule: str, rel: str, text: str, occurrence: int = 0) -> str:
    norm = re.sub(r"\s+", " ", text.strip())
    return hashlib.sha256(f"{rule}|{rel}|{norm}|{occurrence}".encode("utf-8")).hexdigest()[:16]


def _finding(rule, rel, line_no, text, detail="", spans=None) -> dict:
    get = (lambda k: getattr(rule, k)) if hasattr(rule, "id") else (lambda k: rule[k])
    stripped = text.lstrip()
    off = len(text) - len(stripped)
    shown = redact(stripped.rstrip(), [(x - off, y - off) for x, y in spans] if spans else None)
    return {"rule": get("id"), "severity": get("severity"), "category": get("category"), "title": get("title"),
            "fix": get("fix"), "file": rel, "line": line_no, "text": shown[:200], "detail": detail,
            "_raw": text}


def _assign_fingerprints(findings: list) -> None:
    """fp per finding, with the occurrence index of identical (rule, file, line text) — then drop _raw."""
    seen = {}
    for f in sorted(findings, key=lambda f: (f["rule"], f["file"], f["line"])):
        norm = re.sub(r"\s+", " ", f.pop("_raw", f["text"]).strip())
        k = (f["rule"], f["file"], norm)
        n = seen.get(k, 0)
        seen[k] = n + 1
        f["fp"] = fingerprint(f["rule"], f["file"], norm, n)


# ----------------------------------------------------------------------------- files

def iter_files(repo: str, gaps: list):
    """(relpath with /, absolute path) of every file worth scanning; problems go to `gaps`."""
    def err(e):
        gaps.append({"path": os.path.relpath(getattr(e, "filename", "") or repo, repo).replace(os.sep, "/"),
                     "reason": f"không liệt kê được thư mục: {type(e).__name__}"})
    for d, dirs, files in os.walk(repo, onerror=err):
        dirs[:] = [x for x in dirs if x not in SKIP_DIRS and not x.startswith(".git")]
        for f in files:
            p = os.path.join(d, f)
            rel = os.path.relpath(p, repo).replace(os.sep, "/")
            try:
                if os.path.islink(p):
                    continue
                size = os.path.getsize(p)
            except OSError as e:
                gaps.append({"path": rel, "reason": f"không đọc được: {type(e).__name__}"})
                continue
            if size > MAX_FILE:
                gaps.append({"path": rel, "reason": f"file quá lớn ({size // 1048576} MB) — chưa quét"})
                continue
            yield rel, p


def read_text(path: str, rel: str, gaps: list):
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError as e:
        gaps.append({"path": rel, "reason": f"không đọc được: {type(e).__name__}"})
        return None
    if b"\0" in raw[:8192]:
        return None                                       # binary: not a text source
    return raw.decode("utf-8", errors="replace")


def _ignored(lines: list, i: int, rule_id: str) -> bool:
    for j in (i, i - 1):
        if 0 <= j < len(lines):
            m = _IGNORE.search(lines[j])
            if m and (not m.group(1) or m.group(1).upper() == rule_id):
                return True
    return False


def _search_spans(pattern, line: str) -> list:
    """Every match span of `pattern` on `line`, long lines scanned in overlapping chunks."""
    if len(line) <= CHUNK:
        return [m.span() for m in pattern.finditer(line)]
    spans = set()
    for start in range(0, len(line), CHUNK - OVERLAP):
        for m in pattern.finditer(line[start:start + CHUNK]):
            spans.add((start + m.start(), start + m.end()))
    return sorted(spans)


# ----------------------------------------------------------------------------- config flattening

_YAML_KEY = re.compile(r"^(\s*)(-\s+)?([\w.\-\"'\[\]/]+)\s*:(?:\s+|$)(.*)$")
_YAML_ITEM = re.compile(r"^(\s*)-\s+(.*)$")


def flatten_config(rel: str, text: str) -> list:
    """[(line_no, dotted.key, value)] of a YAML (indentation-based, the common subset), .properties or
    .env file. List items keep their parent key (R1 #8); a block scalar (| / >) is one value of its key.
    Multi-document YAML (---) restarts the path."""
    out = []
    if rel.endswith(".properties") or _ENV_FILE.search(rel):
        for n, line in enumerate(text.splitlines(), 1):
            s = line.strip()
            if not s or s[0] in "#!":
                continue
            s = re.sub(r"^export\s+", "", s)
            m = re.match(r"([^=:\s]+)\s*[=:]\s*(.*)$", s)
            if m:
                out.append((n, m.group(1), m.group(2).strip().strip("\"'")))
        return out
    stack, block = [], None                               # stack [(indent, key)]; block [indent, line, path, parts]
    for n, line in enumerate(text.splitlines(), 1):
        if block is not None:
            ind = len(line) - len(line.lstrip())
            if line.strip() and ind > block[0]:
                block[3].append(line.strip())
                continue
            if not line.strip():
                continue
            out.append((block[1], block[2], " ".join(block[3])))
            block = None
        if line.startswith("---") or line.strip() == "...":
            stack = []
            continue
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = _YAML_KEY.match(line)
        if not m:
            it = _YAML_ITEM.match(line)                    # "- value": an item of the enclosing key
            if it:
                indent = len(it.group(1))
                while stack and stack[-1][0] >= indent + 1 and stack[-1][0] > indent:
                    stack.pop()
                item = re.sub(r"\s+#.*$", "", it.group(2)).strip().strip("\"'")
                if stack and item:
                    out.append((n, ".".join(k for _, k in stack), item))
            continue
        indent = len(m.group(1)) + (len(m.group(2)) if m.group(2) else 0)
        key = m.group(3).strip("\"'")
        value = re.sub(r"\s+#.*$", "", m.group(4)).strip()
        while stack and stack[-1][0] >= indent:
            stack.pop()
        path = ".".join([k for _, k in stack] + [key])
        if value[:1] in ("|", ">"):
            block = [indent, n, path, []]
        elif value:
            if value.startswith("[") and value.endswith("]"):      # flow list: one entry per item
                for item in value[1:-1].split(","):
                    out.append((n, path, item.strip().strip("\"'")))
            else:
                out.append((n, path, value.strip("\"'")))
        else:
            stack.append((indent, key))
    if block is not None:
        out.append((block[1], block[2], " ".join(block[3])))
    return out


_JSON_PAIR = re.compile(r"\"([\w.\-]+)\"\s*:\s*\"([^\"]*)\"")


def _json_pairs(text: str) -> list:
    """[(line_no, key, value)] of "key": "string" pairs of a JSON file (key = last segment)."""
    return [(n, m.group(1), m.group(2)) for n, line in enumerate(text.splitlines(), 1)
            for m in _JSON_PAIR.finditer(line[:CHUNK * 4])]


def _is_prod_config(rel: str) -> bool:
    name = rel.rsplit("/", 1)[-1].lower()
    if is_test(rel) or re.search(r"-(?:dev|local|test|e2e|it|ci)\b", name):
        return False
    return bool(re.match(r"(?:application|bootstrap)(?:-prod[\w-]*)?\.(?:ya?ml|properties)$", name))   # R1 #9


def secret_value(value: str):
    """The committed secret inside a config value, or None: `${VAR}` is a reference, but the DEFAULT of
    `${VAR:default}` is a committed value (R1 #5)."""
    v = value.strip().strip("\"'")
    m = re.fullmatch(r"\$\{[^:}]*:(.*)\}", v)
    if m:
        v = m.group(1).strip()
    if not v or len(v) < 4 or R.NOT_A_SECRET.search(v):
        return None
    return v


def _key_findings(rel: str, text: str) -> list:
    out = []
    lines = text.splitlines()
    pairs = _json_pairs(text) if rel.endswith(".json") else flatten_config(rel, text)
    for n, key, value in pairs:
        for r in R.KEY_RULES:
            if r.prod_only and not _is_prod_config(rel):
                continue
            if rel.endswith(".json") and r.id != "SEC-003":
                continue
            if not r.key.search(key):
                continue
            if r.id == "SEC-003":
                if is_test(rel) or secret_value(value) is None:
                    continue
            elif not r.value.search(value):
                continue
            if _ignored(lines, n - 1, r.id):
                continue
            shown = f"{key}: {value}"
            out.append(_finding(r, rel, n, shown, spans=[(len(key) + 2, len(shown))] if r.secret else None))
    return out


def _unless_near(rule, code: list, i: int) -> bool:
    """Hardening written next to THIS call (same or next 15 lines of code, comments stripped) — not anywhere
    in the file (R1 #11)."""
    return bool(rule.unless and rule.unless.search("\n".join(code[i:i + 16])))


# ----------------------------------------------------------------------------- the scan

def scan(repo: str, history: bool = False, max_commits: int = 300) -> dict:
    t0 = time.time()
    findings, java, files_n, gaps = [], {}, 0, []
    for rel, path in iter_files(repo, gaps):
        text = read_text(path, rel, gaps)
        if text is None:
            continue
        files_n += 1
        test = is_test(rel)
        if rel.endswith((".java", ".kt")) and not test:
            java[rel] = text
        lines = text.splitlines()
        if (rel.endswith((".yml", ".yaml", ".properties", ".json")) or _ENV_FILE.search(rel)) \
                and not rel.endswith(_LOCKFILES):                                         # R1 #4
            findings += _key_findings(rel, text)
        code = None
        for r in R.RULES:
            if (r.main_only and test) or not _match(rel, r.files):
                continue
            if code is None:                              # comments do not run: code rules see code only
                code = spring.strip_comments(text).splitlines() if rel.endswith((".java", ".kt", ".js", ".ts",
                                                                                 ".tsx", ".jsx")) else lines
            src = lines if r.category in ("secret", "leftover") else code
            for i, line in enumerate(src):
                spans = _search_spans(r.pattern, line)
                if not spans:
                    continue
                if _ignored(lines, i, r.id) or _unless_near(r, code, i):
                    continue
                raw = lines[i] if i < len(lines) else line
                f = _finding(r, rel, i + 1, raw, spans=spans if r.secret else None)
                if r.secret and test:
                    f["severity"], f["detail"] = "low", "trong code test"
                findings.append(f)
    eps, ep_findings = spring.classify(spring.endpoints(java), spring.url_rules(java))
    by_id = {r["id"]: r for r in R.ENDPOINT_RULES}
    for e in ep_findings:
        findings.append(_finding(by_id[e["rule"]], e["file"], e["line"], e["text"], detail=e["detail"]))
    hist = history_secrets(repo, max_commits) if history else {"scanned": 0, "findings": []}
    if hist.get("error"):
        gaps.append({"path": "(lịch sử git)", "reason": hist["error"]})          # R1 #2
    findings += hist["findings"]
    _assign_fingerprints(findings)
    findings.sort(key=lambda f: (SEVERITY_ORDER.get(f["severity"], 9), f["rule"], f["file"], f["line"]))
    return {"repo": os.path.abspath(repo), "findings": findings, "endpoints": eps, "gaps": gaps,
            "stats": {"files": files_n, "java_controllers": len({e["file"] for e in eps}), "endpoints": len(eps),
                      "url_rules": len(spring.url_rules(java)), "history_commits": hist["scanned"],
                      "gaps": len(gaps), "seconds": round(time.time() - t0, 1)}}


def history_secrets(repo: str, max_commits: int = 300) -> dict:
    """Credentials in lines ADDED by the last `max_commits` commits (a secret deleted later is still in
    history). Streams `git log -p`. Any git failure is returned as `error` (the scan is then incomplete)."""
    line_rules = [R.BY_ID[i] for i in R.HISTORY_RULES]
    key_rule = R.KEY_BY_ID["SEC-003"]
    cmd = ["git", "-C", repo, "log", "--all", "-p", "--no-color", "-U0", f"-n{int(max_commits)}",
           "--pretty=format:@@COMMIT %H %ad", "--date=short"]
    out, scanned, commit, rel = [], 0, "", ""
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                             encoding="utf-8", errors="replace")
    except OSError as e:
        return {"scanned": 0, "findings": [], "error": f"không chạy được git: {type(e).__name__}"}
    try:
        for line in p.stdout:
            if line.startswith("@@COMMIT "):
                parts = line.split()
                commit, scanned = parts[1][:10] + " " + parts[-1], scanned + 1
                continue
            if line.startswith("+++ "):
                rel = line[6:].strip() if line.startswith("+++ b/") else ""
                continue
            if not line.startswith("+") or not rel:
                continue
            added = line[1:].rstrip("\n")
            for r in line_rules:
                spans = _search_spans(r.pattern, added)
                if spans:
                    f = _finding(r, rel, 0, added, detail=f"trong lịch sử git: commit {commit}", spans=spans)
                    out.append(_hist(f, r))
            if rel.endswith((".yml", ".yaml", ".properties")) or _ENV_FILE.search(rel):
                m = re.match(r"^\s*(?:export\s+)?([\w.\-]+)\s*[:=]\s*(.+)$", added)
                if m and key_rule.key.search(m.group(1)) and secret_value(m.group(2)):
                    shown = f"{m.group(1)}: {m.group(2)}"
                    f = _finding(key_rule, rel, 0, shown, detail=f"trong lịch sử git: commit {commit}",
                                 spans=[(len(m.group(1)) + 2, len(shown))])
                    out.append(_hist(f, key_rule))
    finally:
        p.stdout.close()
        err = p.stderr.read() if p.stderr else ""
        try:
            rc = p.wait(timeout=30)
        except subprocess.TimeoutExpired:
            p.kill()
            rc = -1
        if p.stderr:
            p.stderr.close()
    if rc != 0:
        return {"scanned": scanned, "findings": out,
                "error": f"git log thất bại (mã {rc}): {redact(err.strip().splitlines()[-1] if err.strip() else '')}"[:200]}
    # one finding per (rule, file, secret line) whatever the number of commits that re-added it
    uniq = {}
    for f in out:
        uniq.setdefault((f["rule"], f["file"], f["_raw"]), f)
    return {"scanned": scanned, "findings": list(uniq.values())}


def _hist(f: dict, r) -> dict:
    f.update(rule="HIST-" + r.id, title="(lịch sử git) " + r.title,
             fix="Đổi/thu hồi secret (đã lộ trong lịch sử); xoá khỏi lịch sử nếu repo được chia sẻ "
                 "(git filter-repo) — xoá ở commit mới KHÔNG đủ.")
    return f


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
