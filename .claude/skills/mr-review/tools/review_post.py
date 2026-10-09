#!/usr/bin/env python3
"""Approve-then-post for MR reviews — the ONLY path that writes a review to GitLab.

Hard gate (enforced here, whichever agent/provider drafted the review):
  1. review.json must validate and be for the head the workspace was prepared at
  2. a human must approve (Orca gate · Telegram button · terminal) — agents cannot,
     `approve` needs an interactive TTY
  3. the MR must not have moved since (stale -> requeued, nothing posted)

    python review_post.py validate <job_id> [--file F]
    python review_post.py render <job_id>                  # preview the GitLab note
    python review_post.py request-approval <job_id> [--via auto|orca|telegram|terminal]
    python review_post.py approval-status <job_id>
    python review_post.py approve|reject <job_id>          # human, interactive terminal only
    python review_post.py post <job_id> [--dry-run] [--no-notify]
    python review_post.py sweep [--dry-run]                # post approved / close rejected (no LLM)

review.json (default: <worktree>.review.json):
{
  "head_sha": "<diff_refs.head_sha from ws.json>",
  "verdict": "APPROVE | REQUEST_CHANGES | COMMENT",
  "summary": "markdown, 1-6 câu",
  "findings": [{"severity": "blocker|major|minor|nit", "title": "...", "body": "...",
                "file": "path/in/repo", "line": 42, "side": "new|old", "suggestion": "..."}],
  "tests": [{"cmd": "...", "result": "pass|fail|skipped", "note": "..."}],
  "reviewed_commits": "all | new_only",
  "model": "provider:model"
}
"""

import argparse
import json
import os
import subprocess
import sys

import review_common as rc

VERDICTS = ("APPROVE", "REQUEST_CHANGES", "COMMENT")
SEVERITIES = ("blocker", "major", "minor", "nit")
SEV_ICON = {"blocker": "🔴", "major": "🟡", "minor": "🟢", "nit": "⚪"}   # same as code_review_prompt.md
VERDICT_ICON = {"APPROVE": "🟢 APPROVE", "REQUEST_CHANGES": "🔴 REQUEST_CHANGES", "COMMENT": "🟡 COMMENT"}
MAX_INLINE = 25


# --------------------------------------------------------------------------- review.json

def review_path(job: dict, override: str = "") -> str:
    return override or (job.get("worktree", "") + ".review.json")


def load_review(job: dict, override: str = "") -> dict:
    with open(review_path(job, override), encoding="utf-8") as f:
        return json.load(f)


def validate(job: dict, review: dict) -> list:
    errs = []
    head = (job.get("diff_refs") or {}).get("head_sha") or job.get("head_sha")
    if review.get("head_sha") != head:
        errs.append(f"head_sha {str(review.get('head_sha'))[:10]} != prepared head {str(head)[:10]}")
    if review.get("verdict") not in VERDICTS:
        errs.append(f"verdict must be one of {VERDICTS}")
    if not isinstance(review.get("summary"), str) or not review["summary"].strip():
        errs.append("summary is required")
    findings = review.get("findings")
    if not isinstance(findings, list):
        errs.append("findings must be a list")
        findings = []
    for i, f in enumerate(findings):
        if f.get("severity") not in SEVERITIES:
            errs.append(f"findings[{i}].severity must be one of {SEVERITIES}")
        if not f.get("title"):
            errs.append(f"findings[{i}].title is required")
        if f.get("line") is not None and not isinstance(f.get("line"), int):
            errs.append(f"findings[{i}].line must be an integer")
        if f.get("side", "new") not in ("new", "old"):
            errs.append(f"findings[{i}].side must be new|old")
    if review.get("verdict") == "APPROVE" and any(f.get("severity") == "blocker" for f in findings):
        errs.append("verdict APPROVE with a blocker finding is contradictory")
    return errs


