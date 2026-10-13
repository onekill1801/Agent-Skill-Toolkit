#!/usr/bin/env python3
"""Proposal lane + weekly proactive scan (checklist F.9, design §12.4 / §12.5).

    python proposals.py scan --repo DIR [--enqueue] [--days 90] [--out FILE]   # "Đề xuất tuần" (no tokens)
    python proposals.py new  --repo DIR --question "Nâng Spring Boot lên 3.x?" [--criteria "..."]
    python proposals.py go <task_id>                       # [Làm]: start a scan candidate (debate -> ADR)
    python proposals.py decide <task_id> accept|reject --reason "..."
    python proposals.py list

Scan candidates (source "schedule") only WAIT; nothing runs until you `go`. The debate itself runs in
`coordinator.py work` (kind "proposal"; paused automatically at >= 80% usage). A rejected candidate
is never proposed again. Accepting records the decision; the ADR file is printed for .agent/decisions/.
"""

import argparse
import json
import os
import runpy
import sys
from pathlib import Path

runpy.run_path(str(Path(__file__).resolve().parents[2] / "_runtime" / "bootstrap.py"))["activate"]()
from agent_skill_toolkit.core import paths  # noqa: E402
from agent_skill_toolkit.core import store as store_mod  # noqa: E402
from agent_skill_toolkit.workflows.intake import gate  # noqa: E402
from agent_skill_toolkit.workflows.proposal import flow, scan  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "dev-automation", "tools"))
import probe_common as pc  # noqa: E402,F401  (UTF-8 stdout on Windows)

REJECTED = "rejected:"            # setting key prefix: one row per rejected candidate ref (never re-proposed)


def _ref(repo: str, key: str) -> str:
    return f"scan:{os.path.normcase(os.path.realpath(repo))}:{key}"


def cmd_scan(repo: str, enqueue: bool, days: int, out: str) -> dict:
    repo = os.path.abspath(repo)
    if not os.path.isdir(repo):
        raise ValueError(f"no such repo: {repo}")
    with store_mod.Store() as st:
        def rejected(f):
            return any(st.get_setting(REJECTED + _ref(repo, k)) is not None for k in [f["key"], *f.get("aliases", [])])
        findings = [f for f in scan.run(repo, days=days) if not rejected(f)]
        if enqueue:
            for f in findings:
                if f["kind"] == "error":
                    continue
                keys = [REJECTED + _ref(repo, k) for k in [f["key"], *f.get("aliases", [])]]
                t = gate.submit(st, "schedule", flow.KIND, _ref(repo, f["key"]), title=f["title"][:200],
                                data={"question": f["title"], "repo": repo, "key": f["key"],
                                      "aliases": f.get("aliases", []), "evidence": f.get("evidence") or {},
                                      "go": False, "kind_hint": f["kind"]},
                                priority=8, unless_settings=keys)   # re-checked atomically with the insert
                f["task_id"] = t.get("id")
        findings = [f for f in findings if not enqueue or f.get("task_id") or f["kind"] == "error"]
    md = scan.digest(os.path.basename(repo), findings)
    path = out or paths.state_dir("proposals", f"week-{scan.week_label()}-{os.path.basename(repo)}.md")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(md)
    return {"repo": repo, "findings": findings, "digest": path, "enqueued": enqueue}


def cmd_new(repo: str, question: str, criteria: str) -> dict:
    if not question.strip():
        raise ValueError("--question is required")
    with store_mod.Store() as st:
        t = gate.submit(st, "self", flow.KIND, f"ask:{os.path.normcase(os.path.realpath(repo))}:{question.strip()[:120]}",
                        title=question.strip()[:200],
                        data={"question": question.strip(), "criteria": criteria, "repo": os.path.abspath(repo),
                              "go": True}, priority=6)
    return {"task_id": t["id"], "duplicate": t["duplicate"]}


def _task(st, task_id: str) -> dict:
    t = st.get(task_id)
    if not t or t["kind"] != flow.KIND:
        raise ValueError(f"{task_id} is not a proposal task")
    return t


def cmd_go(task_id: str) -> dict:
    with store_mod.Store() as st:
        t = _task(st, task_id)
        if t["status"] != "blocked" or not str(t.get("last_error") or "").startswith(flow.WAIT_GO):
            raise ValueError(f"{task_id} is not a candidate waiting for [Làm] (status {t['status']})")
        if not st.patch_data(task_id, ("blocked",), go=True):
            raise ValueError(f"{task_id} changed meanwhile — look again")
        st.unblock(task_id, by="human:go")
    return {"task_id": task_id, "started": True, "next": "coordinator.py work runs the debate"}


