#!/usr/bin/env python3
"""Toolkit web dashboard — ONE page to watch everything the repo runs, plus the settings form.

    python dashboard.py serve [--port 8765]     # prints the URL to open (with its edit key), e.g. in Orca's browser
    python dashboard.py snapshot                # the same data once, as JSON (scripts / debugging)

Sections: overview (usage tier, capacity, daily cap) · tasks of the durable store (review_mr / verify /
proposal / refactor / atask, as `coordinator.py board`) · agent accounts (`accounts.py`: who runs what,
usage left from Orca) · MR review jobs · pending approvals · daemons · pipeline runs · intake queue ·
port-forwards · feedback · settings (workflows.settings: non-secret switches only) · a command help per section.

Safety: listens on 127.0.0.1 only, refuses a foreign Host header (DNS rebinding). Monitoring is read-only
(store opened with Store.reader = sqlite mode=ro; no reader creates a directory). Writes — all need the
per-run edit key printed by `serve` (only in that terminal — the page receives it through the URL
fragment, never from the server) and a same-origin JSON request:
  POST /api/settings  the settings form (catalog validation)
  POST /api/action    a human decision on ONE task revision (unblock / retry / cancel + reason / accept an
                      aTask proposal / start, accept or reject a proposal) — runs in human.web_decision and
                      only if the task's `updated` is still the one the page showed (else 409)
  POST /api/assign    new work -> a store task (verify ladder / proposal / MR review); repos only under work/
Posting an MR review stays `review_post.py approve` (its approval is bound to the rendered text).
GET /api/task?id= is the full read-only view of one task (timeline, checkpoints, evidence report).
Secrets are never read out: the page only learns whether a secret key is set. Free text from state files
is passed through the secret redactor before it leaves the process; no traceback is sent.

Snapshots are built by ONE background worker (never per request): a request gets the latest snapshot
at once; `?fresh=1` asks the worker for a new one and waits a few seconds at most.
"""

import argparse
import hmac
import json
import os
import runpy
import secrets
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_HERE = os.path.dirname(os.path.abspath(__file__))
_SKILLS = os.path.dirname(os.path.dirname(_HERE))
runpy.run_path(os.path.join(_SKILLS, "_runtime", "bootstrap.py"))["activate"]()
from agent_skill_toolkit.core import store as store_mod  # noqa: E402
from agent_skill_toolkit.workflows import coordinator as coord  # noqa: E402
from agent_skill_toolkit.workflows import settings as settings_mod  # noqa: E402
from agent_skill_toolkit.workflows.execution import usage as usage_mod  # noqa: E402
from agent_skill_toolkit.workflows.verify.safety import redact  # noqa: E402

sys.path.insert(0, _HERE)
import status as status_mod  # noqa: E402  (daemons / runs / queue / feedback readers)

HTML_FILE = os.path.join(_HERE, "dashboard.html")
CACHE_S = 5                 # the worker rebuilds the snapshot at most this often (while someone watches)
IDLE_S = 60                 # no request for this long: the worker stops polling
ACCOUNTS_CACHE_S = 30       # `orca account list` is a subprocess: at most twice a minute
ORCA_TIMEOUT_S = 10
FRESH_WAIT_S = 6            # `?fresh=1` waits at most this long for the new snapshot
FAILED_WINDOW_S = 7 * 86400  # failures are terminal (no "resolve" action): shown for this long, KPI = same window
_cache = {"accounts_at": 0.0, "accounts": None}


def _clean(obj):
    """EVERY string leaving the process goes through the secret redactor — no key is trusted, since
    ids / statuses of state files are as untrusted as titles (Codex dashboard #1, R2 #1, R3 #1)."""
    if isinstance(obj, dict):
        return {(redact(k) if isinstance(k, str) else k): _clean(v) for k, v in obj.items()}   # keys too (R4)
    if isinstance(obj, list):
        return [_clean(v) for v in obj]
    if isinstance(obj, str):
        return redact(obj)
    return obj


