#!/usr/bin/env python3
"""Approve-then-post for MR reviews — the ONLY path that writes a review to GitLab.

Hard gate (enforced here, whichever agent/provider drafted the review):
  1. review.json must validate and be for the head the workspace was prepared at
  2. EITHER the exact payload passes the auto-post rules (review_rules.py, REVIEW_AUTO_POST=auto)
     OR a human approved THIS payload (Telegram button · terminal TTY). Every approval is bound
     to a hash of the text to post — edit the review after approving and the approval is void.
     Orca board status / gates are display only: agents can set them, so they never approve.
  3. the MR must not have moved since (stale -> requeued, nothing posted)
  4. our marker for this head is not already on the MR (reconcile before any retry)

Interim limit (until the isolated executor, design §10.2): the job and approval files are
still writable by a process running as the same OS user, so a determined local agent could
forge a human approval. The auto-post rules do not depend on those files.

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
import hashlib
import json
import os
import re
import subprocess
import sys

import review_common as rc
import review_rules as rr

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
        if not isinstance(f, dict):
            errs.append(f"findings[{i}] must be an object")
            continue
        for k in ("title", "body", "suggestion", "file"):
            if f.get(k) is not None and not isinstance(f.get(k), str):
                errs.append(f"findings[{i}].{k} must be a string")
        if f.get("severity") not in SEVERITIES:
            errs.append(f"findings[{i}].severity must be one of {SEVERITIES}")
        if not f.get("title"):
            errs.append(f"findings[{i}].title is required")
        if f.get("line") is not None and not isinstance(f.get("line"), int):
            errs.append(f"findings[{i}].line must be an integer")
        if f.get("side", "new") not in ("new", "old"):
            errs.append(f"findings[{i}].side must be new|old")
    if review.get("verdict") == "APPROVE" and any(isinstance(f, dict) and f.get("severity") == "blocker"
                                                 for f in findings):
        errs.append("verdict APPROVE with a blocker finding is contradictory")
    return errs


def _counts(findings: list) -> str:
    parts = [f"{sum(1 for f in findings if f.get('severity') == s)} {s}" for s in SEVERITIES]
    return " · ".join(p for p in parts if not p.startswith("0 ")) or "không có finding"


def render_note(job: dict, review: dict, inline_ok: set, approver: str = "") -> str:
    findings = review.get("findings", [])
    lines = [rr.marker(job), f"### 🤖 AI review — {VERDICT_ICON[review['verdict']]}", "", review["summary"].strip(), "",
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


def inline_candidates(review: dict) -> list:
    findings = review.get("findings", [])
    return [i for i, f in enumerate(findings) if f.get("file") and f.get("line")][:MAX_INLINE]


def payload(job: dict, review: dict) -> tuple:
    """(note, inline bodies) — the superset of text that can reach GitLab for this review."""
    findings = review.get("findings", [])
    return render_note(job, review, set()), [render_inline(findings[i]) for i in inline_candidates(review)]


def payload_hash(job: dict, review: dict) -> str:
    note, inline = payload(job, review)
    head = (job.get("diff_refs") or {}).get("head_sha") or job.get("head_sha") or ""
    blob = json.dumps({"head": head, "note": note, "inline": inline}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


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


class ApprovalUncertain(RuntimeError):
    """A request for this exact payload was started but never recorded (crash while sending):
    it may already be on someone's phone. Sending again could produce two cards — a human checks."""