def cmd_decide(task_id: str, decision: str, reason: str) -> dict:
    import hashlib
    if decision not in ("accept", "reject"):
        raise ValueError("decision must be accept or reject")
    if decision == "reject" and not reason.strip():
        raise ValueError("--reason is required to reject (it is kept so the idea is not proposed again)")
    with store_mod.Store() as st:
        t = _task(st, task_id)
        err = str(t.get("last_error") or "")
        allowed = (flow.WAIT_DECISION,) if decision == "accept" else (flow.WAIT_DECISION, flow.WAIT_GO)
        prefix = next((p for p in allowed if err.startswith(p)), None)
        if t["status"] != "blocked" or not prefix:
            raise ValueError(f"{task_id} is not waiting for this decision (status {t['status']})")
        note = json.dumps({"decision": decision, "reason": reason}, ensure_ascii=False)
        if decision == "reject":
            d = t["data"]
            keys = [REJECTED + t["ref"]]
            if d.get("key"):
                keys += [REJECTED + _ref(d.get("repo") or "", k) for k in [d["key"], *d.get("aliases", [])]]
            ok = st.reject_and_remember(task_id, sorted(set(keys)), t["updated"], prefix, note=note)
            if not ok:
                raise ValueError(f"{task_id} changed while deciding — look again")
            return {"task_id": task_id, "decision": "reject"}
        # accept: the ADR you read must be the one the valid checkpoint describes (Codex F.9 #2)
        cp = next((c for c in reversed(st.checkpoints(task_id, valid_only=True)) if c["step"] == "adr"), None)
        if not cp:
            raise ValueError(f"{task_id} has no valid ADR — it must be regenerated (coordinator.py work)")
        path = cp["data"].get("path") or ""
        try:
            with open(path, encoding="utf-8") as f:
                body = f.read()
        except OSError:
            raise ValueError(f"ADR file {path} is missing — regenerate it")
        if hashlib.sha256(body.encode("utf-8")).hexdigest() != cp["data"].get("sha256"):
            raise ValueError(f"ADR file {path} was changed after it was generated — regenerate or restore it")
        ok = st.resolve_blocked(task_id, "done", by="human", note=json.dumps(
            {"decision": "accept", "reason": reason, "adr": path, "sha256": cp["data"]["sha256"]}, ensure_ascii=False),
            if_updated=t["updated"], if_error_prefix=prefix, if_checkpoint_valid=cp["id"])
        if not ok:
            raise ValueError(f"{task_id} changed while deciding — look again")
    return {"task_id": task_id, "decision": "accept", "adr": path,
            "next": "copy the ADR into the repo's .agent/decisions/ and open a task (lane vua / lon) for it"}


def cmd_list() -> list:
    with store_mod.Store() as st:
        return [{k: t.get(k) for k in ("id", "status", "title", "last_error")}
                for t in st.list(kind=flow.KIND, limit=100)]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="proposals.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("scan")
    p.add_argument("--repo", required=True)
    p.add_argument("--enqueue", action="store_true")
    p.add_argument("--days", type=int, default=90)
    p.add_argument("--out", default="")
    p = sub.add_parser("new")
    p.add_argument("--repo", required=True)
    p.add_argument("--question", required=True)
    p.add_argument("--criteria", default="")
    sub.add_parser("go").add_argument("task_id")
    p = sub.add_parser("decide")
    p.add_argument("task_id")
    p.add_argument("decision", choices=["accept", "reject"])
    p.add_argument("--reason", default="")
    sub.add_parser("list")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "scan":
            out = cmd_scan(a.repo, a.enqueue, a.days, a.out)
        elif a.cmd == "new":
            out = cmd_new(a.repo, a.question, a.criteria)
        elif a.cmd == "go":
            out = cmd_go(a.task_id)
        elif a.cmd == "decide":
            out = cmd_decide(a.task_id, a.decision, a.reason)
        else:
            out = cmd_list()
    except (ValueError, PermissionError) as e:
        out = {"error": True, "message": str(e)}
    return pc.emit(out)


if __name__ == "__main__":
    sys.exit(main())