def _counts(findings: list) -> str:
    parts = [f"{sum(1 for f in findings if f.get('severity') == s)} {s}" for s in SEVERITIES]
    return " · ".join(p for p in parts if not p.startswith("0 ")) or "không có finding"


def render_note(job: dict, review: dict, inline_ok: set, approver: str = "") -> str:
    findings = review.get("findings", [])
    lines = [f"### 🤖 Review — {VERDICT_ICON[review['verdict']]}", "", review["summary"].strip(), "",
             f"**Findings:** {_counts(findings)}" + (f" ({len(inline_ok)} comment inline)" if inline_ok else "")]
    rest = [(i, f) for i, f in enumerate(findings) if i not in inline_ok]
    if rest:
        lines.append("")
        for _, f in rest:
            loc = f" — `{f['file']}" + (f":{f['line']}`" if f.get("line") else "`") if f.get("file") else ""
            lines.append(f"- {SEV_ICON[f['severity']]} **{f['title']}**{loc}")
            if f.get("body"):
                lines.append(f"  {f['body'].strip()}")
            if f.get("suggestion"):
                lines.append(f"  _Đề xuất:_ {f['suggestion'].strip()}")
    tests = review.get("tests") or []
    if tests:
        lines += ["", "**Kiểm thử:** " + " · ".join(f"`{t.get('cmd', '?')}` → {t.get('result', '?')}" for t in tests)]
    ws = _ws(job)
    c = (ws.get("commits") or {})
    scope = f"{c.get('new', '?')}/{c.get('total', '?')} commit mới" if c else ""
    head = ((job.get("diff_refs") or {}).get("head_sha") or "")[:8]
    meta = [x for x in (review.get("model") and f"model {review['model']}", approver and f"duyệt bởi {approver}",
                        f"head {head}", scope) if x]
    lines += ["", "---", "_" + " · ".join(meta) + "_"]
    return "\n".join(lines)


def render_inline(f: dict) -> str:
    body = f"{SEV_ICON[f['severity']]} **{f['title']}**"
    if f.get("body"):
        body += f"\n\n{f['body'].strip()}"
    if f.get("suggestion"):
        body += f"\n\n_Đề xuất:_ {f['suggestion'].strip()}"
    return body