def _section(fn):
    """Run one reader; a failing section becomes {"ok": False, "error": <type: redacted message>}
    and never breaks the page. No traceback leaves the process."""
    t0 = time.time()
    try:
        return {"ok": True, "data": _clean(fn()), "ms": round((time.time() - t0) * 1000)}
    except Exception as e:  # noqa: BLE001 — a dashboard shows the error instead of dying
        return {"ok": False, "error": redact(f"{type(e).__name__}: {e}")[:300],
                "ms": round((time.time() - t0) * 1000)}


def _state_base() -> str:
    """Base of the toolkit's state files, as core.paths / rc_config resolve it — never created here."""
    return os.environ.get("TOOLKIT_STATE_DIR") or os.path.join(status_mod._repo_root(), "temp")


def read_store() -> dict:
    """Board + settings through a READ-ONLY connection (no schema setup / write lock while agents run)."""
    try:
        st = store_mod.Store.reader()
    except FileNotFoundError:
        return {"exists": False, "rows": [], "summary": {"counts": {}}, "settings": {}, "done_24h": 0}
    try:
        b = coord.board(st)                                  # open tasks + those closed in the last 24 h
        today = usage_mod.agent_usage(st, since=usage_mod.local_midnight())
        settings = {"capacity": st.capacity(),
                    "daily_agent_runs": int(st.get_setting("daily_agent_runs") or 0),
                    "agent_runs_today": today["runs"], "agent_seconds_today": round(today["seconds"])}
        # counted in SQL, independent of the board's display limit (Codex dashboard R2 #3)
        now = time.time()
        done_24h = st.db.execute("select count(*) n from tasks where status='done' and updated>=?",
                                 (now - 86400,)).fetchone()["n"]
        # failed tasks for "needs you", independent of the board's closed-row limit (Codex dashboard R3 #2)
        failed = [{"id": t["id"], "kind": t["kind"], "title": t.get("title") or t["ref"], "status": "failed",
                   "note": str(t.get("last_error") or "").splitlines()[0][:160] if t.get("last_error") else "",
                   "updated": t.get("updated")}
                  for t in st.list(status=("failed",), limit=500) if (t.get("updated") or 0) >= now - FAILED_WINDOW_S]
        return {"exists": True, "rows": b["rows"], "summary": b["summary"], "settings": settings,
                "done_24h": done_24h, "failed_recent": failed, "failed_window_days": FAILED_WINDOW_S // 86400,
                "failed_window_count": st.db.execute("select count(*) n from tasks where status='failed' and updated>=?",
                                                     (now - FAILED_WINDOW_S,)).fetchone()["n"]}
    finally:
        st.close()


def read_accounts() -> dict:
    now = time.time()
    if _cache["accounts"] is not None and now - _cache["accounts_at"] < ACCOUNTS_CACHE_S:
        return _cache["accounts"]
    sys.path.insert(0, os.path.join(_SKILLS, "auto-dev", "tools"))
    import accounts as accounts_tool                      # auto-dev/tools/accounts.py (facade)
    data = accounts_tool.gather(orca_timeout=ORCA_TIMEOUT_S)
    _cache.update(accounts=data, accounts_at=now)
    return data


def _json_files(d: str):
    """(name, data) of every *.json in d, plus how many could NOT be read: a skipped file must
    never turn into "nothing to do" on the page (Codex dashboard R2 #2)."""
    ok, bad = [], 0
    if not os.path.isdir(d):
        return ok, bad
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return ok, 1
    for name in names:
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(d, name), encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            continue                                          # removed while listing: not an error
        except (OSError, ValueError):
            bad += 1
            continue
        if isinstance(data, dict):
            ok.append((name, data))
        else:
            bad += 1
    return ok, bad


def read_approvals() -> dict:
    """Pending approval requests — the SAME directory rc_config/approvals write (TOOLKIT_STATE_DIR aware)."""
    files, bad = _json_files(os.path.join(_state_base(), "tg_approvals"))
    items = [{"id": r.get("id"), "tool": r.get("tool"), "risk": r.get("risk"),
              "summary": (r.get("summary") or "")[:60], "created": r.get("created") or 0}
             for _, r in files if r.get("status") == "pending" and r.get("id") and r.get("tool")]
    items.sort(key=lambda a: a["created"], reverse=True)
    return {"items": items, "unreadable": bad}


