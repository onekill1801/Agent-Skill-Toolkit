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
    """JSON request with the private token. Errors -> {"error": True, "status": code|None, "message": ...}."""
    headers = {"PRIVATE-TOKEN": env.get("GITLAB_PRIVATE_TOKEN"), "Content-Type": "application/json"}
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, context=_ssl_context(), timeout=timeout) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        return {"error": True, "status": e.code, "message": (e.read().decode(errors="replace") if e.fp else "")}
    except (urllib.error.URLError, OSError, ValueError) as e:
        return {"error": True, "status": None, "message": f"{type(e).__name__}: {getattr(e, 'reason', e)}"}


def project_info(project) -> dict:
    """project = numeric id or 'group/sub/repo' path."""
    return request(f"{base_url()}/projects/{urllib.parse.quote(str(project), safe='')}")


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

    def get_merge_request(self, mr_iid: int) -> dict:
        return request(self._api(f"/merge_requests/{int(mr_iid)}"))

    def add_mr_comment(self, mr_iid: int, body: str) -> dict:
        return request(self._api(f"/merge_requests/{int(mr_iid)}/notes"), method="POST", data={"body": body})
