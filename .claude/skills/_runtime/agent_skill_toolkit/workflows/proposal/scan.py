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
    m = re.match(r"\D*(\d+)(?:\.(\d+))?", str(v or ""))
    return (int(m.group(1)), int(m.group(2) or 0)) if m else None


# ----------------------------------------------------------------------------- deps

def _pom_versions(path: str) -> dict:
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return {}
    ns = {"m": root.tag.split("}")[0].strip("{")} if root.tag.startswith("{") else {}
    q = (lambda t: f"m:{t}") if ns else (lambda t: t)
    out = {}
    props = root.find(q("properties"), ns)
    if props is not None:
        for p in props:
            tag = p.tag.split("}")[-1]
            if tag.endswith(".version") and (p.text or "").strip():
                out[tag[:-len(".version")]] = p.text.strip()
    parent = root.find(q("parent"), ns)
    if parent is not None:
        art = (parent.findtext(q("artifactId"), "", ns) or "").strip()
        ver = (parent.findtext(q("version"), "", ns) or "").strip()
        if "spring-boot" in art and ver:
            out.setdefault("spring-boot", ver)
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
        have = _ver(ver)
        if not rule or not have or have >= rule[0]:
            continue
        gap = (rule[0][0] - have[0]) * 10 + max(0, rule[0][1] - have[1])
        out.append({"key": f"dep:{key}", "kind": "upgrade", "score": 50 + min(40, gap * 5),
                    "title": f"Nâng cấp {key} {ver} (dưới {rule[0][0]}.{rule[0][1]})",
                    "evidence": {"file": src, "property": name, "version": ver, "why": rule[1],
                                 "table": "static 2026-10 [Unverified — verify upstream]"}})
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
    ranked = sorted(changes, key=lambda f: (changes[f] + 2 * fixes.get(f, 0)), reverse=True)
    out = []
    for f in ranked[:top]:
        if changes[f] < min_changes:
            break
        out.append({"key": f"hotspot:{f}", "kind": "refactor", "score": min(90, 20 + changes[f] + 3 * fixes.get(f, 0)),
                    "title": f"Điểm nóng: {f} ({changes[f]} lần sửa, {fixes.get(f, 0)} lần fix trong {days} ngày)",
                    "evidence": {"file": f, "changes": changes[f], "fix_commits": fixes.get(f, 0), "days": days}})
    return out


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


def digest(repo_name: str, findings: list, week: str = "") -> str:
    week = week or week_label()
    lines = [f"# Đề xuất tuần {week} — {repo_name}", "",
             "Chỉ là ĐỀ XUẤT: không việc nào tự chạy. Bấm [Làm] = `proposals.py go <task>`; [Bỏ] = "
             "`proposals.py decide <task> reject --reason ...` (không đề xuất lại).", ""]
    if not findings:
        lines.append("_Không có gì đáng đề xuất tuần này._")
    for i, f in enumerate(findings, 1):
        ev = ", ".join(f"{k}={v}" for k, v in (f.get("evidence") or {}).items())
        lines.append(f"{i}. **{f['title']}** (điểm {f['score']}) — `{f['key']}`")
        lines.append(f"   - bằng chứng: {ev[:300]}")
        if f.get("task_id"):
            lines.append(f"   - task: `{f['task_id']}`")
    return "\n".join(lines) + "\n"