def read_reviews() -> dict:
    # same place as review_common.temp_dir("jobs"), computed WITHOUT its makedirs
    jobs_dir = os.path.join(os.environ.get("REVIEW_STATE_DIR")
                            or os.path.join(status_mod._repo_root(), "temp", "review_queue"), "jobs")
    out = []
    files, bad = _json_files(jobs_dir)
    for _, j in files:
        out.append({"id": j.get("id"), "status": j.get("status"),
                    "mr": f"{j.get('project_path') or j.get('project')}!{j.get('iid')}",
                    "title": (j.get("title") or "")[:90], "verdict": j.get("verdict"),
                    "branches": f"{j.get('source_branch')} → {j.get('target_branch')}",
                    "updated": j.get("updated") or j.get("created"),
                    "by": ", ".join(sorted({str(r.get("by") or r.get("source", ""))
                                            for r in j.get("requests") or [] if isinstance(r, dict)})),
                    # requester notices not delivered (failed: sweep retries; unconfirmed / uncertain: a human
                    # decides) — shown under "needs you"
                    "notify_open": [{"state": n.get("state"), "error": str(n.get("error") or "")[:200],
                                     "attempts": n.get("attempts", 0)}
                                    for n in (j.get("notices") or {}).values()
                                    if isinstance(n, dict) and n.get("state") in ("failed", "unconfirmed", "uncertain")]})
    out.sort(key=lambda j: str(j.get("updated") or ""), reverse=True)
    return {"jobs": out, "unreadable": bad}


def _port_open(port) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=0.3):
            return True
    except (OSError, ValueError, TypeError):
        return False


def read_forwards() -> dict:
    """podfwd state files read directly (podfwd.list_all would mkdir and run `tasklist` without a
    deadline — Codex dashboard #3/#4): name, ports, target and whether the local port answers."""
    d = os.path.join(status_mod._repo_root(), "temp", "port_forwards")
    rows = []
    if os.path.isdir(d):
        for f in sorted(os.listdir(d)):
            if not f.endswith(".json"):
                continue
            try:
                with open(os.path.join(d, f), encoding="utf-8") as fh:
                    st = json.load(fh)
            except (OSError, ValueError):
                continue
            if not isinstance(st, dict) or not st.get("local_port"):
                continue
            t = st.get("target_spec") or {}
            rows.append({"name": st.get("name") or f[:-5], "local": f"127.0.0.1:{st['local_port']}",
                         "target": st.get("target"), "context": t.get("context"), "namespace": t.get("namespace"),
                         "listening": _port_open(st["local_port"]), "restarts": st.get("restarts", 0)})
    return {"count": len(rows), "forwards": rows}


def read_settings() -> dict:
    """Editable settings (values of NON-secret keys only) + which secret keys are set — no redaction:
    the catalog holds no secret, and a redacted value (a 24-hex group id looks like a token) would be
    saved back wrong."""
    try:
        st = store_mod.Store.reader()
    except FileNotFoundError:
        st = None
    try:
        return settings_mod.snapshot(st)
    finally:
        if st is not None:
            st.close()


# ----------------------------------------------------------------------------- one task, in full

DETAIL_EVENTS = 150
_BIG = 4000            # a free-text field longer than this is cut (the page shows a note)


def _cut(v, n: int = _BIG):
    if isinstance(v, str) and len(v) > n:
        return v[:n] + f"… (+{len(v) - n} ký tự)"
    if isinstance(v, dict):
        return {k: _cut(x, n) for k, x in v.items()}
    if isinstance(v, list):
        return [_cut(x, n) for x in v[:200]]
    return v


def _evidence(st, t: dict) -> dict:
    """Kind-specific "what did the AI produce" view. verify: AC -> evidence report (read-only: git show
    of the spec + stored checkpoints). Never raises: a missing report is shown as its reason."""
    if t["kind"] != "verify":
        return {}
    try:
        from agent_skill_toolkit.workflows.verify import ladder as L
        rep = L.evidence_report(st, t["id"])
        return {"report_md": rep.get("markdown") or "", "rungs": rep.get("rungs") or [],
                "acs": rep.get("acs") or [], "not_checked": rep.get("not_checked") or []}
    except Exception as e:  # noqa: BLE001
        return {"report_error": f"{type(e).__name__}: {e}"[:400]}


