#!/usr/bin/env python3
"""Coordinator facade (checklist F.7, design §5.2 / §11): run ALL queued work with shared limits and
show the progress board (agent_skill_toolkit.workflows.coordinator).

    python coordinator.py work  [--parallel 2] [--once] [--interval 30] [--kinds review_mr,verify]
    python coordinator.py board [--json] [--hours 24]        # progress board (open + closed in last N h)
    python coordinator.py accept <task_id> [--lane vua]      # accept an aTask proposal (atask_intake)

Kinds run: review_mr (mr-review/review_flow) · verify (verify.py) · atask_intake (proposal).
`--parallel` is a GLOBAL cap: tasks already running in other `work` loops count against it.
Two tasks with the same `resource` (repo@branch) never run at once; a task waits for its
dependencies (store `depends_on`), and is blocked if one of them failed / was cancelled.
"""

import argparse
import json
import os
import runpy
import socket
import sys
from pathlib import Path

runpy.run_path(str(Path(__file__).resolve().parents[2] / "_runtime" / "bootstrap.py"))["activate"]()
from agent_skill_toolkit.core import store as store_mod  # noqa: E402
from agent_skill_toolkit.workflows import coordinator as C  # noqa: E402
from agent_skill_toolkit.workflows.intake import atask_flow  # noqa: E402

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "..", "dev-automation", "tools"))
import probe_common as pc  # noqa: E402,F401  (UTF-8 stdout on Windows)


def load_registry(kinds: list = None) -> tuple:
    """(registry, hooks, warnings) from every workflow that can be loaded on this machine."""
    reg, hooks, warn = dict(atask_flow.REGISTRY), [], []
    try:
        import verify
        reg.update(verify.REGISTRY)
    except Exception as e:  # noqa: BLE001
        warn.append(f"verify_ladder unavailable: {type(e).__name__}: {e}")
    try:
        sys.path.insert(0, os.path.join(_HERE, "..", "..", "mr-review", "tools"))
        import review_flow
        reg.update(review_flow.REGISTRY)
        hooks.append(review_flow.reconcile)
    except Exception as e:  # noqa: BLE001
        warn.append(f"review_mr unavailable: {type(e).__name__}: {e}")
    if kinds:
        unknown = [k for k in kinds if k not in reg]
        if unknown:
            raise ValueError(f"unknown / unavailable kinds {unknown} (have {sorted(reg)})")
        reg = {k: v for k, v in reg.items() if k in kinds}
    return reg, hooks, warn


def accept(task_id: str, lane: str = "") -> dict:
    with store_mod.Store() as st:
        t = st.get(task_id)
        if not t or t["kind"] != atask_flow.KIND:
            raise ValueError(f"{task_id} is not an {atask_flow.KIND} task")
        if t["status"] != "blocked":
            raise ValueError(f"{task_id} is {t['status']} — only a blocked proposal can be accepted")
        tri = next((c["data"] for c in reversed(st.checkpoints(task_id, valid_only=True))
                    if c["step"] == "triage"), {})
        lane = lane or tri.get("lane") or ""
        if lane and lane not in ("nho", "vua", "lon", "de_xuat"):
            raise ValueError("lane must be nho|vua|lon|de_xuat")
        ok = st.resolve_blocked(task_id, "done", by="human", note=json.dumps(
            {"accepted": True, "kind": tri.get("kind"), "lane": lane}, ensure_ascii=False))
    return {"task_id": task_id, "accepted": ok, "kind": tri.get("kind"), "lane": lane}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="coordinator.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("work")
    p.add_argument("--parallel", type=int, default=int(os.environ.get("TOOLKIT_MAX_PARALLEL") or 2))
    p.add_argument("--once", action="store_true")
    p.add_argument("--interval", type=float, default=30)
    p.add_argument("--kinds", default="")
    p.add_argument("--owner", default=f"coordinator@{socket.gethostname()}:{os.getpid()}")
    p = sub.add_parser("board")
    p.add_argument("--json", action="store_true")
    p.add_argument("--hours", type=float, default=24)
    p = sub.add_parser("accept")
    p.add_argument("task_id")
    p.add_argument("--lane", default="")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "work":
            reg, hooks, warn = load_registry([k for k in a.kinds.split(",") if k])
            for w in warn:
                print(f"[coordinator] {w}", file=sys.stderr)

            def show(tick):
                if tick.get("blocked_orphans"):
                    print(f"[coordinator] blocked (dependency failed): {tick['blocked_orphans']}", file=sys.stderr)
            out = C.Coordinator(reg, a.parallel, a.owner, hooks).run(once=a.once, interval=a.interval, on_tick=show)
            out["warnings"] = warn
        elif a.cmd == "board":
            with store_mod.Store() as st:
                b = C.board(st, a.hours)
            if not a.json:
                print(b["text"])
                return 0
            out = b
        else:
            out = accept(a.task_id, a.lane)
    except (ValueError, PermissionError, RuntimeError) as e:
        out = {"error": True, "message": str(e)}
    return pc.emit(out)


if __name__ == "__main__":
    sys.exit(main())
