#!/usr/bin/env python3
"""MR review queue — one job per MR, many MRs/projects in parallel.

Listeners (FChat group_watch, MR watchers, a human) only ADD jobs. Workers (an
Orca automation run, or any agent) CLAIM one job at a time with `next`. Claiming
is atomic, so concurrent workers never take the same MR.

    python review_queue.py add --project etask --iid 123 [--requester "Hùng" --requester-id U1
                               --source fchat --group G --group-type T --priority 5 --note "..."]
    python review_queue.py add-text "review giúp https://git/x/y/-/merge_requests/12" [--default-project etask ...]
    python review_queue.py pending          # exit 0 if a job can be started now (Orca precheck)
    python review_queue.py next             # claim one job -> prints it; exit 3 if none
    python review_queue.py list [--status queued,running] [--brief]
    python review_queue.py show <job_id>
    python review_queue.py set <job_id> --status <s> [--note ...]
    python review_queue.py cancel <job_id>

Config: REVIEW_MAX_PARALLEL (default 3) · REVIEW_JOB_TIMEOUT_MIN (default 60, then
requeued; failed after 2 attempts).

Job status: queued -> running -> awaiting_approval -> approved -> posted
            (or rejected / failed / cancelled; stale = MR got new commits, requeued)
"""

import argparse
import os
import sys

import review_common as rc

PRIORITY_DEFAULT = 5   # 0 = most urgent


def _max_parallel() -> int:
    return max(1, rc.cfg_int("REVIEW_MAX_PARALLEL", 3))


def _flow_task_open(job: dict) -> bool:
    """A job taken by review_flow ("flow:<task_id>") lives as long as its durable task is open
    (paused for quota, blocked on a human, running) — its liveness is the store lease, not
    claimed_at. Reaping it would let the Orca path review the same MR twice (Codex F.4b #2)."""
    worker = job.get("worker") or ""
    if not worker.startswith("flow:"):
        return False
    try:
        import runpy
        from pathlib import Path
        runpy.run_path(str(Path(__file__).resolve().parents[2] / "_runtime" / "bootstrap.py"))["activate"]()
        from agent_skill_toolkit.core import store as store_mod
        with store_mod.Store() as st:
            return st.get(worker[5:]).get("status") in store_mod.OPEN
    except Exception:  # noqa: BLE001 — store unreadable: be conservative, do not reap
        return True


def _reap(jobs: list) -> None:
    """Requeue jobs whose worker died (running past the timeout)."""
    limit = rc.cfg_int("REVIEW_JOB_TIMEOUT_MIN", 60) * 60
    for j in jobs:
        if j.get("status") == "running" and rc.now() - j.get("claimed_at", 0) > limit:
            if _flow_task_open(j):
                continue
            j["attempts"] = j.get("attempts", 0) + 1
            j["status"] = "failed" if j["attempts"] >= 2 else "queued"
            rc.log_event(j, "reaped", status=j["status"])
            rc.save_job(j)


def add_job(project, iid, *, requester="", requester_id="", source="manual", group="",
            group_type="", priority=PRIORITY_DEFAULT, note="", mr=None) -> dict:
    project_id, path = rc.resolve_project_id(project)
    mr = mr or rc.gitlab_mr(project_id, iid)
    if mr.get("state") != "opened":
        raise ValueError(f"MR !{iid} is {mr.get('state')} — nothing to review")
    jid = rc.job_id(project_id, iid)
    with rc.file_lock("queue"):
        job = rc.load_job(jid)
        head = (mr.get("diff_refs") or {}).get("head_sha") or mr.get("sha")
        request = {"at": rc.now(), "by": requester, "by_id": requester_id, "source": source,
                   "group": group, "group_type": group_type, "note": note[:500]}
        if job and job.get("status") not in rc.TERMINAL:
            job.setdefault("requests", []).append(request)
            job["priority"] = min(job.get("priority", PRIORITY_DEFAULT), priority)
            if job.get("head_sha") != head and job.get("status") != "queued":
                job["rerequested_head"] = head        # post step will mark the review stale
            rc.log_event(job, "merged_request", by=requester)
            return {"job": rc.save_job(job), "duplicate": True}
        job = {
            "id": jid, "project_id": project_id, "iid": int(iid),
            "project_path": path or (mr.get("references") or {}).get("full", "").split("!")[0],
            "title": mr.get("title", ""), "web_url": mr.get("web_url", ""),
            "author": (mr.get("author") or {}).get("username", ""),
            "source_branch": mr.get("source_branch"), "target_branch": mr.get("target_branch"),
            "head_sha": head, "status": "queued", "priority": priority, "attempts": 0,
            "created": rc.now(), "requests": [request],
            "previous": {k: job.get(k) for k in ("status", "head_sha", "verdict")} if job else None,
        }
        rc.log_event(job, "queued", by=requester, source=source)
        return {"job": rc.save_job(job), "duplicate": False}


