#!/usr/bin/env python3
"""Codebase grounding — gather the real code an implementer must respect.

Before the Implement stage edits anything, this collects the actual target files
(from the plan spec's <target_files>) plus their neighbours and a stack hint, and
writes a grounding artifact the implementing agent reads. This closes the gap
where the pipeline planned rigorously but then coded blind to repo conventions.

Mechanical-first: gathering needs no tokens. Pass --backend to additionally have an
agent distil the conventions into a short note appended to the artifact.

The result JSON carries `verdict` (pass|fail) so the orchestrator can:
    python grounding.py run --run <RID> --root <clone_dir> > g.json
    python run_log.py record-gate <RID> grounding --verdict pass --json g.json

`fail` means no target file could be located → the plan can't be grounded against
the repo (wrong paths, wrong checkout), which SHOULD block implement in auto mode.

Stdlib only.
"""

import argparse
import json
import os
import sys

try:  # Windows consoles default to cp1252; repo snippets are often non-ASCII.
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

_HERE = os.path.dirname(os.path.abspath(__file__))
_SKILLS = os.path.dirname(os.path.dirname(_HERE))
sys.path.insert(0, os.path.join(_SKILLS, "fork-terminal", "tools"))
sys.path.insert(0, _HERE)
import agent_parser  # noqa: E402
import agent_runner  # noqa: E402

HEAD_LINES = 80
MAX_NEIGHBOURS = 12
STACK_MARKERS = {
    "pom.xml": "maven/java", "build.gradle": "gradle/java",
    "build.gradle.kts": "gradle/kotlin", "package.json": "node",
    "pyproject.toml": "python", "requirements.txt": "python", "go.mod": "go",
}

# --- scout (pre-grounding) settings ---------------------------------------------
# Directories never worth scanning (build output, deps, VCS metadata).
SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "target", "build", "dist", "out",
    "__pycache__", ".venv", "venv", ".idea", ".gradle", ".mvn", "bin", "obj",
    ".next", ".nuxt", "coverage", "vendor", ".pytest_cache",
}
# Text/code extensions worth grepping.
CODE_EXTS = {
    ".java", ".kt", ".kts", ".py", ".js", ".ts", ".tsx", ".jsx", ".vue", ".go",
    ".rb", ".php", ".cs", ".sql", ".xml", ".yml", ".yaml", ".json", ".properties",
    ".html", ".md", ".gradle", ".c", ".cpp", ".h", ".scala",
}
MAX_SCAN_FILES = 20000        # hard cap so a huge monorepo cannot hang the pipeline
MAX_FILE_BYTES = 1_000_000    # skip files larger than ~1MB
MAX_SCOUT_CANDIDATES = 15     # top-N files reported
MAX_SNIPPETS = 2              # sample matching lines per candidate
_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "when", "should", "must",
    "will", "have", "into", "your", "task", "issue", "bug", "fix", "add", "make",
    "cần", "một", "các", "được", "khi", "cho", "này", "thì", "phải", "trong", "của",
    "làm", "lỗi", "sửa", "thêm",
}


def _repo_root() -> str:
    search = _HERE
    for _ in range(6):
        if os.path.isdir(os.path.join(search, ".git")) or \
           os.path.isfile(os.path.join(search, "CLAUDE.md")):
            return search
        search = os.path.dirname(search)
    return _HERE


def _plan_path(run_id, explicit):
    if explicit:
        return explicit
    safe = "".join(c for c in str(run_id) if c.isalnum() or c in "-_.") or "debate"
    return os.path.join(_repo_root(), "temp", "runs", f"{safe}_plan.xml")


def _artifact_path(run_id):
    safe = "".join(c for c in str(run_id) if c.isalnum() or c in "-_.") or "run"
    runs = os.path.join(_repo_root(), "temp", "runs")
    os.makedirs(runs, exist_ok=True)
    return os.path.join(runs, f"{safe}_grounding.md")


def _stack_hint(root):
    hits = [label for marker, label in STACK_MARKERS.items()
            if os.path.isfile(os.path.join(root, marker))]
    return ", ".join(sorted(set(hits))) or "unknown"


