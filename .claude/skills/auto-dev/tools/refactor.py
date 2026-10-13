#!/usr/bin/env python3
"""Large-refactor lane tools (checklist F.10a, design §12.3): survey + architecture ratchet.

    python refactor.py survey  --repo DIR [--depth 1] [--out FILE]      # read-only map (no LLM)
    python refactor.py ratchet check   --repo DIR [--base REF]          # new violations vs the baseline
    python refactor.py ratchet init    --repo DIR                       # first baseline = today's violations
    python refactor.py ratchet tighten --repo DIR                       # shrink the baseline (never grows)
    python refactor.py program new --repo DIR --goal "..." --invariants "..." --slices FILE.json [--base develop]
    python refactor.py program approve <program_id>                     # YOU approve the plan -> slices start
    python refactor.py program status <program_id>

slices.json: [{"id": "s1", "title": "...", "desc": "...", "acs": ["..."], "depends_on": []}, ...]
Each slice: own worktree + branch refactor/<goal>-<id>, the implement agent codes it, the verify ladder
proves it (`coordinator.py work` runs both); merging = your MR.

Rules: .agent/architecture.json, baseline: .agent/ratchet-baseline.json (commit both). In the verify
ladder use a rung `kind: ratchet` — it reads both from the BASE ref. `init` / `tighten` only write
the baseline file in your working tree; you review and commit it.
"""

import argparse
import os
import runpy
import subprocess
import sys
from pathlib import Path

runpy.run_path(str(Path(__file__).resolve().parents[2] / "_runtime" / "bootstrap.py"))["activate"]()
from agent_skill_toolkit.core import paths  # noqa: E402
from agent_skill_toolkit.core import store as store_mod  # noqa: E402
from agent_skill_toolkit.workflows.refactor import program as P  # noqa: E402
from agent_skill_toolkit.workflows.refactor import ratchet as R  # noqa: E402
from agent_skill_toolkit.workflows.refactor import survey  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "dev-automation", "tools"))
import probe_common as pc  # noqa: E402,F401  (UTF-8 stdout on Windows)


