#!/usr/bin/env python3
"""Shared HTTP client for the TChat REST API (base URL from FCHAT_BASE_API_URL).

Auth model (verified from authenticated traffic capture):
  - Authorization: Bearer <JWT>
  - Required custom headers: x-app, x-lang, x-request-id
  - Origin: <FCHAT_WEB_ORIGIN in .env>

Internal module — imported by users.py / groups.py / messages.py / todos.py /
auth.py. Not meant to be run directly.
"""

import json
import ssl
import sys
import uuid
import urllib.error
import urllib.parse
import urllib.request

import config
import tokens

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36")


def _ssl_ctx():
    ctx = ssl.create_default_context()
    if not config.verify_ssl():
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        print("[WARN] SSL verification disabled (FCHAT_VERIFY_SSL=false)", file=sys.stderr)
    return ctx


def _headers(auth=True, content_type=None) -> dict:
    h = {
        "Accept": "application/json",
        "x-app": config.x_app(),
        "x-lang": config.lang(),
        "x-request-id": str(uuid.uuid4()),
        "Origin": config.web_origin(),
        "Referer": config.web_origin() + "/",
        "User-Agent": _UA,
    }
    if auth:
        token = tokens.ensure_fresh()   # proactively refresh if near expiry
        if not token:
            print("[ERROR] FCHAT_BEARER_TOKEN is not set. Run: python config.py", file=sys.stderr)
            sys.exit(1)
        h["Authorization"] = f"Bearer {token}"
    if content_type:
        h["Content-Type"] = content_type
    return h


def _http(url: str, method: str = "GET", payload=None, auth=True, _retried=False) -> dict:
    ct = "application/json" if payload is not None else None
    headers = _headers(auth=auth, content_type=ct)
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)

    try:
        with urllib.request.urlopen(req, context=_ssl_ctx()) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        # On 401, try a one-time token refresh and retry the request.
        if e.code == 401 and auth and not _retried:
            r = tokens.refresh()
            if not r.get("error"):
                return _http(url, method=method, payload=payload, auth=auth, _retried=True)
        text = ""
        try:
            text = e.read().decode("utf-8")
        except Exception:
            pass
        hint = ""
        if e.code == 401:
            hint = " — token expired and refresh failed; update FCHAT_REFRESH_TOKEN in .env."
        elif e.code == 429:
            ra = e.headers.get("Retry-After", "?")
            hint = f" — rate limited; back off (Retry-After={ra}s)."
        return {"error": True, "status": e.code, "message": (text or str(e)) + hint}
    except urllib.error.URLError as e:
        return {"error": True, "status": 0, "message": str(e.reason)}


def _qs(params: dict) -> str:
    clean = {k: v for k, v in (params or {}).items() if v is not None and v != ""}
    return ("?" + urllib.parse.urlencode(clean)) if clean else ""


def api_get(path: str, params: dict = None, auth=True) -> dict:
    return _http(f"{config.base_url()}{path}{_qs(params)}", method="GET", auth=auth)


def api_post(path: str, payload: dict = None, auth=True) -> dict:
    return _http(f"{config.base_url()}{path}", method="POST", payload=payload or {}, auth=auth)


def print_json(data):
    """Write JSON to stdout; UTF-8 safe on Windows."""
    out = json.dumps(data, indent=2, ensure_ascii=False)
    sys.stdout.buffer.write(out.encode("utf-8"))
    sys.stdout.buffer.write(b"\n")


def check_error(result: dict, tool_name: str):
    if isinstance(result, dict) and result.get("error"):
        code = result.get("status", "ERROR")
        print(f"[ERROR] {tool_name} failed ({code}): {result.get('message')}", file=sys.stderr)
        sys.exit(1)
