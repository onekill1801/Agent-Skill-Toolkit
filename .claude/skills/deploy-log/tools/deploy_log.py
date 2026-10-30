#!/usr/bin/env python3
"""deploy-log — log a prod deploy: create the aTask record (status ready_to_deploy) and
write the matching row into the "Kyta Deploy" Google Sheet (current month's tab).

One deploy needs only: --service and --desc. Everything else is derived:
  task name / Message Group = "[<env> - <service> - <desc>]"
  task link (sheet col P)    = https://etask.sandbox.kytaplatform.com/task/<KCR code>
  date (col N)               = today, DD/MM/YYYY
  confirmer (col H)          = chungtv8 (override with --confirm)

Default is a DRY RUN (prints the plan, writes nothing). Pass --commit to create the
task and write the sheet row.

  python deploy_log.py --service eDlm-ext --desc "Update: add perm change flow"
  python deploy_log.py --service eDlm-ext --desc "..." --commit
"""

import argparse
import datetime as dt
import json
import os
import re
import runpy
import sys
from pathlib import Path

# ── activate runtime (gsheets, gitlab) + aTask skill client ──────────────────
_HERE = Path(__file__).resolve()
_TOOLKIT = _HERE.parents[4]                      # .../Agent-Skill-Toolkit
runpy.run_path(str(_TOOLKIT / ".claude" / "skills" / "_runtime" / "bootstrap.py"))["activate"]()
sys.path.insert(0, str(_TOOLKIT / ".claude" / "skills" / "atask-automation" / "tools"))

from agent_skill_toolkit.integrations import gsheets, gitlab   # noqa: E402
from agent_skill_toolkit.core import paths                      # noqa: E402
import client as atask                                          # noqa: E402  (aTask skill client)

# ── config (see work/memory/deploy-log-google-sheets.md) ─────────────────────
SHEET_ID = "13WC7LhSZ4w4-AEL-p0K562ASIzZSLAeaBcU9foEi6XU"
SHEET_GID = 533694387                 # the current monthly tab the user linked
LIST_ID = "00002vkIvo5FpbranqkzFg6kK"
STATUS_READY_TO_DEPLOY = "00002lKIpnoC0RwMOQpVtO35z"
TASK_LINK_BASE = "https://etask.sandbox.kytaplatform.com/task/"
DEFAULT_CONFIRM = "chungtv8"

# sheet columns filled when creating a record (1-indexed): B,C,F,H,N,P,Q
COL = {"env": 2, "service": 3, "desc": 6, "confirm": 8, "date": 14, "link": 16, "msg": 17}
LAST_COL_LETTER = "Q"


ALIAS_PATH = os.path.join(paths.work_dir(), "deploy-log-aliases.json")