def _file(repo: str, rel: str, base: str = "") -> str:
    if base:
        p = subprocess.run(["git", "-C", repo, "show", f"{base}:{rel}"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=60)
        if p.returncode != 0:
            raise ValueError(f"{rel} not found at {base}")
        return p.stdout
    path = os.path.join(repo, rel)
    if not os.path.isfile(path):
        return ""
    with open(path, encoding="utf-8") as f:
        return f.read()


def ratchet(action: str, repo: str, base: str = "") -> dict:
    repo = os.path.abspath(repo)
    text = _file(repo, R.RULES_FILE, base)
    if not text:
        raise ValueError(f"{R.RULES_FILE} not found — write the rules first (see refactor.py --help)")
    R.parse_rules(text)                                  # validate before the isolated scan
    res = R.scan_isolated(repo, text)
    if res["unscanned"]:
        raise ValueError("covered files could not be scanned: " + "; ".join(
            f"{u['file']} ({u['why']})" for u in res["unscanned"][:5]))
    now = res["violations"]
    path = os.path.join(repo, R.BASELINE_FILE)
    have = R.parse_baseline(_file(repo, R.BASELINE_FILE, base))
    if action == "init":
        if os.path.exists(path):                         # even an EMPTY baseline is never re-initialised
            raise ValueError(f"{R.BASELINE_FILE} already exists — use `tighten` (the baseline only shrinks)")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(R.baseline_doc(now))
        return {"baseline": path, "count": sum(now.values()), "by_rule": R.by_rule(now), "next": "review + commit it"}
    cmp = R.compare(R.normalize_baseline(have), now)
    if action == "tighten":
        # renames since the baseline was last committed carry their entries to the new path (Codex F.10a R2)
        ref = subprocess.run(["git", "-C", repo, "log", "-1", "--format=%H", "--", R.BASELINE_FILE],
                             capture_output=True, text=True, timeout=60).stdout.strip()
        renames = {}
        if ref:
            diff = subprocess.run(["git", "-C", repo, "diff", "--name-status", "-M", ref], capture_output=True,
                                  text=True, encoding="utf-8", errors="replace", timeout=120).stdout
            for ln in diff.splitlines():
                parts = ln.split("\t")
                if len(parts) == 3 and parts[0].startswith("R"):
                    renames[parts[1]] = parts[2]
        have = R.rename_keys(R.normalize_baseline(have), renames)
        cmp = R.compare(have, now)
        if cmp["new"]:
            raise ValueError(f"{len(cmp['new'])} new violation(s) beyond the baseline — fix them first: "
                             + "; ".join(list(cmp["new"])[:5]))
        shrunk = {k: min(n, now.get(k, 0)) for k, n in have.items() if now.get(k, 0)}
        with open(path, "w", encoding="utf-8") as f:
            f.write(R.baseline_doc(shrunk))
        return {"baseline": path, "was": cmp["baseline"], "now": sum(shrunk.values()), "next": "review + commit it"}
    return {"passed": cmp["passed"], "baseline": cmp["baseline"], "now": cmp["now"],
            "new": cmp["new"], "fixed": len(cmp["fixed"]), "by_rule": R.by_rule(now)}


def program(action: str, a) -> dict:
    import json
    with store_mod.Store() as st:
        if action == "new":
            with open(a.slices, encoding="utf-8") as f:
                slices = json.load(f)
            return P.create(st, a.repo, a.goal, a.invariants, slices, a.base, a.spec)
        t = st.get(a.program_id)
        if not t or t["kind"] != P.PROGRAM:
            raise ValueError(f"{a.program_id} is not a refactor program")
        if action == "approve":
            if t["status"] != "blocked" or not str(t.get("last_error") or "").startswith(P.WAIT_PLAN):
                raise ValueError(f"{a.program_id} is not waiting for plan approval (status {t['status']})")
            if not st.patch_data(a.program_id, ("blocked",), approved=True):
                raise ValueError("the program changed meanwhile — look again")
            st.unblock(a.program_id, by="human:approve")
            return {"program": a.program_id, "approved": True, "next": "coordinator.py work"}
        kids = st.children(a.program_id)
        done = sum(1 for k in kids if k["status"] == "done")
        return {"program": a.program_id, "status": t["status"], "progress": f"{done}/{len(kids)}",
                "tasks": [{k2: k.get(k2) for k2 in ("id", "kind", "title", "status", "last_error")} for k in kids]}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="refactor.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("survey")
    p.add_argument("--repo", required=True)
    p.add_argument("--depth", type=int, default=1)
    p.add_argument("--out", default="")
    p = sub.add_parser("program")
    p.add_argument("action", choices=["new", "approve", "status"])
    p.add_argument("program_id", nargs="?", default="")
    p.add_argument("--repo", default="")
    p.add_argument("--goal", default="")
    p.add_argument("--invariants", default="")
    p.add_argument("--slices", default="")
    p.add_argument("--base", default="")
    p.add_argument("--spec", default="", help="trusted verify spec file (else .agent/project.yml on the base)")
    p = sub.add_parser("ratchet")
    p.add_argument("action", choices=["check", "init", "tighten"])
    p.add_argument("--repo", required=True)
    p.add_argument("--base", default="", help="check: read rules + baseline from this ref")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "survey":
            s = survey.run(os.path.abspath(a.repo), a.depth)
            path = a.out or paths.state_dir("refactor", f"survey-{os.path.basename(os.path.abspath(a.repo))}.md")
            with open(path, "w", encoding="utf-8") as f:
                f.write(survey.markdown(s))
            out = {**s, "report": path}
        elif a.cmd == "program":
            if a.action == "new" and not (a.repo and a.goal and a.slices):
                raise ValueError("program new needs --repo, --goal and --slices")
            if a.action != "new" and not a.program_id:
                raise ValueError("program approve/status needs the program id")
            out = program(a.action, a)
        else:
            if a.action != "check" and a.base:
                raise ValueError("--base only applies to `check`")
            out = ratchet(a.action, a.repo, a.base)
    except (ValueError, RuntimeError, TimeoutError, PermissionError, OSError) as e:
        out = {"error": True, "message": str(e)}
    rc = pc.emit(out)
    return 1 if out.get("passed") is False else rc      # a failed check fails the shell step (Codex F.10a #7)


if __name__ == "__main__":
    sys.exit(main())
