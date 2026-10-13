#!/usr/bin/env python3
"""Verify ladder facade (checklist F.2) — run a repo's build → test → review rungs with an
automatic fix loop, durable across quota stops / crashes (agent_skill_toolkit.workflows.verify).

    python verify.py plan  --repo DIR [--base origin/develop] [--spec FILE]      # show the rungs
    python verify.py start --repo DIR [--base B] [--spec FILE] [--fixer-role implement]
                           [--allow-default-branch]                             # enqueue a verify task
    python verify.py work  [--once] [--interval 30] [--owner NAME]               # run queued verify tasks
    python verify.py status [<task_id>]
    python verify.py report <task_id> [--out FILE]                              # AC -> evidence, checked / not checked

The spec is `verify:` in the repo's .agent/project.yml, read from the BASE ref (the branch under
test cannot change its own commands); `--spec` uses an explicit trusted file instead.
The fixer commits WIP commits on the CURRENT branch — use a feature branch or worktree.
"""

import argparse
import json
import os
import runpy
import socket
import sys
import time
from pathlib import Path

runpy.run_path(str(Path(__file__).resolve().parents[2] / "_runtime" / "bootstrap.py"))["activate"]()
from agent_skill_toolkit.core import store as store_mod  # noqa: E402
from agent_skill_toolkit.workflows import engine as E  # noqa: E402
from agent_skill_toolkit.workflows.verify import ladder as L  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "dev-automation", "tools"))
import probe_common as pc  # noqa: E402,F401  (UTF-8 stdout on Windows)

REGISTRY = {L.KIND: lambda task: L.steps_for(task["data"])}


def parse_acs(items: list) -> list:
    """["AC1: trả .xlsx", "403 khi không quyền"] -> [{"id": "AC1", ...}, {"id": "AC2", ...}]"""
    import re
    out, used = [], set()
    for raw in items:
        text = str(raw).strip().lstrip("-•* ").strip()
        if not text:
            continue
        m = re.match(r"(?i)^AC[-_ ]?0*(\d{1,3})\s*[.:)\-]\s*(.*)$", text)
        if m:
            aid, text = f"AC{int(m.group(1))}", m.group(2).strip()
        else:
            n = 1
            while f"AC{n}" in used:
                n += 1
            aid = f"AC{n}"
        if aid in used:
            raise ValueError(f"duplicate acceptance criterion id {aid}")
        used.add(aid)
        out.append({"id": aid, "text": text})
    return out


def start(repo: str, base: str, spec: str, fixer_role: str, allow_default: bool, acs: list = None) -> dict:
    repo = os.path.abspath(repo)
    br = L.guard_branch(repo, allow_default)
    s = L.load_spec(repo, base, spec)                       # fail fast on a bad spec
    resolved = L.resolve_base(repo, base or s["base"] or "")   # fail fast on a missing / unrelated base
    try:
        L.require_clean(repo, "at start")
    except E.NeedsHuman as e:
        raise PermissionError(str(e))
    with store_mod.Store() as st:
        t = st.add(L.KIND, f"{repo}@{br}", title=f"verify {os.path.basename(repo)}@{br}",
                   data={"repo": repo, "branch": br, "base": resolved, "allow_default": bool(allow_default),
                         "acs": acs or [],
                         "spec_file": os.path.abspath(spec) if spec else "", "fixer_role": fixer_role},
                   max_attempts=3)
    return {"task_id": t["id"], "duplicate": t["duplicate"], "branch": br,
            "rungs": [r["name"] for r in s["rungs"]]}


def work(owner: str, once: bool, interval: int) -> dict:
    ran = []
    with store_mod.Store() as st:
        while True:
            res = E.work_once(st, owner, REGISTRY, lease_s=900)
            if res.get("status") != "idle":
                ran.append(res)
                print(json.dumps(res, ensure_ascii=False, default=str)[:2000], file=sys.stderr, flush=True)
                continue
            if once:
                return {"ran": ran}
            time.sleep(interval)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="verify.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("plan", "start"):
        p = sub.add_parser(name)
        p.add_argument("--repo", required=True)
        p.add_argument("--base", default="")
        p.add_argument("--spec", default="")
        if name == "start":
            p.add_argument("--fixer-role", default="implement")
            p.add_argument("--allow-default-branch", action="store_true")
            p.add_argument("--ac", action="append", default=[], help="acceptance criterion (repeatable), e.g. 'AC1: ...'")
            p.add_argument("--ac-file", default="", help="one acceptance criterion per line")
    p = sub.add_parser("work")
    p.add_argument("--once", action="store_true")
    p.add_argument("--interval", type=int, default=30)
    p.add_argument("--owner", default=f"verify@{socket.gethostname()}:{os.getpid()}")
    sub.add_parser("status").add_argument("task_id", nargs="?", default="")
    p = sub.add_parser("report")
    p.add_argument("task_id")
    p.add_argument("--out", default="", help="also write the markdown report to this file")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "plan":
            s = L.load_spec(os.path.abspath(a.repo), a.base, a.spec)
            out = {"base": a.base or s["base"], "rungs": s["rungs"], "budgets": s["budgets"]}
        elif a.cmd == "start":
            items = list(a.ac)
            if a.ac_file:
                with open(a.ac_file, encoding="utf-8") as f:
                    items += f.read().splitlines()
            out = start(a.repo, a.base, a.spec, a.fixer_role, a.allow_default_branch, parse_acs(items))
        elif a.cmd == "report":
            with store_mod.Store() as st:
                out = L.evidence_report(st, a.task_id)
            if a.out:
                with open(a.out, "w", encoding="utf-8") as f:
                    f.write(out["markdown"] + "\n")
        elif a.cmd == "work":
            out = work(a.owner, a.once, a.interval)
        else:
            with store_mod.Store() as st:
                if a.task_id:
                    t = st.get(a.task_id)
                    out = {"task": t, "checkpoints": [{k: c[k] for k in ("step", "valid", "input_key", "data")}
                                                      for c in st.checkpoints(a.task_id)]}
                else:
                    out = [{k: t[k] for k in ("id", "ref", "status", "step", "attempts", "last_error")}
                           for t in st.list(store_mod.OPEN, L.KIND, 50)]
    except (ValueError, PermissionError, RuntimeError) as e:
        out = {"error": True, "message": str(e)}
    return pc.emit(out)


if __name__ == "__main__":
    sys.exit(main())
