#!/usr/bin/env python3
"""Facade for the durable task store (agent_skill_toolkit.core.store) — inspect / manage tasks.

State is local (temp/toolkit.db, or $TOOLKIT_STORE); nothing here talks to an outside system.

    python taskstore.py summary                       # counts per status + next quota resume
    python taskstore.py list [--status queued,paused_quota] [--kind review_mr] [--limit 50]
    python taskstore.py show <task_id>                # task + checkpoints + handoff + last events
    python taskstore.py events [<task_id>] [--limit 50]
    python taskstore.py add <kind> <ref> [--title T] [--priority 5] [--data '{"k":1}']
    python taskstore.py unblock|cancel <task_id>      # human actions
"""

import argparse
import json
import runpy
import sys
from pathlib import Path

runpy.run_path(str(Path(__file__).resolve().parents[2] / "_runtime" / "bootstrap.py"))["activate"]()
from agent_skill_toolkit.core import store as store_mod  # noqa: E402
from agent_skill_toolkit.workflows.execution import handoff  # noqa: E402

import probe_common as pc  # noqa: E402,F401  (UTF-8 stdout on Windows)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="taskstore.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("summary")
    p = sub.add_parser("list")
    p.add_argument("--status", default="")
    p.add_argument("--kind", default="")
    p.add_argument("--limit", type=int, default=50)
    p = sub.add_parser("show")
    p.add_argument("task_id")
    p = sub.add_parser("events")
    p.add_argument("task_id", nargs="?", default="")
    p.add_argument("--limit", type=int, default=50)
    p = sub.add_parser("add")
    p.add_argument("kind")
    p.add_argument("ref")
    p.add_argument("--title", default="")
    p.add_argument("--priority", type=int, default=5)
    p.add_argument("--data", default="{}")
    for name in ("unblock", "cancel"):
        sub.add_parser(name).add_argument("task_id")
    a = ap.parse_args(argv)
    with store_mod.Store() as st:
        if a.cmd == "summary":
            out = st.summary()
        elif a.cmd == "list":
            status = tuple(s for s in a.status.split(",") if s)
            out = [{k: t[k] for k in ("id", "kind", "ref", "title", "status", "step", "attempts", "resume_at",
                                      "last_error", "updated")} for t in st.list(status, a.kind, a.limit)]
        elif a.cmd == "show":
            t = st.get(a.task_id)
            out = {"task": t, "checkpoints": st.checkpoints(a.task_id), "handoff": handoff.read(a.task_id),
                   "events": st.events(a.task_id, 20)} if t else {"error": True, "message": f"no task {a.task_id}"}
        elif a.cmd == "events":
            out = st.events(a.task_id, a.limit)
        elif a.cmd == "add":
            try:
                data = json.loads(a.data)
            except ValueError as e:
                return pc.emit({"error": True, "message": f"--data is not JSON: {e}"})
            out = st.add(a.kind, a.ref, title=a.title, data=data, priority=a.priority)
        elif a.cmd == "unblock":
            out = {"task_id": a.task_id, "unblocked": st.unblock(a.task_id, by="cli")}
        else:
            out = {"task_id": a.task_id, "cancelled": st.cancel(a.task_id, by="cli")}
    return pc.emit(out)


if __name__ == "__main__":
    sys.exit(main())
