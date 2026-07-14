#!/usr/bin/env python3
"""Intake context pack — enrich a thin/vague task with the context that already exists.

Root cause of low auto-dev accuracy on vague tasks: the pipeline planned from the
task's `title` + `description` alone. But on aTask (and Azure DevOps) the real
requirements usually live *elsewhere* — in the **comments**, the **checklist**, the
**subtasks**, the **parent task**, and (Azure) the **acceptance criteria / root cause
/ solution** fields. This tool gathers all of it into one Markdown "context pack" that
becomes the `--desc` for `clarify.py` and `debate_engine.py`, and emits `ac_seeds`
(checklist items + explicit AC) ready for `run_log.py ac-add`.

It shells out to the existing skill tools (atask `tasks.py` / `checklists.py`,
dev-automation `azure_devops.py`) so it inherits their config/auth/UTF-8 handling and
stays stdlib-only itself. Every sub-call is defensive: a failed fetch is noted and the
pack is still produced from whatever succeeded (never hard-fails the pipeline).

Usage:
    python context_pack.py build --source atask --task 12345 --type feature
    python context_pack.py build --source azure --task 987 --out ../../../temp/runs/az-987_context.md
    python context_pack.py build --source atask --task 12345 --no-comments   # skip a slow fetch

Output: one JSON object on stdout; the Markdown pack is written to --out
(default temp/runs/<source>-<task>_context.md).
"""

import argparse
import json
import os
import subprocess
import sys

try:  # Windows consoles default to cp1252; task text is often Vietnamese.
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

_HERE = os.path.dirname(os.path.abspath(__file__))
_SKILLS = os.path.dirname(os.path.dirname(_HERE))
_ATASK_TOOLS = os.path.join(_SKILLS, "atask-automation", "tools")
_DEV_TOOLS = os.path.join(_SKILLS, "dev-automation", "tools")

sys.path.insert(0, _HERE)
import grounding  # noqa: E402  (reuse its keyword extractor so scout gets clean anchors)

# Explicit-acceptance markers: a checklist item / comment line that reads like a
# done-definition is a strong acceptance-criteria seed (EN + VI).
AC_HINT = (
    "must", "should", "expect", "return", "verify", "given", "when", "then",
    "phải", "kết quả", "mong đợi", "nghiệm thu", "đảm bảo", "trả về", "kiểm tra",
)


def _repo_root():
    search = _HERE
    for _ in range(6):
        if os.path.isdir(os.path.join(search, ".git")) or \
           os.path.isfile(os.path.join(search, "CLAUDE.md")):
            return search
        search = os.path.dirname(search)
    return _HERE