def _gather_file(root, rel):
    """Return a record for one target file: existence, head excerpt, neighbours."""
    full = os.path.join(root, rel)
    rec = {"path": rel, "exists": os.path.isfile(full)}
    if not rec["exists"]:
        return rec
    with open(full, encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    rec["line_count"] = len(lines)
    rec["head"] = "".join(lines[:HEAD_LINES]).rstrip()
    d = os.path.dirname(full)
    try:
        siblings = sorted(n for n in os.listdir(d)
                          if os.path.isfile(os.path.join(d, n)) and n != os.path.basename(full))
    except OSError:
        siblings = []
    rec["neighbours"] = siblings[:MAX_NEIGHBOURS]
    return rec


def _render(run_id, root, stack, records):
    out = [f"# Grounding — run {run_id}", "",
           f"- repo root: `{root}`", f"- stack hint: **{stack}**",
           f"- target files: {len(records)} "
           f"({sum(1 for r in records if r['exists'])} found)", ""]
    for r in records:
        if not r["exists"]:
            out.append(f"## `{r['path']}` — **MISSING** (plan references a path not in repo)")
            out.append("")
            continue
        out.append(f"## `{r['path']}` — {r['line_count']} lines")
        if r["neighbours"]:
            out.append(f"_neighbours_: {', '.join(r['neighbours'])}")
        out.append("")
        out.append("```")
        out.append(r["head"])
        out.append("```")
        out.append("")
    return "\n".join(out)


_AGENT_SYSTEM = (
    "You are grounding an implementer. From the gathered file excerpts, write a SHORT "
    "note (<=10 lines, plain text) on the conventions to follow: naming, layering, error "
    "handling, test placement. No Markdown headers, no fluff, only what an editor must respect."
)


def cmd_run(args) -> dict:
    plan = _plan_path(args.run, args.plan)
    if not os.path.isfile(plan):
        return {"error": True, "message": f"plan spec not found: {plan}"}
    with open(plan, encoding="utf-8") as f:
        spec = f.read()
    targets = agent_parser.extract_list_items(spec, "target_files", "file")
    root = os.path.abspath(args.root)
    if not os.path.isdir(root):
        return {"error": True, "message": f"--root is not a directory: {root}"}

    stack = _stack_hint(root)
    records = [_gather_file(root, t.strip()) for t in targets if t.strip()]
    found = [r["path"] for r in records if r["exists"]]
    missing = [r["path"] for r in records if not r["exists"]]

    body = _render(args.run, root, stack, records)
    agent_note = None
    if args.backend and found:
        try:
            ctx = body[:12000]
            agent_note = agent_runner.run_turn(
                ctx, system=_AGENT_SYSTEM, backend=args.backend, model=args.model,
                dry_run_text=args.dry_run_text)
            body += "\n## Conventions (agent note)\n\n" + agent_note + "\n"
        except agent_runner.AgentRunError as e:
            body += f"\n## Conventions (agent note)\n\n_unavailable: {e}_\n"

    artifact = _artifact_path(args.run)
    with open(artifact, "w", encoding="utf-8") as f:
        f.write(body)

    # verdict: we could ground iff the plan listed files AND at least one resolves.
    # No targets at all, or none found, means the implementer would be coding blind.
    verdict = "pass" if found else "fail"
    summary = (f"grounded {len(found)}/{len(records)} target file(s); stack={stack}"
               if records else "plan listed no <target_files>")
    return {
        "ok": True,
        "run_id": args.run,
        "plan": plan,
        "artifact": artifact,
        "stack": stack,
        "target_count": len(records),
        "found": found,
        "missing": missing,
        "verdict": verdict,
        "summary": summary,
        "kind": "grounding",
        "agent_note": bool(agent_note),
    }


# --- scout: reverse grounding BEFORE the plan exists ----------------------------
# The plain `run` grounding needs the plan's <target_files>; but for a vague task the
# planner is coding blind precisely because it doesn't know which files matter. Scout
# runs at Intake: given the task's keywords, it greps the repo and surfaces the most
# relevant files so the plan can anchor to real code instead of guessing.

def _id_like(tok):
    """Opaque IDs (base62 task/comment ids) are useless anchors and match noise.

    A token >=16 chars mixing upper+lower+digits is almost certainly a generated id,
    not a code symbol (real class/method names that long rarely embed digits).
    """
    return (len(tok) >= 16 and any(c.isdigit() for c in tok)
            and any(c.islower() for c in tok) and any(c.isupper() for c in tok))


def _keywords(explicit, desc):
    """Build a keyword set from --keywords and/or free-text description.

    Keeps identifier-like tokens (camelCase / snake_case / dotted) and significant
    words (len>=4, not a stopword). Drops opaque ids. Lower-cased match, de-duped, capped.
    """
    terms = []
    if explicit:
        terms += [t.strip() for t in explicit.split(",") if t.strip()]
    if desc:
        import re
        # Strip markdown chrome so a context-pack file doesn't inject heading/label noise.
        clean_lines = [ln for ln in desc.splitlines() if not ln.lstrip().startswith("#")]
        desc = " ".join(clean_lines).replace("*", " ").replace("`", " ")
        # identifier-ish tokens first (they are the strongest anchors)
        for tok in re.findall(r"[A-Za-z_][A-Za-z0-9_]{3,}(?:\.[A-Za-z0-9_]+)*", desc):
            terms.append(tok)
        # quoted strings often name endpoints/fields/tables
        for q in re.findall(r"[\"'`]([^\"'`]{3,40})[\"'`]", desc):
            terms.append(q.strip())
    seen, out = set(), []
    for t in terms:
        low = t.lower()
        if len(low) < 4 or low in _STOPWORDS or _id_like(t):
            continue
        if low not in seen:
            seen.add(low)
            out.append(t)
    return out[:20]


def _iter_files(root):
    scanned = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            ext = os.path.splitext(fn)[1].lower()
            if ext not in CODE_EXTS:
                continue
            full = os.path.join(dirpath, fn)
            try:
                if os.path.getsize(full) > MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            yield full
            scanned += 1
            if scanned >= MAX_SCAN_FILES:
                return


def _score_file(full, kw_low):
    """Return (score, hit_terms, snippets) for one file against lowercased keywords."""
    try:
        with open(full, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return 0, [], []
    low = text.lower()
    hits, score = [], 0
    for kw in kw_low:
        c = low.count(kw)
        if c:
            hits.append(kw)
            score += c
    if not hits:
        return 0, [], []
    # filename match is a strong signal — weight it up
    base = os.path.basename(full).lower()
    for kw in kw_low:
        if kw in base:
            score += 10
    snippets = []
    for i, line in enumerate(text.splitlines(), 1):
        ll = line.lower()
        if any(kw in ll for kw in hits):
            snippets.append(f"{i}: {line.strip()[:160]}")
            if len(snippets) >= MAX_SNIPPETS:
                break
    return score, hits, snippets


def _render_scout(run_id, root, stack, keywords, candidates):
    out = [f"# Scout (pre-grounding) — run {run_id}", "",
           f"- repo root: `{root}`", f"- stack hint: **{stack}**",
           f"- keywords: {', '.join(keywords) or '(none)'}",
           f"- candidates: {len(candidates)}", ""]
    if not candidates:
        out.append("_No files matched the task keywords — the plan must locate targets manually._")
        return "\n".join(out) + "\n"
    for c in candidates:
        rel = os.path.relpath(c["path"], root)
        out.append(f"## `{rel}` — score {c['score']} (hits: {', '.join(c['hits'])})")
        if c["snippets"]:
            out.append("```")
            out += c["snippets"]
            out.append("```")
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def _scout_artifact_path(run_id):
    safe = "".join(c for c in str(run_id) if c.isalnum() or c in "-_.") or "run"
    runs = os.path.join(_repo_root(), "temp", "runs")
    os.makedirs(runs, exist_ok=True)
    return os.path.join(runs, f"{safe}_scout.md")


def cmd_scout(args) -> dict:
    root = os.path.abspath(args.root)
    if not os.path.isdir(root):
        return {"error": True, "message": f"--root is not a directory: {root}"}
    desc = args.desc
    if args.desc_file:
        try:
            with open(args.desc_file, encoding="utf-8") as f:
                desc = f.read()
        except OSError as e:
            return {"error": True, "message": f"cannot read --desc-file: {e}"}
    keywords = _keywords(args.keywords, desc)
    if not keywords:
        return {"error": True, "message": "no usable keywords (pass --keywords or --desc/--desc-file)"}

    kw_low = [k.lower() for k in keywords]
    scored = []
    for full in _iter_files(root):
        score, hits, snippets = _score_file(full, kw_low)
        if score:
            scored.append({"path": full, "score": score, "hits": hits, "snippets": snippets})
    scored.sort(key=lambda c: c["score"], reverse=True)
    candidates = scored[:MAX_SCOUT_CANDIDATES]

    stack = _stack_hint(root)
    body = _render_scout(args.run, root, stack, keywords, candidates)
    artifact = _scout_artifact_path(args.run)
    with open(artifact, "w", encoding="utf-8") as f:
        f.write(body)

    verdict = "pass" if candidates else "fail"
    return {
        "ok": True,
        "run_id": args.run,
        "artifact": artifact,
        "root": root,
        "stack": stack,
        "keywords": keywords,
        "candidate_count": len(candidates),
        "candidates": [{"path": os.path.relpath(c["path"], root),
                        "score": c["score"], "hits": c["hits"]} for c in candidates],
        "verdict": verdict,
        "summary": (f"{len(candidates)} candidate file(s) from {len(keywords)} keyword(s)"
                    if candidates else "no files matched task keywords"),
        "kind": "scout",
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Gather codebase grounding for the Implement stage.")
    sub = ap.add_subparsers(dest="action", required=True)
    p = sub.add_parser("run")
    p.add_argument("--run", required=True, help="run_id (locates temp/runs/<id>_plan.xml + artifact)")
    p.add_argument("--root", required=True, help="clone_dir of the target repo to read files from")
    p.add_argument("--plan", default=None, help="explicit plan xml path (overrides --run lookup)")
    p.add_argument("--backend", default=None, help="optional agent for a conventions note")
    p.add_argument("--model", default=None)
    p.add_argument("--dry-run-text", default=None)

    s = sub.add_parser("scout", help="pre-grounding: grep repo by task keywords BEFORE the plan")
    s.add_argument("--run", required=True, help="run_id (names temp/runs/<id>_scout.md)")
    s.add_argument("--root", required=True, help="clone_dir of the target repo to grep")
    s.add_argument("--keywords", default=None, help="comma-separated keywords/identifiers")
    s.add_argument("--desc", default=None, help="task text to auto-extract keywords from")
    s.add_argument("--desc-file", default=None, help="read task text from a file (e.g. context pack)")

    args = ap.parse_args()
    try:
        out = cmd_scout(args) if args.action == "scout" else cmd_run(args)
    except (OSError, ValueError) as e:
        out = {"error": True, "message": str(e)}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 1 if isinstance(out, dict) and out.get("error") else 0


if __name__ == "__main__":
    sys.exit(main())
