#!/usr/bin/env python3
"""Shared helpers for the mr-review tools (queue / workspace / post).

Not run directly. Stdlib only; reuses dev-automation's config + gitlab_api.

Layout (all gitignored):
    temp/review_queue/jobs/<job_id>.json     one job per MR  (job_id = "<project_id>-<iid>")
    temp/review_queue/locks/                 short-lived file locks
    work/cache/<path_with_namespace>.git     bare partial clone per GitLab project
    work/reviews/<project_id>/<iid>@<sha8>/  detached worktree at the MR head
    work/reviews/<project_id>/<iid>@<sha8>.ws.json  workspace facts (diff_refs, commits, ...)
    work/reviews/ledger/<project_id>.jsonl   commits already reviewed (sha + patch-id)
"""

import contextlib
import json
import os
import re
import sys
import time
import urllib.parse

_HERE = os.path.dirname(os.path.abspath(__file__))
_DEV_TOOLS = os.path.normpath(os.path.join(_HERE, "..", "..", "dev-automation", "tools"))
if _DEV_TOOLS not in sys.path:
    sys.path.insert(0, _DEV_TOOLS)

import config  # noqa: E402  (dev-automation .env loader)
import gitlab_api  # noqa: E402
import probe_common as pc  # noqa: E402,F401  (UTF-8 stdout on Windows)
import project_config  # noqa: E402

TERMINAL = ("posted", "rejected", "failed", "cancelled")
ACTIVE = ("running", "awaiting_approval", "approved")
STATUSES = ("queued",) + ACTIVE + TERMINAL + ("stale",)


# --------------------------------------------------------------------------- paths

def repo_root() -> str:
    return project_config._repo_root()


def work_dir() -> str:
    return project_config._work_dir()


def temp_dir(*parts) -> str:
    p = os.path.join(os.environ.get("REVIEW_STATE_DIR") or os.path.join(repo_root(), "temp", "review_queue"), *parts)
    os.makedirs(p if not os.path.splitext(p)[1] else os.path.dirname(p), exist_ok=True)
    return p


def reviews_dir(*parts) -> str:
    p = os.path.join(os.environ.get("REVIEW_WS_DIR") or os.path.join(work_dir(), "reviews"), *parts)
    return p


def cache_dir(*parts) -> str:
    return os.path.join(os.environ.get("REVIEW_CACHE_DIR") or os.path.join(work_dir(), "cache"), *parts)


def tools_dir() -> str:
    return _HERE


def cfg_int(key: str, default: int) -> int:
    try:
        return int(config.get(key, str(default)) or default)
    except ValueError:
        return default


def now() -> int:
    return int(time.time())


# --------------------------------------------------------------------------- locks

@contextlib.contextmanager
def file_lock(name: str, timeout: float = 120, stale_after: float = 900):
    """Portable exclusive lock (O_EXCL lock file). Breaks locks older than stale_after."""
    path = temp_dir("locks", re.sub(r"[^A-Za-z0-9_.-]", "_", name) + ".lock")
    deadline = time.time() + timeout
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, f"{os.getpid()} {now()}".encode())
            os.close(fd)
            break
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(path) > stale_after:
                    os.remove(path)
                    continue
            except OSError:
                continue
            if time.time() > deadline:
                raise TimeoutError(f"lock busy: {name}")
            time.sleep(0.2)
    try:
        yield
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


# --------------------------------------------------------------------------- jobs

def job_id(project_id, iid) -> str:
    return f"{project_id}-{iid}"


def job_path(jid: str) -> str:
    if not re.fullmatch(r"\d+-\d+", jid or ""):
        raise ValueError(f"bad job id: {jid!r}")
    return temp_dir("jobs", f"{jid}.json")