def _run_tool(script_dir, script, cli_args):
    """Run a skill CLI, return (parsed_json | None, error_str | None). Never raises."""
    path = os.path.join(script_dir, script)
    if not os.path.isfile(path):
        return None, f"tool not found: {path}"
    try:
        proc = subprocess.run(
            [sys.executable, path, *cli_args],
            cwd=script_dir, capture_output=True, text=True, encoding="utf-8", timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return None, f"exec failed: {e}"
    out = (proc.stdout or "").strip()
    if not out:
        return None, (proc.stderr or "").strip() or f"empty output (exit {proc.returncode})"
    try:
        return json.loads(out), None
    except json.JSONDecodeError:
        # Some tools may print a non-JSON preamble; grab the last JSON object.
        start = out.rfind("{")
        if start != -1:
            try:
                return json.loads(out[start:]), None
            except json.JSONDecodeError:
                pass
        return None, "non-JSON output"


def _records(result):
    """Pull a list of records out of an aTask envelope {success, content|content.data}."""
    if not isinstance(result, dict) or result.get("error") or result.get("success") is False:
        return []
    content = result.get("content")
    if isinstance(content, dict):
        data = content.get("data")
        return data if isinstance(data, list) else [content]
    if isinstance(content, list):
        return content
    return []


def _single(result):
    recs = _records(result)
    return recs[0] if recs else {}


def _first(d, *keys, default=""):
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return v
    return default


def _looks_like_ac(text):
    t = (text or "").lower()
    return any(h in t for h in AC_HINT)


# --- aTask source ----------------------------------------------------------------

def _fetch_records(script, cli_args, label, notes):
    """Run a tool, append any error to notes, return the record list (possibly empty)."""
    res, err = _run_tool(_ATASK_TOOLS, script, cli_args)
    if err:
        notes.append(f"{label}: {err}")
        return []
    return _records(res)


def _atask_checklist(task_id, notes, ac_seeds):
    lines = []
    for it in _fetch_records("checklists.py", ["list", str(task_id)], "checklists", notes):
        name = _first(it, "name", "content", "title", default="").strip()
        if not name:
            continue
        checked = str(_first(it, "value", "checked", default="")).lower()
        box = "[x]" if checked in ("checked", "true", "1") else "[ ]"
        lines.append(f"- {box} {name}")
        ac_seeds.append(name)  # each checklist item is a natural AC seed
    return (f"Checklist ({len(lines)})", "\n".join(lines)) if lines else None


def _atask_comments(task_id, notes, ac_seeds):
    lines = []
    for c in _fetch_records("checklists.py", ["comments", str(task_id)], "comments", notes):
        body = _first(c, "content", "comment", "text", default="").strip()
        if not body:
            continue
        who = _first(c, "createdByName", "userName", "author", default="?")
        when = str(_first(c, "createdAt", "createDate", "createdDate", default=""))[:10]
        lines.append(f"- **{who}** ({when}): {body}")
        if _looks_like_ac(body):
            ac_seeds.append(body[:200])
    return (f"Comments ({len(lines)})", "\n".join(lines)) if lines else None


def _atask_subtasks(task_id, notes):
    lines = []
    for s in _fetch_records("tasks.py", ["subtasks", str(task_id), "--format", "json"],
                            "subtasks", notes):
        name = _first(s, "name", "title", default="").strip()
        if not name:
            continue
        sid = _first(s, "id", default="?")
        sst = _first(s, "statusName", "statusType", default="")
        lines.append(f"- [{sst}] {name} (id={sid})")
    return (f"Subtasks ({len(lines)})", "\n".join(lines)) if lines else None


def _merge_detail(task, detail):
    """REST trực tiếp (Bearer) trả field mà kênh ai/execute thiếu — detail thắng
    theo từng khoá không-rỗng. Trả (merged, ok)."""
    if not isinstance(detail, dict) or detail.get("error"):
        return task, False
    merged = dict(task)
    for k, v in detail.items():
        if v not in (None, "", [], {}):
            merged[k] = v
    return merged, True


def _gather_atask(task_id, want_comments, want_checklist, want_subtasks):
    notes, ac_seeds, sections = [], [], []

    task_res, err = _run_tool(_ATASK_TOOLS, "tasks.py", ["get", str(task_id), "--format", "json"])
    if err:
        notes.append(f"get_task: {err}")
    task = _single(task_res) if task_res else {}

    # Làm giàu bằng REST trực tiếp GET /api/tasks/{id} (cần ATASK_BEARER_TOKEN) —
    # kênh ai/execute bỏ sót description/custom fields ở một số task.
    detail, derr = _run_tool(_ATASK_TOOLS, "tasks.py", ["get-detail", str(task_id)])
    if derr:
        notes.append(f"get_detail: {derr[:150]}")
    else:
        task, ok = _merge_detail(task, detail)
        if not ok:
            notes.append(f"get_detail: {str((detail or {}).get('message', 'error'))[:150]}")

    title = _first(task, "name", "title", default="")
    desc = _first(task, "description", "desc", default="")

    for enabled, sec in (
        (want_checklist, lambda: _atask_checklist(task_id, notes, ac_seeds)),
        (want_comments, lambda: _atask_comments(task_id, notes, ac_seeds)),
        (want_subtasks, lambda: _atask_subtasks(task_id, notes)),
    ):
        if enabled:
            section = sec()
            if section:
                sections.append(section)

    meta = {"parent_id": _first(task, "parentId", "parent_id", default=""),
            "status": _first(task, "statusName", "statusType", default="")}
    return title, desc, sections, ac_seeds, notes, meta


# --- Azure DevOps source ----------------------------------------------------------

def _gather_azure(task_id):
    notes, ac_seeds, sections = [], [], []
    res, err = _run_tool(_DEV_TOOLS, "azure_devops.py", ["get", str(task_id)])
    if err:
        notes.append(f"get_work_item: {err}")
        return "", "", [], [], notes, {}
    if not isinstance(res, dict) or res.get("error"):
        notes.append(f"get_work_item: {(res or {}).get('message', 'error')}")
        return "", "", [], [], notes, {}

    title = res.get("title", "")
    desc = res.get("description", "")
    for label, key in (("Acceptance criteria", "acceptance_criteria"),
                       ("Root cause", "root_cause"), ("Solution", "solution")):
        val = (res.get(key) or "").strip()
        if val:
            sections.append((label, val))
            if key == "acceptance_criteria":
                for ln in val.splitlines():
                    ln = ln.strip("-* \t")
                    if ln:
                        ac_seeds.append(ln[:200])
    meta = {"status": res.get("state", ""), "type": res.get("work_item_type", ""),
            "tags": res.get("tags", "")}
    return title, desc, sections, ac_seeds, notes, meta


# --- render ----------------------------------------------------------------------

def _render(source, task_id, title, desc, sections, meta):
    out = [f"# Context pack — {source} task {task_id}", ""]
    if title:
        out += [f"**Title:** {title}"]
    bits = [f"{k}={v}" for k, v in (meta or {}).items() if v]
    if bits:
        out += [f"_{' · '.join(bits)}_"]
    out += ["", "## Description", (desc.strip() or "_(trống — mô tả sơ sài, dựa vào các mục dưới)_"), ""]
    for heading, body in sections:
        out += [f"## {heading}", body, ""]
    return "\n".join(out).rstrip() + "\n"


def cmd_build(args):
    task_id = args.task
    source = args.source.lower()
    if source == "atask":
        title, desc, sections, ac_seeds, notes, meta = _gather_atask(
            task_id, not args.no_comments, not args.no_checklist, not args.no_subtasks)
    elif source == "azure":
        title, desc, sections, ac_seeds, notes, meta = _gather_azure(task_id)
    else:
        return {"error": True, "message": f"unknown source '{source}' (atask|azure)"}

    pack = _render(source, task_id, title, desc, sections, meta)

    # Clean anchors for scout: derived from real content (title + desc + section bodies),
    # NOT the markdown chrome — so `grounding.py scout --keywords` stays on-target.
    content_text = " ".join([title or "", desc or ""] + [body for _, body in sections])
    keywords = grounding._keywords(None, content_text)

    out_path = args.out or os.path.join(
        _repo_root(), "temp", "runs", f"{source}-{task_id}_context.md")
    try:
        os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(pack)
    except OSError as e:
        return {"error": True, "message": f"cannot write pack: {e}"}

    # De-dupe ac_seeds preserving order.
    seen, seeds = set(), []
    for s in ac_seeds:
        s = (s or "").strip()
        if s and s.lower() not in seen:
            seen.add(s.lower())
            seeds.append(s)

    desc_words = len((desc or "").split())
    enriched_words = len(pack.split())
    return {
        "ok": True,
        "source": source,
        "task_id": task_id,
        "pack_path": out_path,
        "title": title,
        "section_count": len(sections),
        "sections": [h for h, _ in sections],
        "keywords": keywords,
        "ac_seeds": seeds,
        "signals": {
            "desc_words": desc_words,
            "enriched_words": enriched_words,
            "enrichment_ratio": round(enriched_words / desc_words, 1) if desc_words else None,
            "thin_description": desc_words < 12,
            "has_extra_context": bool(sections),
        },
        "notes": notes,
    }


def main():
    ap = argparse.ArgumentParser(description="Assemble an Intake context pack for a task.")
    sub = ap.add_subparsers(dest="action", required=True)
    b = sub.add_parser("build", help="Gather task + comments + checklist + subtasks -> pack")
    b.add_argument("--source", required=True, help="atask | azure")
    b.add_argument("--task", required=True, help="task / work-item id")
    b.add_argument("--type", default=None, help="hint bugfix|feature (passed through, optional)")
    b.add_argument("--out", default=None, help="pack output path (default temp/runs/<src>-<id>_context.md)")
    b.add_argument("--no-comments", action="store_true", help="skip comment fetch")
    b.add_argument("--no-checklist", action="store_true", help="skip checklist fetch")
    b.add_argument("--no-subtasks", action="store_true", help="skip subtask fetch")

    args = ap.parse_args()
    try:
        out = cmd_build(args)
    except (OSError, ValueError) as e:
        out = {"error": True, "message": str(e)}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 1 if isinstance(out, dict) and out.get("error") else 0


if __name__ == "__main__":
    sys.exit(main())
