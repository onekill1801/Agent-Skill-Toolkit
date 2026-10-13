"""Weekly proactive scan (design §12.5, checklist F.9): tool-only scanners, no LLM, no network.
Each finding is a CANDIDATE proposal — it never starts work by itself.

    findings = scan.run(repo)                     # [{"key", "kind", "title", "score", "evidence"}]
    md = scan.digest(repo, findings, week="2026-W42")

Scanners:
    deps       pom.xml / package.json / requirements*.txt versions vs a STATIC end-of-life table
               (offline: the table is a snapshot, verify upstream before acting)
    hotspots   files changed most in the last N days, weighted by fix-like commits (git log)
Messages, names and versions read from the repo are DATA: they are quoted, never interpreted.
"""

import json
import os
import re
import subprocess
import time
import xml.etree.ElementTree as ET

# Static snapshot (2026-10) of open-source support status; [Unverified] — re-check upstream before a
# decision. (min supported major.minor, label)
EOL = {
    "spring-boot": ((3, 4), "Spring Boot < 3.4 is past open-source support"),
    "java": ((17, 0), "Java < 17 is an old LTS line (many libraries dropped it)"),
    "jhipster": ((8, 0), "JHipster < 8 is no longer maintained"),
    "node": ((20, 0), "Node.js < 20 is past end-of-life"),
    "react": ((18, 0), "React < 18 is no longer receiving fixes"),
    "angular": ((17, 0), "Angular < 17 is past long-term support"),
    "django": ((4, 2), "Django < 4.2 is past support"),
}
FIX_RX = re.compile(r"\b(fix|bug|hotfix|sửa|lỗi|revert|patch)\b", re.I)


def _ver(v: str):
    """("exact", (major, minor)) for a concrete version (2.1.10.RELEASE, ^17.0.2, ~1.2, v18),
    ("min", (major, minor)) for a lower bound only (>=8.9.0, >16): the range ALLOWS that version,
    None for anything else (<20, *, x, ||, unresolved ${...}) — never guessed (Codex F.9 #6)."""
    s = str(v or "").strip()
    m = re.fullmatch(r"[v=^~]?\s*(\d+)(?:\.(\d+))?(?:[.\-+][\w.\-+]*)?", s)
    if m:
        return ("exact", (int(m.group(1)), int(m.group(2) or 0)))
    m = re.fullmatch(r">=?\s*v?(\d+)(?:\.(\d+))?(?:\.[\w.\-]*)?", s)
    if m:
        return ("min", (int(m.group(1)), int(m.group(2) or 0)))
    return None


def _resolve(v: str, props: dict) -> str:
    """Maven ${prop} references, a few levels deep; unresolved -> '' (reported as unknown, not guessed)."""
    for _ in range(5):
        if "${" not in (v or ""):
            return v or ""
        v = re.sub(r"\$\{([^}]+)\}", lambda m: props.get(m.group(1), m.group(0)), v)
        if all(f"${{{k}}}" not in v for k in props) and "${" in v:
            return ""
    return "" if "${" in (v or "") else v


# ----------------------------------------------------------------------------- deps

def _pom_versions(path: str) -> dict:
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return {}
    ns = {"m": root.tag.split("}")[0].strip("{")} if root.tag.startswith("{") else {}
    q = (lambda t: f"m:{t}") if ns else (lambda t: t)
    raw = {}
    props = root.find(q("properties"), ns)
    if props is not None:
        for p in props:
            raw[p.tag.split("}")[-1]] = (p.text or "").strip()
    out = {}
    for tag, val in raw.items():
        if tag.endswith(".version") and val:
            out[tag[:-len(".version")]] = _resolve(val, raw)
    # Spring Boot imported as a BOM / declared in dependencyManagement (no parent)
    for dep in root.iter(f"{{{ns['m']}}}dependency" if ns else "dependency"):
        art = (dep.findtext(q("artifactId"), "", ns) or "").strip()
        ver = (dep.findtext(q("version"), "", ns) or "").strip()
        if art == "spring-boot-dependencies" and ver:
            out["spring-boot"] = _resolve(ver, raw) or ver
    parent = root.find(q("parent"), ns)
    if parent is not None:
        art = (parent.findtext(q("artifactId"), "", ns) or "").strip()
        ver = (parent.findtext(q("version"), "", ns) or "").strip()
        if art in ("spring-boot-starter-parent", "spring-boot-dependencies", "spring-boot-parent") and ver:
            out["spring-boot"] = _resolve(ver, raw) or ver   # the parent is what is really built with
    return out


