#!/usr/bin/env python3
"""Feedback ledger — record every human intervention so the pipeline learns from it.

The auto-dev pipeline keeps a human in the loop (checkpoints, Telegram approvals), but
until now those decisions evaporated: approval files were deleted after the call, and
run_log checkpoints only stored approved/rejected, never *what the human changed*. With
no memory, the same vague-task mistakes get made every run.

This tool is the missing memory. Each time a human edits a plan, rejects an approval,
overrides a triage tier, or a review misses something a human later fixed, append a
record here. Then `recall` feeds the most relevant past corrections back into the next
run's prompts (debate / clarify / triage) so the agent stops repeating them.

Storage: <work_dir>/feedback/<project>.jsonl — append-only, never deleted, gitignored
(machine-specific; back it up / sync it if you run on several machines). work_dir is
$WORK_DIR or <repo>/work (same resolution as test_runner.py / project_config.py).

Record schema:
    {"ts","run_id","project","stage","task_type","tier",
     "agent_output","human_action":"edited|approved|rejected|overridden",
     "correction","reason","tags":[...]}

Stdlib only. Output: one JSON object on stdout.

Usage:
    python feedback.py add --project atask --stage plan --run-id atask-123 \\
        --task-type bugfix --action edited \\
        --correction "plan targeted UserService, real fix is in AuthFilter" \\
        --reason "this repo puts authz in filters, not services" --tags convention,wrong-file
    python feedback.py recall --project atask --stage plan --type bugfix --query "authz null check"
    python feedback.py list --project atask --stage plan --limit 10
    python feedback.py search --project atask --tags convention --query "exception"
    python feedback.py stats --project atask
"""

import argparse
import json
import os
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

VALID_ACTIONS = ("edited", "approved", "rejected", "overridden")
# Only these actions carry a learning signal worth recalling into future prompts.
CORRECTION_ACTIONS = ("edited", "rejected", "overridden")
RECALL_BLOCK_BUDGET = 1500  # chars — keep the injected block small


def _repo_root():
    search = os.path.dirname(os.path.abspath(__file__))
    for _ in range(6):
        if os.path.isdir(os.path.join(search, ".git")) or \
           os.path.isfile(os.path.join(search, "CLAUDE.md")):
            return search
        search = os.path.dirname(search)
    return os.path.dirname(os.path.abspath(__file__))


def _work_dir():
    return os.environ.get("WORK_DIR") or os.path.join(_repo_root(), "work")


def _ledger_path(project):
    safe = "".join(c for c in str(project) if c.isalnum() or c in "-_.") or "default"
    return os.path.join(_work_dir(), "feedback", f"{safe}.jsonl")


def _read(project):
    """Return all records for a project (oldest first); [] if none."""
    path = _ledger_path(project)
    if not os.path.isfile(path):
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # tolerate a partially written trailing line
    return out


def _tokens(text):
    return {t for t in "".join(c.lower() if c.isalnum() else " " for c in (text or "")).split()
            if len(t) >= 3}


def _score(rec, query_tokens, want_tags):
    """Relevance = keyword overlap (correction+reason+output) + tag match, recency tiebreak."""
    hay = _tokens(" ".join(str(rec.get(k, "")) for k in ("correction", "reason", "agent_output")))
    score = len(query_tokens & hay)
    if want_tags:
        score += 2 * len(set(want_tags) & set(rec.get("tags", [])))
    return score


# --- commands --------------------------------------------------------------------

def cmd_add(args):
    if args.action not in VALID_ACTIONS:
        return {"error": True, "message": f"--action must be one of {VALID_ACTIONS}"}
    rec = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "run_id": args.run_id or "",
        "project": args.project,
        "stage": args.stage or "",
        "task_type": args.task_type or "",
        "tier": args.tier or "",
        "agent_output": (args.agent_output or "")[:2000],
        "human_action": args.action,
        "correction": (args.correction or "")[:2000],
        "reason": (args.reason or "")[:1000],
        "tags": [t.strip() for t in (args.tags or "").split(",") if t.strip()],
    }
    path = _ledger_path(args.project)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError as e:
        return {"error": True, "message": f"cannot write ledger: {e}"}
    return {"ok": True, "path": path, "record": rec}


def _filter(records, stage, task_type, tags):
    out = records
    if stage:
        out = [r for r in out if r.get("stage") == stage]
    if task_type:
        out = [r for r in out if r.get("task_type") == task_type]
    if tags:
        want = set(tags)
        out = [r for r in out if want & set(r.get("tags", []))]
    return out


def cmd_list(args):
    records = _filter(_read(args.project), args.stage, args.task_type,
                      [t.strip() for t in (args.tags or "").split(",") if t.strip()])
    records = records[-args.limit:] if args.limit else records
    return {"ok": True, "project": args.project, "count": len(records), "records": records}


def cmd_search(args):
    tags = [t.strip() for t in (args.tags or "").split(",") if t.strip()]
    records = _filter(_read(args.project), args.stage, args.task_type, tags)
    qt = _tokens(args.query)
    scored = sorted(
        ((_score(r, qt, tags), r) for r in records),
        key=lambda sr: sr[0], reverse=True)
    hits = [r for s, r in scored if s > 0][:args.limit] if qt or tags else records[-args.limit:]
    return {"ok": True, "project": args.project, "count": len(hits), "records": hits}