def request_approval(job: dict, via: str = "auto") -> dict:
    """Ask for approval of the CURRENT review payload — once per payload, whoever calls it
    (review_flow, the CLI, an Orca worker), even across crashes and concurrent callers."""
    jid = job["id"]
    with rc.file_lock(f"approval-{jid}", timeout=60):
        job = rc.load_job(jid) or job
        if job.get("quarantine"):
            raise PermissionError(f"job {jid} is quarantined ({job['quarantine'].get('reason', '')[:120]}) — "
                                  "a human must release it")
        review = load_review(job)
        errs = validate(job, review)
        if errs:
            raise ValueError("review.json invalid: " + "; ".join(errs))
        phash = payload_hash(job, review)
        a = job.get("approval") or {}
        if a.get("payload_hash") == phash and a.get("via"):
            return {"job_id": jid, "approval": a, "reused": True}
        intent = job.get("approval_intent") or {}
        if intent.get("payload_hash") == phash:
            raise ApprovalUncertain("an approval request for this exact review may already have been sent "
                                    "(interrupted while sending) — check Telegram, or approve in a terminal")
        job["approval_intent"] = {"payload_hash": phash, "at": rc.now()}   # durable BEFORE anything is sent
        rc.save_job(job, sync=False)
        out = _request_approval_locked(job, review, phash, via)
        return out


def _request_approval_locked(job: dict, review: dict, phash: str, via: str) -> dict:
    note, inline = payload(job, review)
    rules = rr.evaluate(job, note, inline)
    question = _preview(job, review)
    if rules["mode"] == "shadow":
        question += ("\n🤖 Auto-post (shadow): " + ("SẼ tự đăng" if rules["rules_passed"]
                     else "KHÔNG tự đăng — " + "; ".join(rules["reasons"][:3])))
    approval = {"via": None, "requested_at": rc.now(), "payload_hash": phash, "rules": rules}
    if via == "auto" and rules["auto_post"]:
        approval["via"] = "rules"                  # no human: the rules are re-checked at post time
    if not approval["via"] and via in ("auto", "telegram", "orca"):
        approval.update(_telegram_request(job, question) or {})
    if not approval["via"]:
        approval["via"] = "terminal"   # human runs: review_post.py approve <job>
    if rc.orca():                       # Orca board = display only, never an approval source
        st = rc.board_statuses()
        state = "tự đăng theo luật" if approval["via"] == "rules" else f"CHỜ DUYỆT qua {approval['via']}"
        rc.board_card(job, status=st["pending"], note=(f"{VERDICT_ICON[review['verdict']]} · "
                      f"{_counts(review.get('findings', []))} — {state}. Xem: python review_post.py render {job['id']}"))
    job.update(status="awaiting_approval", approval=approval, verdict=review["verdict"],
               review_file=review_path(job))
    job.pop("approval_intent", None)                  # recorded: the send is no longer uncertain
    rc.log_event(job, "approval_requested", via=approval["via"], rules_passed=rules["rules_passed"])
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


def _current_payload(job: dict):
    try:
        review = load_review(job, job.get("review_file", ""))
    except (OSError, ValueError):
        return None, None
    if validate(job, review):
        return None, None
    return review, payload_hash(job, review)


def approval_status(job: dict) -> str:
    """'pending' | 'approved' | 'rejected' — for the payload as it is NOW."""
    a = job.get("approval") or {}
    if job.get("quarantine"):            # an agent tampered with this job: nothing counts until a human releases it
        return "pending"
    review, phash = _current_payload(job)
    if review is None:
        return "pending"
    if a.get("via") == "rules":
        note, inline = payload(job, review)
        return "approved" if rr.evaluate(job, note, inline)["auto_post"] else "pending"
    if a.get("via") == "terminal":
        decision = a.get("decision")
    elif a.get("via") == "telegram":
        sys.path.insert(0, os.path.normpath(os.path.join(rc.tools_dir(), "..", "..", "remote-control", "tools")))
        import approvals
        decision = {"approved": "approved", "denied": "rejected"}.get(approvals.get(a.get("id")).get("status"))
    else:
        return "pending"   # orca-board / orca gate / unknown: agents can set these, never trusted
    if decision == "rejected":
        return "rejected"
    # an approval counts only for the exact payload the human saw
    return "approved" if decision == "approved" and a.get("payload_hash") == phash else "pending"