def read_task(task_id: str) -> dict:
    """Everything one task did: row, checkpoints (what each step produced), events (timeline), deps,
    children, usage, and the kind's evidence. READ-ONLY handle; every string redacted."""
    if not task_id or len(task_id) > 64:
        raise ValueError("task id missing")
    st = store_mod.Store.reader()
    try:
        t = st.get(task_id)
        if not t:
            raise LookupError(f"no task {task_id}")
        cps = [{"id": c["id"], "step": c["step"], "valid": bool(c["valid"]), "ts": c["ts"],
                "sha": (c.get("sha") or "")[:12], "data": _cut(c.get("data") or {}, 1500)}
               for c in st.checkpoints(task_id)]
        events = [{"ts": e["ts"], "kind": e["kind"], "data": _cut(e.get("data") or {}, 600)}
                  for e in st.events(task_id, limit=DETAIL_EVENTS)]
        usage = st.db.execute("select count(*) n, coalesce(sum(seconds),0) s, coalesce(sum(tokens_in),0) ti, "
                              "coalesce(sum(tokens_out),0) tout from usage where task_id=?", (task_id,)).fetchone()
        deps = [{"id": d, "status": (st.get(d) or {}).get("status", "?"), "title": (st.get(d) or {}).get("title", "")}
                for d in st.deps_of(task_id)]
        kids = [{"id": c["id"], "kind": c["kind"], "status": c["status"], "title": c.get("title") or c["ref"]}
                for c in st.children(task_id)]
        task = {k: t.get(k) for k in ("id", "kind", "ref", "title", "status", "step", "attempts", "max_attempts",
                                       "last_error", "created", "updated", "resource", "parent", "resume_at",
                                       "lease_owner", "priority")}
        out = {"task": task, "data": _cut(t.get("data") or {}), "checkpoints": cps, "events": events,
               "deps": deps, "children": kids, "actions": actions_for(t),
               "usage": {"runs": usage["n"], "seconds": round(usage["s"] or 0),
                         "tokens_in": usage["ti"], "tokens_out": usage["tout"]},
               "evidence": _evidence(st, t)}
    finally:
        st.close()
    upd = out["task"]["updated"]                    # the revision token travels exactly (never redacted)
    out = _clean(out)
    out["task"]["updated"] = upd
    return out


# ----------------------------------------------------------------------------- human actions (edit key)
#
# Every action is a HUMAN decision: the request carries the per-run edit key, runs inside
# human.web_decision (the same handlers the terminal uses accept it instead of a TTY; refused when the
# dashboard itself runs under an agent), and names the revision (`updated`) the person looked at —
# if the task moved meanwhile nothing happens and the page says "look again".