def _ws(job: dict) -> dict:
    try:
        with open(job.get("ws_path", ""), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


# --------------------------------------------------------------------------- approval

def _preview(job: dict, review: dict) -> str:
    f = review.get("findings", [])
    top = "\n".join(f"{SEV_ICON[x['severity']]} {x['title']}" for x in f[:8])
    return (f"Đăng review {job.get('project_path')}!{job['iid']} ({job.get('source_branch')} → "
            f"{job.get('target_branch')})?\n{VERDICT_ICON[review['verdict']]} — {_counts(f)}\n{top}\n"
            f"{job.get('web_url', '')}\nXem đầy đủ: python review_post.py render {job['id']}")


def request_approval(job: dict, via: str = "auto") -> dict:
    review = load_review(job)
    errs = validate(job, review)
    if errs:
        raise ValueError("review.json invalid: " + "; ".join(errs))
    question = _preview(job, review)
    approval = {"via": None, "requested_at": rc.now()}
    if via in ("auto", "orca") and rc.orca():
        st = rc.board_statuses()
        note = (f"{VERDICT_ICON[review['verdict']]} · {_counts(review.get('findings', []))} — CHỜ DUYỆT. "
                f"Kéo sang '{st['approve']}' = đăng, '{st['reject']}' = bỏ. "
                f"Xem: python review_post.py render {job['id']}")
        if rc.board_card(job, status=st["pending"], note=note):
            approval.update(via="orca-board", card=job["worktree"], statuses=st)
    if not approval["via"] and via in ("auto", "telegram"):
        approval.update(_telegram_request(job, question) or {})
    if not approval["via"]:
        approval["via"] = "terminal"   # human runs: review_post.py approve <job>
    job.update(status="awaiting_approval", approval=approval, verdict=review["verdict"],
               review_file=review_path(job))
    rc.log_event(job, "approval_requested", via=approval["via"])
    rc.save_job(job)
    return {"job_id": job["id"], "approval": approval, "preview": question}


def _telegram_request(job: dict, question: str) -> dict:
    rc_tools = os.path.join(rc.tools_dir(), "..", "..", "remote-control", "tools")
    sys.path.insert(0, os.path.normpath(rc_tools))
    try:
        import approvals
        import tg_api
        bot = tg_api.approval_bot()
        chats = tg_api.allowed_chats(bot)
        if not chats:
            return {}
        req = approvals.create("review_post", f"post review {job['id']}", question, risk="write")
        tg_api.send_message(chats[0], question, reply_markup=tg_api.approve_keyboard(req), bot=bot,
                            parse_mode="")
        return {"via": "telegram", "id": req}
    except Exception as e:  # noqa: BLE001
        job["approval_error"] = str(e)[:200]
        return {}


def approval_status(job: dict) -> str:
    """'pending' | 'approved' | 'rejected'."""
    a = job.get("approval") or {}
    # Orca / Telegram: always re-read the source of truth (the job file is writable by
    # agents, so a "decision" stored there is only trusted for the terminal channel).
    if a.get("via") == "terminal":
        return a.get("decision") if a.get("decision") in ("approved", "rejected") else "pending"
    if a.get("via") == "orca-board":
        st, cur = a.get("statuses") or rc.board_statuses(), rc.board_status(job)
        if cur == st.get("approve"):
            return "approved"
        if cur == st.get("reject"):
            return "rejected"
        return "pending"
    if a.get("via") == "orca":
        o = rc.orca()
        if not o:
            return "pending"
        g = o.gate_get(a.get("run"), a.get("id"))
        if g.get("status") == "resolved":
            res = (g.get("resolution") or "").strip().lower()
            return "approved" if res in ("approve", "approved", "yes", "ok", "duyệt") else "rejected"
        return "pending"
    if a.get("via") == "telegram":
        sys.path.insert(0, os.path.normpath(os.path.join(rc.tools_dir(), "..", "..", "remote-control", "tools")))
        import approvals
        st = approvals.get(a.get("id")).get("status")
        return {"approved": "approved", "denied": "rejected"}.get(st, "pending")
    return "pending"


def human_decide(job: dict, approved: bool) -> dict:
    if not sys.stdin.isatty():
        raise PermissionError("approve/reject needs an interactive terminal (a human) — agents cannot approve")
    ans = input(f"{'APPROVE' if approved else 'REJECT'} review {job['id']} ({job.get('title', '')[:60]})? [y/N] ")
    if ans.strip().lower() not in ("y", "yes"):
        return {"job_id": job["id"], "changed": False}
    job.setdefault("approval", {}).update(decision="approved" if approved else "rejected",
                                          by=os.environ.get("USER") or os.environ.get("USERNAME") or "terminal")
    rc.log_event(job, "decided", decision=job["approval"]["decision"])
    rc.save_job(job)
    return {"job_id": job["id"], "decision": job["approval"]["decision"]}


# --------------------------------------------------------------------------- post

def _inline(gl, job: dict, f: dict) -> bool:
    refs = job["diff_refs"]
    side_new = f.get("side", "new") == "new"
    pos = {"position_type": "text", "base_sha": refs["base_sha"], "start_sha": refs["start_sha"],
           "head_sha": refs["head_sha"], "new_path": f["file"], "old_path": f["file"]}
    pos["new_line" if side_new else "old_line"] = f["line"]
    url = gl._api(f"/merge_requests/{job['iid']}/discussions")
    res = gl._request(url, method="POST", data={"body": render_inline(f), "position": pos})
    return not (isinstance(res, dict) and res.get("error"))


def post(jid: str, dry_run: bool = False, notify: bool = True) -> dict:
    job = rc.load_job(jid)
    if not job:
        raise ValueError(f"no job {jid}")
    if job.get("status") == "posted":
        return {"job_id": jid, "skipped": "already posted"}
    decision = approval_status(job)
    if decision != "approved":
        raise PermissionError(f"not approved (approval: {decision}) — nothing posted")
    review = load_review(job, job.get("review_file", ""))
    errs = validate(job, review)
    if errs:
        raise ValueError("review.json invalid: " + "; ".join(errs))
    import review_ws
    st = review_ws.check_stale(jid)
    if st["stale"]:
        job.update(status="queued" if st["state"] == "opened" else "cancelled", head_sha=st["current_head"],
                   approval=None, previous_review=job.get("review_file"))
        rc.log_event(job, "stale", reviewed=str(st["reviewed_head"])[:8], current=str(st["current_head"])[:8])
        rc.save_job(job)
        return {"job_id": jid, "stale": True, "requeued": job["status"] == "queued", "posted": False}

    findings = review.get("findings", [])
    inline_cands = [i for i, f in enumerate(findings) if f.get("file") and f.get("line")][:MAX_INLINE]
    approver = (job.get("approval") or {}).get("by") or (job.get("approval") or {}).get("via", "")
    if dry_run:
        return {"job_id": jid, "dry_run": True, "inline_candidates": len(inline_cands),
                "note": render_note(job, review, set(), approver)}

    inline_ok = set()
    with rc.gitlab_project(job["project_id"]) as gl:
        for i in inline_cands:
            if _inline(gl, job, findings[i]):
                inline_ok.add(i)
        note = rc.gl_check(gl.add_mr_comment(job["iid"], render_note(job, review, inline_ok, approver)),
                           "post note")
    ws = _ws(job)
    import review_ws as rw
    reviewed = [c for c in (ws.get("commits") or {}).get("list", []) if not c.get("merge")]
    rw.ledger_add(job["project_id"], job["iid"], reviewed)
    job.update(status="posted", posted_at=rc.now(), note_id=note.get("id"), inline_posted=len(inline_ok))
    rc.log_event(job, "posted", verdict=review["verdict"], inline=len(inline_ok))
    rc.save_job(job)
    out = {"job_id": jid, "posted": True, "verdict": review["verdict"], "inline": len(inline_ok),
           "note_id": note.get("id")}
    rc.board_card(job, note=f"✅ Đã đăng review ({review['verdict']}, {len(inline_ok)} inline) — {job.get('web_url', '')}")
    if notify:
        out["notified"] = notify_requesters(job, review)
    return out   # worktree (= Orca card) kept until `review_ws.py gc`


def notify_requesters(job: dict, review: dict) -> list:
    """Fixed-template FChat message to each FChat requester (never raw agent output)."""
    script = os.path.normpath(os.path.join(rc.tools_dir(), "..", "..", "tchat-automation", "tools", "notify_group.py"))
    text = (f"🔎 Review {job.get('project_path')}!{job['iid']} — {VERDICT_ICON[review['verdict']]}: "
            f"{_counts(review.get('findings', []))}. {job.get('web_url', '')}")
    sent, seen = [], set()
    for r in job.get("requests", []):
        if r.get("source") != "fchat" or not r.get("group") or (r["group"], r.get("by_id")) in seen:
            continue
        seen.add((r["group"], r.get("by_id")))
        args = [sys.executable, script, "--group", r["group"], "--text", text]
        if r.get("group_type"):
            args += ["--group-type", r["group_type"]]
        if r.get("by_id") and r.get("by"):
            args += ["--user-id", r["by_id"], "--user-name", r["by"]]
        p = subprocess.run(args, capture_output=True, text=True, timeout=60)
        sent.append({"group": r["group"], "ok": p.returncode == 0})
    return sent


def close_idle_workers(dry_run: bool = False) -> list:
    """Close the Orca tab of a worker whose job left `running` (the agent session stays
    open after its turn). Skips a terminal still running another job."""
    if rc.config.get("REVIEW_CLOSE_WORKER_TERMINAL", "1").lower() in ("0", "false", "no"):
        return []
    o = rc.orca()
    if not o:
        return []
    grace = rc.cfg_int("REVIEW_CLOSE_WORKER_AFTER_SEC", 60)
    jobs = rc.all_jobs()
    busy = {j.get("worker_terminal") for j in jobs if j.get("status") == "running"}
    closed = []
    for j in jobs:
        h = j.get("worker_terminal")
        if (not h or j.get("worker_terminal_closed") or j.get("status") == "running" or h in busy
                or rc.now() - j.get("updated", 0) < grace):
            continue
        if h == os.environ.get("ORCA_TERMINAL_HANDLE"):
            continue                                   # never close the caller's own tab
        if not dry_run:
            try:
                o.terminal_close(h)
            except Exception as e:  # noqa: BLE001 — already closed by hand, etc.
                j["worker_terminal_error"] = str(e)[:120]
            j["worker_terminal_closed"] = True
            rc.save_job(j, sync=False)
        closed.append({"job_id": j["id"], "closed_terminal": h})
    return closed


def sweep(dry_run: bool = False) -> dict:
    actions = close_idle_workers(dry_run)
    for job in rc.all_jobs():
        if job.get("status") != "awaiting_approval":
            continue
        decision = approval_status(job)
        if decision == "approved":
            try:
                actions.append(post(job["id"], dry_run=dry_run))
            except (PermissionError, ValueError, RuntimeError) as e:
                actions.append({"job_id": job["id"], "error": str(e)})
        elif decision == "rejected" and not dry_run:
            rc.board_card(job, note="❌ Review bị từ chối — không đăng.")
            job["status"] = "rejected"
            rc.log_event(job, "rejected")
            rc.save_job(job)
            actions.append({"job_id": job["id"], "rejected": True})
    return {"actions": actions}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="review_post.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("validate", "render", "approval-status", "approve", "reject"):
        p = sub.add_parser(name)
        p.add_argument("job_id")
        if name == "validate":
            p.add_argument("--file", default="")
    p = sub.add_parser("request-approval")
    p.add_argument("job_id")
    p.add_argument("--via", default="auto", choices=["auto", "orca", "telegram", "terminal"])
    p = sub.add_parser("post")
    p.add_argument("job_id")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-notify", action="store_true")
    p = sub.add_parser("sweep")
    p.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "sweep":
            return rc.pc.emit(sweep(a.dry_run))
        job = rc.load_job(a.job_id)
        if not job:
            return rc.pc.emit({"error": True, "message": f"no job {a.job_id}"})
        if a.cmd == "validate":
            errs = validate(job, load_review(job, a.file))
            out = {"job_id": a.job_id, "valid": not errs, "errors": errs}
            rc.pc.emit(out)
            return 0 if not errs else 1
        if a.cmd == "render":
            review = load_review(job, job.get("review_file", ""))
            print(render_note(job, review, set()))
            return 0
        if a.cmd == "request-approval":
            out = request_approval(job, a.via)
        elif a.cmd == "approval-status":
            out = {"job_id": a.job_id, "approval": approval_status(job)}
        elif a.cmd in ("approve", "reject"):
            out = human_decide(job, a.cmd == "approve")
        else:
            out = post(a.job_id, a.dry_run, notify=not a.no_notify)
    except FileNotFoundError as e:
        out = {"error": True, "message": f"review file missing: {e.filename}"}
    except (PermissionError, ValueError, RuntimeError, TimeoutError) as e:
        out = {"error": True, "message": str(e)}
    return rc.pc.emit(out)


if __name__ == "__main__":
    sys.exit(main())