def _recall_block(records):
    lines = ["<past_corrections>"]
    used = len(lines[0])
    for r in records:
        reason = (r.get("reason") or "").strip()
        corr = (r.get("correction") or "").strip()
        body = " — ".join(x for x in (reason, corr) if x) or "(no detail)"
        # Angle brackets in a correction (code snippets, XML) must not break the
        # pseudo-XML structure of the injected block.
        body = body.replace("<", "&lt;").replace(">", "&gt;")
        entry = (f'<correction stage="{r.get("stage", "")}" '
                 f'type="{r.get("task_type", "")}">{body}</correction>')
        if used + len(entry) > RECALL_BLOCK_BUDGET:
            break
        lines.append(entry)
        used += len(entry)
    lines.append("</past_corrections>")
    return "\n".join(lines)


def cmd_recall(args):
    """Most relevant past *corrections* for (project, stage, type) → injectable block."""
    records = _filter(_read(args.project), args.stage, args.task_type, None)
    records = [r for r in records if r.get("human_action") in CORRECTION_ACTIONS]
    qt = _tokens(args.query)
    # Rank by relevance; when no query, fall back to most recent.
    if qt:
        scored = sorted(((_score(r, qt, None), r) for r in records),
                        key=lambda sr: sr[0], reverse=True)
        picked = [r for s, r in scored if s > 0][:args.limit]
        if not picked:  # nothing matched keywords → still surface recent lessons
            picked = records[-args.limit:]
    else:
        picked = records[-args.limit:]
    return {
        "ok": True,
        "project": args.project,
        "count": len(picked),
        "records": picked,
        "block": _recall_block(picked) if picked else "",
    }


def cmd_stats(args):
    records = _read(args.project)
    by_stage = {}
    tag_counts = {}
    for r in records:
        st = r.get("stage") or "?"
        s = by_stage.setdefault(st, {"total": 0, "clean": 0, "corrections": 0})
        s["total"] += 1
        if r.get("human_action") == "approved" and not (r.get("correction") or "").strip():
            s["clean"] += 1
        if r.get("human_action") in CORRECTION_ACTIONS:
            s["corrections"] += 1
        for t in r.get("tags", []):
            tag_counts[t] = tag_counts.get(t, 0) + 1
    for st, s in by_stage.items():
        s["straight_through_rate"] = round(s["clean"] / s["total"], 2) if s["total"] else None
    return {
        "ok": True,
        "project": args.project,
        "total_records": len(records),
        "by_stage": by_stage,
        "top_tags": dict(sorted(tag_counts.items(), key=lambda kv: kv[1], reverse=True)[:10]),
    }


def main():
    ap = argparse.ArgumentParser(description="Feedback ledger: record + recall human interventions.")
    sub = ap.add_subparsers(dest="action_cmd", required=True)

    a = sub.add_parser("add", help="append a feedback record")
    a.add_argument("--project", required=True)
    a.add_argument("--stage", default=None, help="plan|implement|test|review|triage|deliver")
    a.add_argument("--run-id", default=None)
    a.add_argument("--task-type", default=None, help="bugfix|feature|...")
    a.add_argument("--tier", default=None)
    a.add_argument("--action", required=True, help="edited|approved|rejected|overridden")
    a.add_argument("--agent-output", default=None, help="summary/hash of what the agent proposed")
    a.add_argument("--correction", default=None, help="what was changed (diff or prose)")
    a.add_argument("--reason", default=None, help="WHY — the single most valuable field")
    a.add_argument("--tags", default=None, help="comma list: convention,wrong-file,missed-ac,style")

    for name, help_ in (("list", "list records (newest last)"),
                        ("search", "keyword/tag search")):
        p = sub.add_parser(name, help=help_)
        p.add_argument("--project", required=True)
        p.add_argument("--stage", default=None)
        p.add_argument("--task-type", default=None)
        p.add_argument("--tags", default=None)
        p.add_argument("--limit", type=int, default=20)
        if name == "search":
            p.add_argument("--query", default="")

    r = sub.add_parser("recall", help="most relevant past corrections → injectable block")
    r.add_argument("--project", required=True)
    r.add_argument("--stage", default=None)
    r.add_argument("--type", dest="task_type", default=None)
    r.add_argument("--query", default="", help="free text (e.g. task description) to rank by")
    r.add_argument("--limit", type=int, default=5)

    s = sub.add_parser("stats", help="straight-through rate + tag distribution")
    s.add_argument("--project", required=True)

    args = ap.parse_args()
    dispatch = {"add": cmd_add, "list": cmd_list, "search": cmd_search,
                "recall": cmd_recall, "stats": cmd_stats}
    try:
        out = dispatch[args.action_cmd](args)
    except (OSError, ValueError) as e:
        out = {"error": True, "message": str(e)}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 1 if isinstance(out, dict) and out.get("error") else 0


if __name__ == "__main__":
    sys.exit(main())