class ResolveError(Exception):
    def __init__(self, service, registry_candidates, gitlab_candidates):
        self.service = service
        self.registry_candidates = registry_candidates
        self.gitlab_candidates = gitlab_candidates


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def load_registry() -> dict:
    try:
        d = json.load(open(os.path.join(paths.work_dir(), "projects.json"), encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return d.get("projects", d)


def load_aliases() -> dict:
    try:
        return json.load(open(ALIAS_PATH, encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_alias(service: str, gitlab_project_id, branch) -> None:
    a = load_aliases()
    a[service] = {"gitlab_project_id": gitlab_project_id, "branch": branch}
    with open(ALIAS_PATH, "w", encoding="utf-8") as f:
        json.dump(a, f, ensure_ascii=False, indent=2)


def _prod_branch(p: dict):
    envs = p.get("environments", {})
    if isinstance(envs, dict):
        for k, e in envs.items():
            if "prod" in k.lower() and isinstance(e, dict):
                b = (e.get("jenkins", {}) or {}).get("branch") or e.get("deployed_branch")
                if b:
                    return b
    return p.get("deployed_branch") or p.get("default_branch")


def resolve_service(service: str, override_gid=None, override_branch=None) -> tuple:
    """Return (gitlab_project_id, branch_or_None, source). Raise ResolveError when ambiguous/unknown.
    branch=None means: let GitLab use the repo's default branch."""
    if override_gid:
        return override_gid, override_branch, "override"
    aliases = load_aliases()
    if service in aliases:
        a = aliases[service]
        return a.get("gitlab_project_id"), override_branch or a.get("branch"), "alias"
    reg = load_registry()
    ns = _norm(service)
    matches = []
    for name, p in reg.items():
        rp = p.get("repo_path", "") or ""
        base = rp.split("/")[-1] if rp else ""
        if _norm(name) == ns or (base and _norm(base) == ns):
            matches.append((name, p))
    if len(matches) == 1:
        name, p = matches[0]
        return p.get("gitlab_project_id"), override_branch or _prod_branch(p), f"registry:{name}"
    # unresolved → gather candidates to help the caller
    reg_cands = [f"{n} (gid={p.get('gitlab_project_id')}, repo={p.get('repo_path')})"
                 for n, p in reg.items() if ns and (ns in _norm(n) or _norm(n) in ns)][:8]
    gl_cands = []
    try:
        res = gitlab.search_projects(service)
        if isinstance(res, list):
            gl_cands = [f"{x.get('path_with_namespace')} (gid={x.get('id')})" for x in res][:8]
    except Exception:
        pass
    raise ResolveError(service, reg_cands, gl_cands)


def derive_desc(service: str, override_gid, override_branch) -> tuple:
    """Return (desc, source_info_dict). Raises ResolveError / RuntimeError on failure."""
    gid, branch, source = resolve_service(service, override_gid, override_branch)
    if not gid:
        raise RuntimeError(f"no gitlab_project_id for service {service!r} (source={source})")
    commit = gitlab.Client(gid).latest_commit(branch)
    if isinstance(commit, dict) and commit.get("error"):
        raise RuntimeError(f"gitlab commit read failed (gid={gid}, branch={branch}): "
                           f"{commit.get('status')} {str(commit.get('message'))[:160]}")
    desc = (commit.get("title") or commit.get("message", "").splitlines()[0]).strip()
    return desc, {"gitlab_project_id": gid, "branch": branch or "(default)",
                  "commit": commit.get("short_id") or (commit.get("id") or "")[:8],
                  "committed_at": commit.get("created_at", ""), "source": source}


def month_tab(d: dt.date) -> str:
    return f"Tháng {d.month:02d}-{d.year}"


def _a1(tab: str, rng: str) -> str:
    return f"'{tab}'!{rng}"


def next_row(tab: str) -> int:
    """First empty record row: after the last row whose Service (col C) is filled (min row 3)."""
    col_c = gsheets.read(SHEET_ID, _a1(tab, "C3:C"))
    last_filled = 0
    for i, row in enumerate(col_c):
        if row and (row[0] or "").strip():
            last_filled = i + 1            # 1-based within C3:C window
    return 3 + last_filled                 # row after the last filled one


def build_row(service: str, desc: str, env: str, confirm: str, link: str, date_str: str) -> list:
    msg = f"[{env} - {service} - {desc}]"
    row = [""] * COL["msg"]                 # A..Q
    row[COL["env"] - 1] = env
    row[COL["service"] - 1] = service
    row[COL["desc"] - 1] = desc
    row[COL["confirm"] - 1] = confirm
    row[COL["date"] - 1] = date_str
    row[COL["link"] - 1] = link
    row[COL["msg"] - 1] = msg
    return row


def _payload(result) -> dict:
    """aTask wraps the object in `content` (ai/execute) or `data` (REST); fall back to the result itself."""
    if not isinstance(result, dict):
        return {}
    for key in ("content", "data"):
        if isinstance(result.get(key), dict):
            return result[key]
    return result


def _task_id(result: dict) -> str:
    d = _payload(result)
    return d.get("id") or d.get("taskId") or ""


def create_task_ready(name: str, existing_id: str = None, set_status: bool = True) -> tuple:
    """Create the aTask task (or reuse existing_id), set it to ready_to_deploy, return (task_id, task_code, link)."""
    tid = existing_id
    if not tid:
        # create_task takes the status (group_task id) directly; a later update_task would need explicit
        # project membership on the AI channel (AgentWriteAuthorizer), which the UI does not require.
        res = atask.execute_tool("create_task", {"name": name, "list_task_id": LIST_ID,
                                                 "status": STATUS_READY_TO_DEPLOY})
        atask.check_error(res, "create_task")
        tid = _task_id(res)
        if not tid:
            raise RuntimeError(f"create_task returned no id: {json.dumps(res)[:300]}")
    elif set_status:
        upd = atask.execute_tool("update_task", {"task_id": tid, "status": STATUS_READY_TO_DEPLOY})
        atask.check_error(upd, "update_task(status)")
    detail = atask.api_get_user(f"/api/tasks/{tid}")
    code = _payload(detail).get("taskCode")
    link = (TASK_LINK_BASE + code) if code else ""
    return tid, code, link


def main():
    ap = argparse.ArgumentParser(description="Log a prod deploy to the Kyta Deploy sheet + aTask")
    ap.add_argument("--service", required=True, help="service / gitlab repo, e.g. eDlm-ext")
    ap.add_argument("--desc", default=None,
                    help="change description; if omitted, derived from the latest GitLab commit")
    ap.add_argument("--env", default="Prod")
    ap.add_argument("--confirm", default=DEFAULT_CONFIRM, help="confirmer (col H)")
    ap.add_argument("--date", default=None, help="DD/MM/YYYY (default: today)")
    ap.add_argument("--gitlab-id", default=None, help="override the GitLab project id for --service")
    ap.add_argument("--branch", default=None, help="override the branch to read the commit from")
    ap.add_argument("--save-alias", action="store_true",
                    help="remember --gitlab-id/--branch for this --service in deploy-log-aliases.json")
    ap.add_argument("--task-id", default=None,
                    help="reuse an already-created aTask task (resume after a partial run) instead of creating one")
    ap.add_argument("--skip-status", action="store_true",
                    help="do not set ready_to_deploy (PAT may lack edit rights on the list; set it in the UI)")
    ap.add_argument("--commit", action="store_true", help="actually create task + write row")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    today = dt.date.today()
    date_str = args.date or f"{today.day:02d}/{today.month:02d}/{today.year}"
    tab = month_tab(today)

    desc_source = None
    desc = args.desc
    if not desc:
        try:
            desc, desc_source = derive_desc(args.service, args.gitlab_id, args.branch)
        except ResolveError as e:
            print(f"[deploy-log] Không map được service {e.service!r} sang repo GitLab.\n"
                  f"  Ứng viên trong registry: {e.registry_candidates or '(không có)'}\n"
                  f"  Ứng viên GitLab search : {e.gitlab_candidates or '(không có)'}\n"
                  f"  → chạy lại với: --gitlab-id <id> [--branch <b>] --save-alias  (hoặc tự nhập --desc)",
                  file=sys.stderr)
            sys.exit(3)
        except RuntimeError as e:
            print(f"[deploy-log] {e}", file=sys.stderr)
            sys.exit(4)

    if args.save_alias and args.gitlab_id:
        save_alias(args.service, args.gitlab_id, args.branch)

    msg = f"[{args.env} - {args.service} - {desc}]"

    try:
        tab_title = gsheets.sheet_title(SHEET_ID, SHEET_GID)
    except Exception:
        tab_title = None
    # Prefer the gid-mapped tab if it matches this month; else the month-named tab.
    target_tab = tab if (tab_title is None or tab_title == tab) else tab
    row_idx = next_row(target_tab)

    plan = {
        "dry_run": not args.commit,
        "desc_source": desc_source,
        "aTask": {"list": LIST_ID, "name": msg, "status": "ready_to_deploy"},
        "sheet": {"tab": target_tab, "row": row_idx,
                  "cells": {"B": args.env, "C": args.service, "F": desc,
                            "H": args.confirm, "N": date_str, "P": "<task link after create>",
                            "Q": msg}},
    }

    if not args.commit:
        print(json.dumps(plan, ensure_ascii=False, indent=2) if args.json
              else _render(plan))
        return

    # --- writes ---
    tid, code, link = create_task_ready(msg, args.task_id, not args.skip_status)
    plan["aTask"].update({"id": tid, "code": code, "link": link})
    plan["sheet"]["cells"]["P"] = link
    row = build_row(args.service, desc, args.env, args.confirm, link, date_str)
    gsheets.update(SHEET_ID, _a1(target_tab, f"A{row_idx}:{LAST_COL_LETTER}{row_idx}"), [row])
    plan["committed"] = True
    print(json.dumps(plan, ensure_ascii=False, indent=2) if args.json else _render(plan))


def _render(p: dict) -> str:
    head = "DRY RUN (nothing written)" if p.get("dry_run") else "COMMITTED"
    s = [f"[{head}]"]
    ds = p.get("desc_source")
    if ds:
        s.append(f"  Desc from commit {ds['commit']} on {ds['branch']} "
                 f"(gid={ds['gitlab_project_id']}, {ds['source']}, {ds.get('committed_at','')})")
    s += [f"  aTask: {p['aTask']['name']}",
          f"         list={p['aTask']['list']} status=ready_to_deploy"]
    if p["aTask"].get("code"):
        s.append(f"         {p['aTask']['code']} -> {p['aTask']['link']}")
    sh = p["sheet"]
    s.append(f"  Sheet: tab '{sh['tab']}' row {sh['row']}")
    for k in ("B", "C", "F", "H", "N", "P", "Q"):
        s.append(f"         {k}: {sh['cells'][k]}")
    return "\n".join(s)


if __name__ == "__main__":
    main()