def _tool(skill: str, name: str):
    """Import a tool facade (auto-dev/tools/verify.py, ...) once, under a private module name, with its
    own directory first on sys.path (two skills have a `config`)."""
    import importlib.util
    mod_name = f"_dash_{skill.replace('-', '_')}_{name}"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    d = os.path.join(_SKILLS, skill, "tools")
    if d not in sys.path:
        sys.path.insert(0, d)
    spec = importlib.util.spec_from_file_location(mod_name, os.path.join(d, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    try:
        spec.loader.exec_module(mod)
    except BaseException:
        sys.modules.pop(mod_name, None)
        raise
    return mod


PROPOSAL_WAIT_GO, PROPOSAL_WAIT_DECISION, ATASK_PROPOSAL = "candidate: ", "adr ready: ", "proposal: "


def actions_for(t: dict) -> list:
    """Buttons the page may show for this task revision (the server re-checks on click)."""
    s, kind, err = t["status"], t["kind"], str(t.get("last_error") or "")
    out = []
    if kind == "proposal" and s == "blocked" and err.startswith(PROPOSAL_WAIT_GO):
        out += ["proposal_go", "proposal_reject"]
    elif kind == "proposal" and s == "blocked" and err.startswith(PROPOSAL_WAIT_DECISION):
        out += ["proposal_accept", "proposal_reject"]
    elif kind == "atask_intake" and s == "blocked" and err.startswith(ATASK_PROPOSAL):
        out += ["atask_accept"]
    elif s == "blocked":
        out.append("unblock")
    if s == "failed":
        out.append("retry")
    if s in store_mod.OPEN or s == "failed":
        out.append("cancel")
    return out


def do_action(body: dict) -> dict:
    from agent_skill_toolkit.core import human
    tid, action = str(body.get("id") or ""), str(body.get("action") or "")
    note = str(body.get("note") or "").strip()[:1000]
    upd = body.get("updated")
    if not tid or not action:
        raise ValueError("id and action are required")
    if not isinstance(upd, (int, float)):
        raise ValueError("updated (the revision you looked at) is required")
    with store_mod.Store() as st:
        t = st.get(tid)
    if not t:
        raise LookupError(f"no task {tid}")
    if t["updated"] != upd:
        raise RuntimeError("việc đã thay đổi từ lúc bạn mở — xem lại rồi bấm lại")
    if action not in actions_for(t):
        raise ValueError(f"'{action}' không áp dụng cho việc đang ở trạng thái {t['status']}")
    by = "human:dashboard"
    with human.web_decision(by):
        if action in ("unblock", "retry", "cancel"):
            with store_mod.Store() as st:
                if action == "unblock":
                    ok = st.unblock(tid, by=by, if_updated=upd, note=note)
                elif action == "retry":
                    ok = st.retry(tid, by=by, if_updated=upd, note=note)
                else:
                    if not note:
                        raise ValueError("ghi lý do huỷ (để sau này còn biết vì sao)")
                    ok = st.cancel(tid, by=by, failed_too=True, if_updated=upd, note=note)
            if not ok:
                raise RuntimeError("việc đã thay đổi từ lúc bạn mở — xem lại rồi bấm lại")
            return {"id": tid, "action": action, "ok": True}
        if action == "atask_accept":
            lane = str(body.get("lane") or "")
            return _tool("auto-dev", "coordinator").accept(tid, lane)
        p = _tool("auto-dev", "proposals")
        if action == "proposal_go":
            return p.cmd_go(tid)
        if action == "proposal_accept":
            return p.cmd_decide(tid, "accept", note)
        if action == "proposal_reject":
            if not note:
                raise ValueError("ghi lý do từ chối (được nhớ để không đề xuất lại)")
            return p.cmd_decide(tid, "reject", note)
    raise ValueError(f"unknown action {action}")


# ----------------------------------------------------------------------------- assign work (edit key)

def _repo_root() -> str:
    return status_mod._repo_root()


def _work_dir() -> str:
    return os.environ.get("WORK_DIR") or os.path.join(_repo_root(), "work")


def read_repos() -> dict:
    """Checkouts the form can target: registry clones + worktrees under work/wt/<project>/<name>.
    Names and paths only (no git call)."""
    work = _work_dir()
    out = []
    try:
        with open(os.path.join(work, "projects.json"), encoding="utf-8") as f:
            reg = json.load(f)
    except (OSError, ValueError):
        reg = {}
    projects = reg.get("projects", reg) if isinstance(reg, dict) else {}
    for name, p in sorted(projects.items()) if isinstance(projects, dict) else []:
        cd = str((p or {}).get("clone_dir") or "") if isinstance(p, dict) else ""
        if cd.startswith("work:"):
            cd = os.path.join(work, cd[5:])
        if cd and os.path.isdir(cd):
            out.append({"project": name, "kind": "clone", "path": os.path.normpath(cd),
                        "default_branch": p.get("default_branch") or ""})
    wt = os.path.join(work, "wt")
    if os.path.isdir(wt):
        for proj in sorted(os.listdir(wt)):
            pd = os.path.join(wt, proj)
            if not os.path.isdir(pd):
                continue
            for n in sorted(os.listdir(pd)):
                if os.path.isdir(os.path.join(pd, n)):
                    out.append({"project": proj, "kind": "worktree", "name": n,
                                "path": os.path.normpath(os.path.join(pd, n))})
    return {"repos": out}


def _inside_work(path: str) -> str:
    """The form may only point at checkouts under work/ (registry clones / worktrees)."""
    real = os.path.realpath(path)
    work = os.path.realpath(_work_dir())
    if not real.lower().startswith(work.lower() + os.sep):
        raise ValueError("repo phải nằm trong work/ (clone trong registry hoặc worktree)")
    if not os.path.isdir(real):
        raise ValueError(f"không có thư mục {path}")
    return real


def do_assign(body: dict) -> dict:
    """New work from the form -> a task in the store (the Coordinator tab runs it)."""
    kind = str(body.get("kind") or "")
    if kind == "verify":
        repo = _inside_work(str(body.get("repo") or ""))
        v = _tool("auto-dev", "verify")
        acs = v.parse_acs(str(body.get("acs") or "").splitlines())
        return v.start(repo, str(body.get("base") or ""), "", str(body.get("fixer_role") or "implement"),
                       False, acs)
    if kind == "proposal":
        repo = _inside_work(str(body.get("repo") or ""))
        q = str(body.get("question") or "").strip()
        if len(q) < 10:
            raise ValueError("câu hỏi quá ngắn")
        return _tool("auto-dev", "proposals").cmd_new(repo, q[:2000], str(body.get("criteria") or "")[:2000])
    if kind == "review_mr":
        text = str(body.get("text") or "").strip()
        if not text:
            raise ValueError("dán link MR (hoặc project!iid)")
        return _tool("mr-review", "review_flow").enqueue_text(text[:4000], str(body.get("project") or ""),
                                                             source="manual", requester="dashboard")
    raise ValueError("kind must be verify | proposal | review_mr")


READERS = {"store": "read_store", "accounts": "read_accounts", "reviews": "read_reviews",
           "approvals": "read_approvals", "daemons": None, "runs": None, "queue": None,
           "forwards": "read_forwards", "feedback": None}


def collect() -> dict:
    """Build one snapshot now (the worker's job; also `snapshot` and tests)."""
    mod = sys.modules[__name__]
    sections = {}
    for name, fn in READERS.items():
        reader = getattr(mod, fn) if fn else getattr(status_mod, f"sect_{name}")
        sections[name] = _section(reader)
    return {"generated_at": time.time(), "host": socket.gethostname(), "sections": sections}


class Snapshots:
    """ONE background worker builds snapshots; requests never run readers themselves (Codex #4)."""

    def __init__(self, build=collect):
        self.build = build
        self.data, self.gen = None, 0
        self.cond = threading.Condition()
        self.wanted = threading.Event()
        self.last_request = time.time()
        self.thread = None

    def _loop(self):
        while True:
            self.wanted.wait(timeout=CACHE_S)
            self.wanted.clear()
            if time.time() - self.last_request > IDLE_S and self.data is not None:
                continue                                       # nobody watching: stay idle
            data = self.build()
            with self.cond:
                self.data, self.gen = data, self.gen + 1
                self.cond.notify_all()

    def start(self):
        if not self.thread:
            self.thread = threading.Thread(target=self._loop, name="dashboard-snapshots", daemon=True)
            self.thread.start()
        return self

    def get(self, fresh: bool = False, wait_s: float = FRESH_WAIT_S) -> dict:
        self.last_request = time.time()
        with self.cond:
            gen = self.gen
            if fresh or self.data is None:
                self.wanted.set()                              # coalesced: many fresh=1 -> one rebuild
                self.cond.wait_for(lambda: self.gen > gen, timeout=wait_s if self.data is not None else 30)
            return self.data or {"generated_at": time.time(), "host": socket.gethostname(),
                                 "sections": {}, "pending": True}


SNAPSHOTS = Snapshots()


MAX_BODY = 16 * 1024
DISCARD_MAX = 1024 * 1024    # an oversized body up to this is read and dropped before the 413


class Handler(BaseHTTPRequestHandler):
    server_version = "toolkit-dashboard"
    snapshots = SNAPSHOTS
    edit_key = ""               # set by serve(): random per run, printed only to its terminal

    def log_message(self, fmt, *args):                    # quiet: only 4xx / 5xx
        if len(args) > 1 and str(args[1]).startswith(("4", "5")):
            sys.stderr.write("dashboard: " + (fmt % args) + "\n")

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").strip().lower()
        if host.startswith("["):
            host = host[1:].split("]", 1)[0]
        else:
            host = host.rsplit(":", 1)[0]
        return host in ("127.0.0.1", "localhost", "::1")

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
                         "frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if not self._host_ok():
            return self._send(403, b"forbidden host", "text/plain; charset=utf-8")
        path, _, query = self.path.partition("?")
        if path in ("/", "/index.html"):
            try:
                with open(HTML_FILE, "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            except OSError:
                return self._send(500, b"dashboard.html missing", "text/plain; charset=utf-8")
        if path == "/api/state":
            data = self.snapshots.get(fresh="fresh=1" in query.split("&"))
            return self._json(200, data)
        if path == "/api/settings":
            try:                                               # NOT through _clean: see read_settings
                return self._json(200, {"ok": True, "data": read_settings(), "editable": bool(self.edit_key)})
            except Exception as e:  # noqa: BLE001
                return self._json(200, {"ok": False, "error": redact(f"{type(e).__name__}: {e}")[:300]})
        if path == "/api/task":
            from urllib.parse import parse_qs
            tid = (parse_qs(query).get("id") or [""])[0]
            try:
                return self._json(200, {"ok": True, "data": read_task(tid), "editable": bool(self.edit_key)})
            except (LookupError, FileNotFoundError) as e:
                return self._json(404, {"ok": False, "error": redact(str(e))[:300]})
            except Exception as e:  # noqa: BLE001
                return self._json(200, {"ok": False, "error": redact(f"{type(e).__name__}: {e}")[:300]})
        if path == "/api/repos":
            return self._json(200, {"ok": True, "data": _clean(read_repos())})
        return self._send(404, b"not found", "text/plain; charset=utf-8")

    def _json(self, code: int, obj):
        return self._send(code, json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"),
                          "application/json; charset=utf-8")

    def _write_allowed(self) -> str:
        """'' when this request may change settings, else why not. A browser page of another site can not
        pass: it has no edit key (never served by GET), a JSON body forces a CORS preflight we never
        answer, and Origin / Sec-Fetch-Site must be this dashboard."""
        if not self._host_ok():
            return "forbidden host"
        port = self.server.server_address[1]
        origin = self.headers.get("Origin")
        if origin is not None and origin not in (f"http://127.0.0.1:{port}", f"http://localhost:{port}"):
            return "foreign origin"
        if (self.headers.get("Sec-Fetch-Site") or "same-origin") != "same-origin":
            return "cross-site request"
        if (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/json":
            return "JSON only"
        key = self.headers.get("X-Dashboard-Key") or ""
        if not self.edit_key or not hmac.compare_digest(key.encode(), self.edit_key.encode()):
            return "missing or wrong edit key — open the URL printed by `dashboard.py serve`"
        return ""

    def _drain(self) -> bytes:
        """Read the request body BEFORE answering (bounded): replying with unread bytes in the socket
        makes Windows reset the connection, and the client sees an error instead of the answer. An
        oversized / unknown-length body is not read: the connection is closed after the reply."""
        try:
            n = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            n = -1
        if 0 <= n <= MAX_BODY:
            return self.rfile.read(n) if n else b""
        self.close_connection = True
        if 0 < n <= DISCARD_MAX:                               # read and drop: a clean 413, not a reset
            while n > 0:
                n -= len(self.rfile.read(min(n, 65536)) or b"x" * n)
        return None

    POST_ROUTES = ("/api/settings", "/api/action", "/api/assign")

    def do_POST(self):  # noqa: N802
        raw = self._drain()
        path = self.path.partition("?")[0]
        if path not in self.POST_ROUTES:
            return self._send(405, b"read-only", "text/plain; charset=utf-8")
        why = self._write_allowed()
        if why:
            return self._json(403, {"ok": False, "error": why})
        if not raw:
            return self._json(413, {"ok": False, "error": "body missing or too large"})
        if path != "/api/settings":
            return self._human_post(path, raw)
        try:
            body = json.loads(raw.decode("utf-8"))
            if not isinstance(body, dict):                     # null / [] / "x" (Codex settings R1 #5)
                raise ValueError("body must be a JSON object")
            changes, expected = body.get("changes"), body.get("expected") or {}
            if not isinstance(changes, dict) or not isinstance(expected, dict):
                raise ValueError("changes / expected must be objects")
            out = settings_mod.apply(changes, by="dashboard", expected=expected)
        except (ValueError, RuntimeError, FileNotFoundError, TimeoutError, UnicodeDecodeError) as e:
            return self._json(400, {"ok": False, "error": redact(str(e))[:400]})
        except OSError as e:                                   # e.g. .env locked by another program (R1 #2)
            return self._json(503, {"ok": False, "error": redact(f"không ghi được: {type(e).__name__}: {e}")[:400]})
        self.snapshots.wanted.set()                            # capacity / caps show at once
        return self._json(200, {"ok": True, **out})

    @staticmethod
    def _hint(msg: str) -> str:
        """A known cause the person can fix, in front of the raw error."""
        low = msg.lower()
        if "invalid_token" in low or ("401" in msg and "gitlab" in low):
            return ("Token GitLab hết hạn / sai — tạo Personal Access Token mới (scope api) rồi sửa "
                    "GITLAB_PRIVATE_TOKEN trong .env. Chi tiết: " + msg)
        return msg

    def _human_post(self, path: str, raw: bytes):
        """/api/action and /api/assign: a human decision (edit key already checked). Errors come back
        as a message, never a traceback; a task that changed meanwhile is 409."""
        try:
            body = json.loads(raw.decode("utf-8"))
            if not isinstance(body, dict):
                raise ValueError("body must be a JSON object")
            out = do_action(body) if path == "/api/action" else do_assign(body)
        except PermissionError as e:
            return self._json(403, {"ok": False, "error": self._hint(redact(str(e)))[:600]})
        except LookupError as e:
            return self._json(404, {"ok": False, "error": self._hint(redact(str(e)))[:600]})
        except RuntimeError as e:                              # changed meanwhile / tool refused
            return self._json(409, {"ok": False, "error": self._hint(redact(str(e)))[:600]})
        except (ValueError, UnicodeDecodeError, FileNotFoundError, OSError) as e:
            return self._json(400, {"ok": False, "error": self._hint(redact(f"{e}"))[:600]})
        except Exception as e:  # noqa: BLE001 — a tool failure is a message on the page
            return self._json(500, {"ok": False, "error": self._hint(redact(f"{type(e).__name__}: {e}"))[:600]})
        sys.stderr.write(f"dashboard: {path} {json.dumps({k: body.get(k) for k in ('action', 'kind', 'id')})}\n")
        self.snapshots.wanted.set()
        return self._json(200, {"ok": True, "result": _clean(out)})

    def _refuse(self):
        self._drain()
        self._send(405, b"read-only", "text/plain; charset=utf-8")

    do_PUT = do_DELETE = do_PATCH = _refuse


def serve(port: int, read_only: bool = False) -> None:
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.daemon_threads = True
    Handler.snapshots.start()
    Handler.edit_key = "" if read_only else secrets.token_urlsafe(24)
    if read_only:
        print(f"Dashboard (chỉ xem): http://127.0.0.1:{port}   (Ctrl+C để dừng)", flush=True)
    else:
        # the key travels in the URL FRAGMENT: browsers never send it to a server or put it in logs
        print(f"Dashboard: http://127.0.0.1:{port}/#key={Handler.edit_key}\n"
              f"  mở ĐÚNG link này để sửa được cài đặt (key đổi mỗi lần chạy; mở không có key = chỉ xem)\n"
              f"  Ctrl+C để dừng", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("serve")
    p.add_argument("--port", type=int, default=int(os.environ.get("TOOLKIT_DASHBOARD_PORT") or 8765))
    p.add_argument("--read-only", action="store_true", help="no settings form (no edit key)")
    sub.add_parser("snapshot")
    a = ap.parse_args(argv)
    if a.cmd == "serve":
        serve(a.port, read_only=a.read_only)
    else:
        print(json.dumps(collect(), ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
