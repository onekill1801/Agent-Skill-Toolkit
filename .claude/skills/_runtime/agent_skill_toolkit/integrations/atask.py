"""aTask AI-agent channel for the runtime (POST /api/ai/execute, PAT header) — stdlib only, config from core.env
(ATASK_BASE_URL, ATASK_PAT_TOKEN, ATASK_PAT_HEADER, ATASK_VERIFY_SSL). Same tools the atask-automation skill
wraps; this copy exists so daemons (group_watch) need not import that skill's `client`/`config`, whose names
clash with the tchat ones.

Errors come back as {"error": True, "status": ..., "message": ...}; a WRITE whose outcome is unknown (network
error, timeout, unreadable body) also carries "uncertain": True — reconcile before retrying (like gitlab.py).
"""

import json
import ssl
import urllib.error
import urllib.request

from agent_skill_toolkit.core import env

TIMEOUT = 60
PAGE = 50
MAX_PAGES = 20           # reconcile scans at most PAGE*MAX_PAGES same-name tasks, then answers "unknown"
WRITES = {"create_task", "update_task", "complete_task", "assign_task_users", "delete_task", "move_task"}


def base_url() -> str:
    return env.get("ATASK_BASE_URL", "http://localhost:8080").strip().rstrip("/")


def task_link(code: str) -> str:
    return f"{base_url()}/task/{code}" if code else ""


def _ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if env.get("ATASK_VERIFY_SSL", "true").lower() in ("false", "0", "no"):
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def execute(tool: str, args: dict, timeout: float = TIMEOUT) -> dict:
    """One tool call. Success = the server's ToolResult ({"success": true, "content": …})."""
    token = env.get("ATASK_PAT_TOKEN", "").strip()
    if not token:
        return {"error": True, "status": 0, "message": "ATASK_PAT_TOKEN chưa đặt"}
    headers = {env.get("ATASK_PAT_HEADER", "X-aTask-PAT").strip() or "X-aTask-PAT": token,
               "Content-Type": "application/json", "Accept": "application/json"}
    body = json.dumps({"name": tool, "arguments": args}).encode("utf-8")
    req = urllib.request.Request(f"{base_url()}/api/ai/execute", data=body, headers=headers, method="POST")
    write = tool in WRITES
    try:
        with urllib.request.urlopen(req, context=_ssl_context(), timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        # 5xx / 408 on a write: the server (or a gateway in front of it) may have committed before failing
        return {"error": True, "status": e.code, "uncertain": write and (e.code >= 500 or e.code == 408),
                "message": (e.read().decode(errors="replace") if e.fp else "")[:500]}
    except (urllib.error.URLError, OSError) as e:
        return {"error": True, "status": None, "uncertain": write,
                "message": f"{type(e).__name__}: {getattr(e, 'reason', e)}"}
    try:
        r = json.loads(raw)
    except ValueError as e:
        return {"error": True, "status": "bad-body", "uncertain": write, "message": str(e)[:200]}
    if not isinstance(r, dict):
        return {"error": True, "status": "bad-body", "uncertain": write, "message": "not an object"}
    if r.get("error") or r.get("success") is False:
        return {"error": True, "status": r.get("errorCode") or r.get("status") or "ERROR",
                "message": str(r.get("errorMessage") or r.get("message") or r)[:500]}
    return r


def content(r: dict) -> dict:
    """The object inside a ToolResult (`content`, or `content.data` for pages)."""
    c = r.get("content") if isinstance(r, dict) else None
    return c if isinstance(c, dict) else {}


def create_task(name: str, list_id: str, description: str = "", due_date: str = "") -> dict:
    args = {"name": name, "list_task_id": list_id}
    if description:
        args["description"] = description
    if due_date:
        args["due_date"] = due_date
    return execute("create_task", args)


def assign_users(task_id: str, user_ids: list) -> dict:
    return execute("assign_task_users", {"task_id": task_id, "user_ids": [int(u) for u in user_ids], "mode": "add"})


def complete_task(task_id: str) -> dict:
    return execute("complete_task", {"task_id": task_id})


def search_page(query: str, list_id: str, page: int, size: int = PAGE):
    """(rows, total) of one page of a list's name search; None on error or a malformed answer."""
    r = execute("search_tasks", {"query": query, "list_task_id": list_id, "page": page, "size": size,
                                 "sort_by": "sortOrder", "sort_order": "asc"})
    if r.get("error"):
        return None
    c = content(r)
    rows, total = c.get("data"), c.get("totalRecords")
    if not isinstance(rows, list) or not isinstance(total, int):
        return None
    return rows, total


def find_by_marker(name: str, list_id: str, marker: str):
    """The task of `list_id` named `name` whose description holds `marker` (idempotency key), {} only when
    EVERY page was read and none matches; None when the answer is unknown (error, malformed page, too many
    pages) — the caller must not create again."""
    seen = 0
    for page in range(MAX_PAGES):
        got = search_page(name, list_id, page)
        if got is None:
            return None
        rows, total = got
        for t in rows:
            if marker in str(t.get("description") or "") and str(t.get("listTaskId") or list_id) == list_id:
                return t
        seen += len(rows)
        if seen >= total:
            return {}
        if len(rows) < PAGE:
            return None                               # a short page before `total` = inconsistent answer
    return None
