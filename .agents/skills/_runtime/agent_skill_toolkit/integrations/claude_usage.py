"""Claude subscription usage of ONE account, read directly from Anthropic (checklist F.15).

For each account's config dir (CLAUDE_CONFIG_DIR, else <HOME>/.claude) this reads the OAuth ACCESS
token of `.credentials.json` and GETs the usage endpoint the Claude apps use:

    GET https://api.anthropic.com/api/oauth/usage      (CLAUDE_USAGE_URL overrides; https only)
    -> {"five_hour": {"utilization": 5.0, "resets_at": "...Z"}, "seven_day": {...}, ...}

[Unverified] The endpoint is not publicly documented; the shape above was observed on 2026-10-15.

Read-only and deliberately limited:
  * an EXPIRED access token is reported ("expired"), never refreshed — refreshing rewrites the
    credentials file Orca / the Claude CLI also rewrite (that race is the deferred rotation work);
  * the token stays in this process: only numbers come back, error texts never contain it;
  * verified TLS only (core.tls), no redirects (a redirect would carry the bearer elsewhere).
"""

import datetime
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from agent_skill_toolkit.core import env, tls

DEFAULT_URL = "https://api.anthropic.com/api/oauth/usage"
TIMEOUT_S = 10              # WHOLE request (connect + headers + body), not per socket read
MAX_BYTES = 256 * 1024      # the real answer is ~2 KB
USER_AGENT = "agent-skill-toolkit"


def usage_url() -> str:
    url = env.get("CLAUDE_USAGE_URL", DEFAULT_URL) or DEFAULT_URL
    p = urllib.parse.urlsplit(url)
    if p.scheme != "https" or p.username or p.password or not p.hostname:
        raise ValueError("CLAUDE_USAGE_URL must be a plain https URL")
    return url


def config_dir(account_env: dict) -> str:
    """Where the Claude CLI of this account keeps `.credentials.json` (same rules as the CLI)."""
    e = account_env or {}
    if e.get("CLAUDE_CONFIG_DIR"):
        return os.path.expanduser(e["CLAUDE_CONFIG_DIR"])
    home = e.get("USERPROFILE") or e.get("HOME")
    return os.path.join(os.path.expanduser(home) if home else os.path.expanduser("~"), ".claude")


def read_access(directory: str, now: float = None) -> dict:
    """{"token", "expires_at"} or {"error": missing|unreadable|no_token|expired, "expires_at"?}."""
    path = os.path.join(directory, ".credentials.json")
    if not os.path.isfile(path):
        return {"error": "missing"}
    if os.path.islink(path):
        return {"error": "unreadable"}
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return {"error": "unreadable"}
    oauth = doc.get("claudeAiOauth") if isinstance(doc, dict) else None
    if not isinstance(oauth, dict):                          # shape checked, never an AttributeError (R1 #4)
        return {"error": "unreadable"}
    token, exp = oauth.get("accessToken"), oauth.get("expiresAt")
    if not isinstance(token, str) or not token:
        return {"error": "no_token"}
    exp_s = exp / 1000 if isinstance(exp, (int, float)) and not isinstance(exp, bool) else 0
    if exp_s <= (now or time.time()) + 30:
        return {"error": "expired", "expires_at": exp_s or None}
    return {"token": token, "expires_at": exp_s}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None                                       # -> HTTPError 30x, the bearer goes nowhere else


def _bucket(b, now):
    if not isinstance(b, dict):
        return None
    used = b.get("utilization")
    if isinstance(used, bool) or not isinstance(used, (int, float)) or not 0 <= used <= 100:
        return None
    resets = None
    if isinstance(b.get("resets_at"), str):
        try:
            resets = datetime.datetime.fromisoformat(b["resets_at"].replace("Z", "+00:00")).timestamp()
        except ValueError:
            resets = None
    return {"used": used, "left": max(0, 100 - used), "resets_at": resets}


def parse(data: dict, now: float = None) -> dict:
    """Endpoint JSON -> the same shape as workflows.execution.accounts uses for Orca readings."""
    now = now or time.time()
    if not isinstance(data, dict):
        raise ValueError("usage response is not an object")
    out = {"session": _bucket(data.get("five_hour"), now),
           "weekly": _bucket(data.get("seven_day"), now), "age_min": 0}
    if out["session"] is None and out["weekly"] is None:
        raise ValueError("no usage buckets in the response")
    return out


def fetch(token: str, timeout: float = TIMEOUT_S) -> dict:
    req = urllib.request.Request(usage_url(), headers={
        "Authorization": "Bearer " + token, "anthropic-beta": "oauth-2025-04-20",
        "Accept": "application/json", "User-Agent": USER_AGENT})
    opener = urllib.request.build_opener(_NoRedirect(), urllib.request.HTTPSHandler(context=tls.context()))
    deadline = time.monotonic() + timeout
    with opener.open(req, timeout=timeout) as r:
        body = read_body(r, deadline)
    return json.loads(body.decode("utf-8"))


def read_body(r, deadline: float, max_bytes: int = MAX_BYTES) -> bytes:
    """Bounded body under an overall monotonic deadline. read1() returns what ONE socket read gives
    (read(n) would wait for n bytes — a server trickling a byte at a time could hold it far past the
    deadline: Codex usage R2 #1), and each socket read is itself bounded by the request timeout."""
    read = getattr(r, "read1", None) or r.read
    chunks, size = [], 0
    while True:
        if time.monotonic() > deadline:
            raise TimeoutError("usage request took longer than its deadline")
        b = read(8192)
        if not b:
            return b"".join(chunks)
        size += len(b)
        if size > max_bytes:
            raise ValueError("usage response too large")
        chunks.append(b)


def usage_for(account_env: dict, timeout: float = TIMEOUT_S, now: float = None) -> dict:
    """{"usage": {...}, "at": t} or {"error": <short reason>, "expires_at"?}. Never raises, never
    returns the token or a server body."""
    now = now or time.time()
    try:
        usage_url()
    except ValueError:
        return {"error": "bad_url"}
    acc = read_access(config_dir(account_env), now)
    if "error" in acc:
        return {"error": acc["error"], **({"expires_at": acc["expires_at"]} if acc.get("expires_at") else {})}
    try:
        return {"usage": parse(fetch(acc["token"], timeout), now), "at": now}
    except urllib.error.HTTPError as e:
        return {"error": f"http_{e.code}"}
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", e)
        return {"error": "tls" if "CERTIFICATE" in str(reason).upper() else "network"}
    except ValueError:
        return {"error": "bad_response"}
    except OSError:                                       # timeouts, resets
        return {"error": "network"}