def _package_json_versions(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            pj = json.load(f)
    except (OSError, ValueError):
        return {}
    out = {}
    for sec in ("dependencies", "devDependencies"):
        for name, ver in (pj.get(sec) or {}).items():
            base = name.split("/")[-1] if name.startswith("@angular/") else name
            if base in ("core",) and name.startswith("@angular/"):
                base = "angular"
            out.setdefault(base, str(ver))
    if (pj.get("engines") or {}).get("node"):
        out["node"] = str(pj["engines"]["node"])
    return out


def deps(repo: str) -> list:
    found = {}
    for rel in ("pom.xml",):
        p = os.path.join(repo, rel)
        if os.path.isfile(p):
            found.update({k: (v, rel) for k, v in _pom_versions(p).items()})
    for rel in ("package.json", os.path.join("src", "main", "webapp", "package.json")):
        p = os.path.join(repo, rel)
        if os.path.isfile(p):
            found.update({k: (v, rel.replace("\\", "/")) for k, v in _package_json_versions(p).items()
                          if k not in found})
    out = []
    for name, (ver, src) in sorted(found.items()):
        key = "jhipster" if name.startswith("jhipster") else name
        rule = EOL.get(key)
        if not rule:
            continue
        parsed = _ver(ver)
        if not parsed:
            # a version we cannot read (unresolved ${...}, <20, *, ranges): said so, never guessed
            out.append({"key": f"dep-unknown:{key}", "kind": "unknown", "score": 15,
                        "title": f"Không xác định được phiên bản {key} (khai báo {ver or '—'})",
                        "evidence": {"file": src, "property": name, "declared": ver or "",
                                     "why": f"cannot check against: {rule[1]}"}})
            continue
        if parsed[1] >= rule[0]:
            continue
        kind, have = parsed
        gap = (rule[0][0] - have[0]) * 10 + max(0, rule[0][1] - have[1])
        what = f"{key} {ver}" if kind == "exact" else f"{key} (khai báo {ver} — cho phép bản cũ)"
        out.append({"key": f"dep:{key}", "kind": "upgrade", "score": (50 if kind == "exact" else 30) + min(40, gap * 5),
                    "title": f"Nâng cấp {what} (dưới {rule[0][0]}.{rule[0][1]})",
                    "evidence": {"file": src, "property": name, "version": ver, "version_kind": kind,
                                 "why": rule[1], "table": "static 2026-10 [Unverified — verify upstream]"}})
    return out


# ----------------------------------------------------------------------------- hotspots

def hotspots(repo: str, days: int = 90, top: int = 5, min_changes: int = 5) -> list:
    since = time.strftime("%Y-%m-%d", time.localtime(time.time() - days * 86400))
    try:
        p = subprocess.run(["git", "-C", repo, "log", f"--since={since}", "--no-merges", "--name-only",
                            "--format=@@%s"], capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=120)
    except (OSError, subprocess.SubprocessError):
        return []
    if p.returncode != 0:
        return []
    changes, fixes, subject = {}, {}, ""
    for line in p.stdout.splitlines():
        if line.startswith("@@"):
            subject = line[2:]
            continue
        f = line.strip()
        if not f or not re.search(r"\.(java|kt|py|js|ts|tsx|go|cs|php|sql|xml)$", f):
            continue
        if re.search(r"(^|/)(tests?|src/test)/", f):
            continue
        changes[f] = changes.get(f, 0) + 1
        if FIX_RX.search(subject):
            fixes[f] = fixes.get(f, 0) + 1
    # qualify FIRST, then rank (Codex F.9 #7)
    ranked = sorted((f for f in changes if changes[f] >= min_changes),
                    key=lambda f: (changes[f] + 2 * fixes.get(f, 0)), reverse=True)
    out = []
    for f in ranked[:top]:
        out.append({"key": f"hotspot:{f}", "kind": "refactor", "score": min(90, 20 + changes[f] + 3 * fixes.get(f, 0)),
                    "title": f"Điểm nóng: {f} ({changes[f]} lần sửa, {fixes.get(f, 0)} lần fix trong {days} ngày)",
                    "evidence": {"file": f, "changes": changes[f], "fix_commits": fixes.get(f, 0), "days": days},
                    "aliases": [f"hotspot:{p}" for p in file_history(repo, f)]})
    return out


def file_history(repo: str, path: str, cap: int = 0) -> list:
    """Every path this file had (git log --follow): a rejected hotspot stays rejected after a rename
    (Codex F.9 #4)."""
    try:
        p = subprocess.run(["git", "-C", repo, "log", "--follow", "--name-only", "--format=", "--", path],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    except (OSError, subprocess.SubprocessError):
        return [path]
    names = [x.strip() for x in p.stdout.splitlines() if x.strip()] if p.returncode == 0 else []
    out = list(dict.fromkeys([path, *names]))           # the FULL rename chain (Codex F.9 R2)
    return out[:cap] if cap else out


# ----------------------------------------------------------------------------- run / digest

def run(repo: str, scanners=("deps", "hotspots"), skip_keys=(), days: int = 90) -> list:
    out = []
    for name in scanners:
        try:
            out += {"deps": lambda r: deps(r), "hotspots": lambda r: hotspots(r, days)}[name](repo)
        except Exception as e:  # noqa: BLE001 — one broken scanner never hides the others
            out.append({"key": f"scan-error:{name}", "kind": "error", "score": 0,
                        "title": f"scanner {name} failed", "evidence": {"error": f"{type(e).__name__}: {e}"}})
    skip = set(skip_keys)
    return sorted((f for f in out if f["key"] not in skip), key=lambda f: -f["score"])


def week_label(now: float = None) -> str:
    import datetime
    y, w, _ = datetime.date.fromtimestamp(now or time.time()).isocalendar()
    return f"{y}-W{w:02d}"


def _inert(text, limit: int) -> str:
    """Repo-derived text in the digest: one line, markdown / links neutralised (Codex F.9 #8)."""
    from agent_skill_toolkit.workflows.intake import lanes
    return lanes.literal(" ".join(str(text).split()))[:limit]


def digest(repo_name: str, findings: list, week: str = "") -> str:
    week = week or week_label()
    lines = [f"# Đề xuất tuần {week} — {_inert(repo_name, 80)}", "",
             "Chỉ là ĐỀ XUẤT: không việc nào tự chạy. Bấm [Làm] = `proposals.py go <task>`; [Bỏ] = "
             "`proposals.py decide <task> reject --reason ...` (không đề xuất lại).", ""]
    if not findings:
        lines.append("_Không có gì đáng đề xuất tuần này._")
    for i, f in enumerate(findings, 1):
        ev = ", ".join(f"{k}={v}" for k, v in (f.get("evidence") or {}).items())
        lines.append(f"{i}. **{_inert(f['title'], 200)}** (điểm {int(f['score'])}) — {_inert(f['key'], 200)}")
        lines.append(f"   - bằng chứng: {_inert(ev, 300)}")
        if f.get("task_id"):
            lines.append(f"   - task: `{f['task_id']}`")
    return "\n".join(lines) + "\n"