def load_job(jid: str) -> dict:
    try:
        with open(job_path(jid), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_job(job: dict, sync: bool = True) -> dict:
    job["updated"] = now()
    if sync and job.get("_synced_status") != job.get("status"):
        orca_sync(job)
        job["_synced_status"] = job.get("status")
    path = job_path(job["id"])
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(job, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return job


def all_jobs() -> list:
    d = temp_dir("jobs")
    out = []
    for name in sorted(os.listdir(d)):
        if name.endswith(".json"):
            j = load_job(name[:-5])
            if j:
                out.append(j)
    return out


def log_event(job: dict, event: str, **detail) -> None:
    job.setdefault("history", []).append({"at": now(), "event": event, **detail})
    job["history"] = job["history"][-50:]


# --------------------------------------------------------------------------- gitlab (per project)

@contextlib.contextmanager
def gitlab_project(project_id):
    """gitlab_api reads GITLAB_PROJECT_ID per call; scope it to one project."""
    old = os.environ.get("GITLAB_PROJECT_ID")
    os.environ["GITLAB_PROJECT_ID"] = str(project_id)
    try:
        yield gitlab_api
    finally:
        if old is None:
            os.environ.pop("GITLAB_PROJECT_ID", None)
        else:
            os.environ["GITLAB_PROJECT_ID"] = old


def gl_check(res, what: str):
    if isinstance(res, dict) and res.get("error"):
        raise RuntimeError(f"GitLab {what} failed ({res.get('status')}): {str(res.get('message'))[:300]}")
    return res


def gitlab_project_info(project) -> dict:
    """project = numeric id or 'group/sub/repo' path."""
    pid = urllib.parse.quote(str(project), safe="")
    return gl_check(gitlab_api._request(f"{config.gitlab_base_url()}/projects/{pid}"), "project lookup")


def gitlab_mr(project_id, iid) -> dict:
    with gitlab_project(project_id) as gl:
        return gl_check(gl.get_merge_request(int(iid)), f"MR !{iid}")


# --------------------------------------------------------------------------- registry

def registry() -> dict:
    path = os.path.join(work_dir(), "projects.json")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def registry_by_gitlab_id(project_id) -> tuple:
    for name, p in registry().items():
        if isinstance(p, dict) and str(p.get("gitlab_project_id")) == str(project_id):
            return name, p
    return None, {}


def registry_by_name(name: str) -> dict:
    p = registry().get(name)
    return p if isinstance(p, dict) else {}


# --------------------------------------------------------------------------- request parsing

_MR_URL = re.compile(r"https?://(?P<host>[^/\s]+)/(?P<path>[\w.\-/]+?)/-/merge_requests/(?P<iid>\d+)")
_MR_BANG = re.compile(r"(?<![\w/])!(?P<iid>\d{1,7})\b")
_MR_WORD = re.compile(r"\bMR\s*#?\s*(?P<iid>\d{1,7})\b", re.I)


def parse_mr_refs(text: str, default_project: str = "") -> list:
    """Extract MR references from free text. Returns [{project, iid, host?}] — data only,
    never instructions. A URL carries its project path; '!123'/'MR 123' need a project
    name from the registry mentioned in the text, else `default_project`."""
    refs, seen = [], set()
    for m in _MR_URL.finditer(text or ""):
        key = (m["path"], m["iid"])
        if key not in seen:
            seen.add(key)
            refs.append({"project": m["path"], "iid": int(m["iid"]), "host": m["host"]})
    if refs:
        return refs
    low = (text or "").lower()
    named = next((n for n in registry() if re.search(rf"(?<![\w-]){re.escape(n.lower())}(?![\w-])", low)), "")
    project = named or default_project
    for rx in (_MR_BANG, _MR_WORD):
        for m in rx.finditer(text or ""):
            key = (project, m["iid"])
            if project and key not in seen:
                seen.add(key)
                refs.append({"project": project, "iid": int(m["iid"])})
    return refs


def resolve_project_id(project: str) -> tuple:
    """Registry name | numeric id | GitLab path -> (project_id, path_with_namespace)."""
    reg = registry_by_name(project)
    if reg.get("gitlab_project_id"):
        return str(reg["gitlab_project_id"]), reg.get("gitlab_path", "")
    if str(project).isdigit():
        return str(project), ""
    info = gitlab_project_info(project)
    return str(info["id"]), info.get("path_with_namespace", project)


# --------------------------------------------------------------------------- orca (optional)

_ORCA_STATUS = {"queued": "pending", "running": "dispatched", "awaiting_approval": "blocked",
                "approved": "dispatched", "stale": "pending", "posted": "completed",
                "rejected": "failed", "failed": "failed", "cancelled": "failed"}


def orca_enabled() -> bool:
    return config.get("REVIEW_ORCA", "1").lower() not in ("0", "false", "no")


def orca():
    """orca_adapter module if Orca integration is on and the runtime is reachable, else None."""
    if not orca_enabled():
        return None
    try:
        import orca_adapter
    except ImportError:
        return None
    return orca_adapter if orca_adapter.available() else None


def orca_title(job: dict) -> str:
    who = ", ".join(sorted({r.get("by") for r in job.get("requests", []) if r.get("by")}))
    title = f"[Review] {job.get('project_path') or job['project_id']}!{job['iid']} — {job.get('title', '')[:70]}"
    return title + (f" · @{who}" if who else "")


def orca_open(job: dict) -> dict:
    """Create this job's own Run + task in Orca (bound to the CALLING terminal — Orca fences
    orchestration writes per terminal, so the worker that drafts the review owns them).
    Raises OrcaError; callers fall back to other approval channels."""
    o = orca()
    if not o:
        raise RuntimeError("orca not available")
    if not job.get("orca_task"):
        title = orca_title(job)
        job["orca_run"] = o.call("orchestration", "run-create", "--objective", title)["run"]["id"]
        spec = (f"MR review job {job['id']}: {job.get('source_branch')} -> {job.get('target_branch')}\n"
                f"{job.get('web_url', '')}\nState: python {os.path.join(tools_dir(), 'review_queue.py')} show {job['id']}")
        job["orca_task"] = o.task_create(job["orca_run"], title, spec, display_name=title)
    return job


def orca_sync(job: dict) -> dict:
    """Mirror the job status onto its Orca task when this terminal owns it (best effort)."""
    if not job.get("orca_task"):
        return job
    o = orca()
    if not o:
        return job
    try:
        o.task_update(job["orca_run"], job["orca_task"], _ORCA_STATUS.get(job["status"], "pending"),
                      {"job": job["id"], "status": job["status"], "verdict": job.get("verdict")})
        job.pop("orca_error", None)
    except Exception as e:  # noqa: BLE001 — fenced from another terminal, Orca down, ...
        job["orca_error"] = str(e)[:160]
    return job


# --------------------------------------------------------------------------- orca board (human UI)
# Gates have no human UI in Orca; the worktree board does. The review worktree is the
# card: awaiting -> REVIEW_BOARD_PENDING; the HUMAN drags it to APPROVE (post) or REJECT.

def board_statuses() -> dict:
    return {"pending": config.get("REVIEW_BOARD_PENDING", "in-review"),
            "approve": config.get("REVIEW_BOARD_APPROVE", "completed"),
            "reject": config.get("REVIEW_BOARD_REJECT", "todo")}


def board_card(job: dict, *, status: str = "", note: str = "") -> bool:
    """Name/annotate the job's worktree card in Orca. False if Orca doesn't know it."""
    o = orca()
    if not o or not job.get("worktree"):
        return False
    title = orca_title(job)[:110]
    try:
        o.worktree_set(f"path:{job['worktree']}", display_name=title, status=status, comment=note[:400])
        return True
    except Exception as e:  # noqa: BLE001
        job["orca_error"] = str(e)[:160]
        return False


def board_status(job: dict) -> str:
    o = orca()
    if not o or not job.get("worktree"):
        return ""
    try:
        return o.worktree_show(f"path:{job['worktree']}").get("workspaceStatus") or ""
    except Exception:  # noqa: BLE001
        return ""
