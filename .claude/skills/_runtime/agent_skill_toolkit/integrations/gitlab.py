"""GitLab REST v4 client for the runtime (checklist R.M4): one PROJECT per client object, configuration from
core.env (GITLAB_URL, GITLAB_PRIVATE_TOKEN, SSL_VERIFY), stdlib only. Same call surface the mr-review tools
already use on dev-automation's gitlab_api module (get_merge_request, add_mr_comment, _api, _request), so a
caller switches by swapping the object, not its code.

Differences from gitlab_api (deliberate): an explicit project per client (no thread-local / env scoping),
every request has a timeout, network errors come back as the usual error dict instead of raising.

    from agent_skill_toolkit.integrations import gitlab
    gl = gitlab.Client(project_id)
    mr = gl.get_merge_request(12)          # dict, or {"error": True, "status": ..., "message": ...}
"""

import json
import ssl
import urllib.error
import urllib.parse
import urllib.request

from agent_skill_toolkit.core import env

TIMEOUT = 60


def base_url() -> str:
    return f"{env.get('GITLAB_URL', 'https://gitlab.com').rstrip('/')}/api/v4"


def _ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if env.get("SSL_VERIFY", "true").lower() in ("false", "0", "no"):
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def request(url: str, method: str = "GET", data: dict = None, timeout: float = TIMEOUT):
    """JSON request with the private token. Errors -> {"error": True, "status": code|None, "message": ...}.
    An EMPTY or non-JSON success body is an error, never `{}` (Codex R.M4 #1). For a write (non-GET), a
    failure after the request may have reached GitLab — network error, timeout, unreadable body — is
    flagged "uncertain": True: the caller must reconcile before retrying, not treat it as a rejection
    (Codex R.M4 #2). A definite HTTP rejection (4xx/5xx status) is never uncertain."""
    headers = {"PRIVATE-TOKEN": env.get("GITLAB_PRIVATE_TOKEN"), "Content-Type": "application/json"}
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    write = method.upper() != "GET"
    try:
        with urllib.request.urlopen(req, context=_ssl_context(), timeout=timeout) as resp:
            raw = resp.read().decode()
    except urllib.error.HTTPError as e:
        return {"error": True, "status": e.code, "message": (e.read().decode(errors="replace") if e.fp else "")}
    except (urllib.error.URLError, OSError) as e:
        return {"error": True, "status": None, "uncertain": write,
                "message": f"{type(e).__name__}: {getattr(e, 'reason', e)}"}
    try:
        if not raw.strip():
            raise ValueError("empty response body")
        return json.loads(raw)
    except ValueError as e:
        return {"error": True, "status": "bad-body", "uncertain": write, "message": str(e)[:200]}


def project_info(project) -> dict:
    """project = numeric id or 'group/sub/repo' path."""
    return request(f"{base_url()}/projects/{urllib.parse.quote(str(project), safe='')}")


def search_projects(name: str, limit: int = 10) -> list:
    """Find projects by name (membership scope). Returns a list of dicts, or an error dict."""
    q = urllib.parse.urlencode({"search": name, "per_page": limit, "membership": "true",
                                "order_by": "last_activity_at"})
    r = request(f"{base_url()}/projects?{q}")
    return r if isinstance(r, list) else r


class Client:
    """Calls for ONE project (the id is fixed at construction — thread-safe by design)."""

    def __init__(self, project_id):
        if project_id in (None, ""):
            raise ValueError("a GitLab project id is required")
        self.project_id = str(project_id)

    def _api(self, path: str) -> str:
        return f"{base_url()}/projects/{urllib.parse.quote(self.project_id, safe='')}{path}"

    def _request(self, url: str, method: str = "GET", data: dict = None):
        return request(url, method=method, data=data)

    def latest_commit(self, ref: str = None) -> dict:
        """Newest commit on `ref` (branch/tag/sha), or the default branch if ref is None.
        Returns the commit dict (id, title, message, author_name, created_at, ...) or an error dict."""
        path = "/repository/commits?per_page=1"
        if ref:
            path += "&ref_name=" + urllib.parse.quote(str(ref), safe="")
        r = request(self._api(path))
        if isinstance(r, dict) and r.get("error"):
            return r
        if isinstance(r, list) and r:
            return r[0]
        return {"error": True, "status": "empty", "message": f"no commits on ref {ref!r}"}

    def get_merge_request(self, mr_iid: int) -> dict:
        return request(self._api(f"/merge_requests/{int(mr_iid)}"))

    def add_mr_comment(self, mr_iid: int, body: str) -> dict:
        return request(self._api(f"/merge_requests/{int(mr_iid)}/notes"), method="POST", data={"body": body})