def human_decide(job: dict, approved: bool) -> dict:
    if not sys.stdin.isatty():
        raise PermissionError("approve/reject needs an interactive terminal (a human) — agents cannot approve")
    review, phash = _current_payload(job)
    if review is None:
        raise ValueError("review.json missing or invalid — nothing to approve")
    print(render_note(job, review, set()))
    ans = input(f"\n{'APPROVE' if approved else 'REJECT'} THIS text for {job['id']} ({job.get('title', '')[:60]})? [y/N] ")
    if ans.strip().lower() not in ("y", "yes"):
        return {"job_id": job["id"], "changed": False}
    job.setdefault("approval", {}).update(via="terminal", decision="approved" if approved else "rejected",
                                          payload_hash=phash,
                                          by=os.environ.get("USER") or os.environ.get("USERNAME") or "terminal")
    rc.log_event(job, "decided", decision=job["approval"]["decision"])
    rc.save_job(job)
    return {"job_id": job["id"], "decision": job["approval"]["decision"]}


# --------------------------------------------------------------------------- post

def _inline(gl, job: dict, f: dict) -> bool:
    refs = job["diff_refs"]
    side_new = f.get("side", "new") == "new"
    pos = {"position_type": "text", "base_sha": refs["base_sha"], "start_sha": refs["start_sha"],
           "head_sha": refs["head_sha"], "new_path": f["file"], "old_path": f.get("old_file") or f["file"]}
    pos["new_line" if side_new else "old_line"] = f["line"]
    url = gl._api(f"/merge_requests/{job['iid']}/discussions")
    res = gl._request(url, method="POST", data={"body": render_inline(f), "position": pos})
    return not (isinstance(res, dict) and res.get("error"))


def post(jid: str, dry_run: bool = False, notify: bool = True) -> dict:
    # one poster per job across processes (several group_watch sweepers may run)
    with rc.file_lock(f"post-{jid}", timeout=5 if not dry_run else 0.5, stale_after=1800):
        return _post_locked(jid, dry_run, notify)


def _safe_name(s: str) -> str:
    return re.sub(r"[^\w.-]", "", s or "")[:40]


def _post_locked(jid: str, dry_run: bool, notify: bool) -> dict:
    job = rc.load_job(jid)
    if not job:
        raise ValueError(f"no job {jid}")
    if job.get("status") in ("posted", "cancelled", "rejected"):
        return {"job_id": jid, "skipped": f"status {job.get('status')}"}
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
    inline_cands = inline_candidates(review)
    a = job.get("approval") or {}
    auto = a.get("via") == "rules"
    approver = "luật tự đăng" if auto else _safe_name(a.get("by") or a.get("via", ""))
    if dry_run:
        return {"job_id": jid, "dry_run": True, "auto": auto, "inline_candidates": len(inline_cands),
                "note": render_note(job, review, set(), approver)}

    head = (job.get("diff_refs") or {}).get("head_sha") or ""
    done = set(job.get("inline_done", {}).get(head, []))   # survives a crash between inline and note
    inline_ok = set(done)
    with rc.gitlab_project(job["project_id"]) as gl:
        if rr.already_posted(gl, job):                      # reconcile: lost response on a previous try
            job.update(status="posted", posted_at=rc.now(), auto_posted=auto, reconciled=True)
            rc.log_event(job, "posted_reconciled")
            rc.save_job(job)
            return {"job_id": jid, "posted": True, "reconciled": True}
        for i in inline_cands:
            if i in done:
                continue
            if _inline(gl, job, findings[i]):
                inline_ok.add(i)
                job.setdefault("inline_done", {})[head] = sorted(inline_ok)
                rc.save_job(job, sync=False)
        note = rc.gl_check(gl.add_mr_comment(job["iid"], render_note(job, review, inline_ok, approver)),
                           "post note")
    ws = _ws(job)
    import review_ws as rw
    reviewed = [c for c in (ws.get("commits") or {}).get("list", []) if not c.get("merge")]
    rw.ledger_add(job["project_id"], job["iid"], reviewed)
    job.update(status="posted", posted_at=rc.now(), note_id=note.get("id"), inline_posted=len(inline_ok),
               auto_posted=auto)
    rc.log_event(job, "posted", verdict=review["verdict"], inline=len(inline_ok), auto=auto)
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
            except (PermissionError, ValueError, RuntimeError, TimeoutError) as e:
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