def pending() -> dict:
    jobs = rc.all_jobs()
    _reap(jobs)
    queued = [j for j in jobs if j.get("status") == "queued"]
    running = [j for j in jobs if j.get("status") == "running"]
    capacity = _max_parallel() - len(running)
    return {"queued": len(queued), "running": len(running), "capacity": max(0, capacity),
            "can_start": bool(queued) and capacity > 0}


def claim_next(worker: str = "") -> dict:
    with rc.file_lock("queue"):
        jobs = rc.all_jobs()
        _reap(jobs)
        if sum(1 for j in jobs if j.get("status") == "running") >= _max_parallel():
            return {}
        queued = sorted((j for j in jobs if j.get("status") == "queued"),
                        key=lambda j: (j.get("priority", PRIORITY_DEFAULT), j.get("created", 0)))
        if not queued:
            return {}
        job = queued[0]
        job.update(status="running", claimed_at=rc.now(), worker=worker,
                   # Orca sets this in every terminal it opens; sweep closes the tab when done
                   worker_terminal=os.environ.get("ORCA_TERMINAL_HANDLE") or None,
                   worker_terminal_closed=False)
        rc.log_event(job, "claimed", worker=worker)
        return rc.save_job(job)


def set_status(jid: str, status: str, **fields) -> dict:
    if status not in rc.STATUSES:
        raise ValueError(f"status must be one of {rc.STATUSES}")
    with rc.file_lock("queue"):
        job = rc.load_job(jid)
        if not job:
            raise ValueError(f"no job {jid}")
        job["status"] = status
        job.update({k: v for k, v in fields.items() if v is not None})
        rc.log_event(job, status, **{k: v for k, v in fields.items() if k == "note" and v})
        return rc.save_job(job)


def _brief(j: dict) -> dict:
    who = ", ".join(sorted({r.get("by") or r.get("source", "") for r in j.get("requests", [])}))
    return {"id": j["id"], "status": j["status"], "mr": f"{j.get('project_path')}!{j['iid']}",
            "branches": f"{j.get('source_branch')} -> {j.get('target_branch')}",
            "title": j.get("title", "")[:80], "verdict": j.get("verdict"), "requested_by": who,
            "priority": j.get("priority")}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="review_queue.py")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def req_opts(p):
        p.add_argument("--requester", default="")
        p.add_argument("--requester-id", default="")
        p.add_argument("--source", default="manual")
        p.add_argument("--group", default="")
        p.add_argument("--group-type", default="")
        p.add_argument("--priority", type=int, default=PRIORITY_DEFAULT)
        p.add_argument("--note", default="")

    p = sub.add_parser("add")
    p.add_argument("--project", required=True)
    p.add_argument("--iid", type=int, required=True)
    req_opts(p)
    p = sub.add_parser("add-text")
    p.add_argument("text")
    p.add_argument("--default-project", default="")
    req_opts(p)
    sub.add_parser("pending")
    p = sub.add_parser("next")
    p.add_argument("--worker", default="")
    p = sub.add_parser("list")
    p.add_argument("--status", default="")
    p.add_argument("--brief", action="store_true")
    p = sub.add_parser("show")
    p.add_argument("job_id")
    p = sub.add_parser("set")
    p.add_argument("job_id")
    p.add_argument("--status", required=True)
    p.add_argument("--note")
    p = sub.add_parser("cancel")
    p.add_argument("job_id")
    a = ap.parse_args(argv)

    try:
        if a.cmd == "add":
            out = add_job(a.project, a.iid, requester=a.requester, requester_id=a.requester_id,
                          source=a.source, group=a.group, group_type=a.group_type,
                          priority=a.priority, note=a.note)
        elif a.cmd == "add-text":
            refs = rc.parse_mr_refs(a.text, a.default_project)
            if not refs:
                return rc.pc.emit({"error": True, "message": "no MR reference found in text"})
            out = {"added": [add_job(r["project"], r["iid"], requester=a.requester,
                                     requester_id=a.requester_id, source=a.source, group=a.group,
                                     group_type=a.group_type, priority=a.priority,
                                     note=a.note or a.text)["job"]["id"] for r in refs]}
        elif a.cmd == "pending":
            out = pending()
            rc.pc.emit(out)
            return 0 if out["can_start"] else 1
        elif a.cmd == "next":
            job = claim_next(a.worker)
            if not job:
                rc.pc.emit({"job": None, "message": "no job to start (queue empty or at capacity)"})
                return 3
            out = {"job": job}
        elif a.cmd == "list":
            wanted = {s for s in a.status.split(",") if s}
            jobs = [j for j in rc.all_jobs() if not wanted or j.get("status") in wanted]
            out = {"count": len(jobs), "jobs": [_brief(j) for j in jobs] if a.brief else jobs}
        elif a.cmd == "show":
            out = rc.load_job(a.job_id) or {"error": True, "message": f"no job {a.job_id}"}
        elif a.cmd == "set":
            out = set_status(a.job_id, a.status, note=a.note)
        elif a.cmd == "cancel":
            out = set_status(a.job_id, "cancelled")
    except (ValueError, RuntimeError, TimeoutError) as e:
        out = {"error": True, "message": str(e)}
    return rc.pc.emit(out)


if __name__ == "__main__":
    sys.exit(main())
