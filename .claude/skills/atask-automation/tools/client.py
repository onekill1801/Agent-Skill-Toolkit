#!/usr/bin/env python3
"""Shared HTTP client for aTask AI-agent REST API.

All tools route through POST /api/ai/execute (tool-registry endpoint).
Auth: PAT token via X-aTask-PAT header.

Usage (internal — imported by other tools, not run directly):
  from client import execute_tool, api_get, api_delete
"""

import json
import ssl
import sys
import urllib.error
import urllib.request

import config


# ── SSL context ────────────────────────────────────────────────────────────────

def _ssl_ctx():
    ctx = ssl.create_default_context()
    if not config.verify_ssl():
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        print("[WARN] SSL verification disabled (ATASK_VERIFY_SSL=false)", file=sys.stderr)
    return ctx


# ── Low-level HTTP ─────────────────────────────────────────────────────────────

def _http(url: str, method: str = "GET", payload=None, auth: str = "pat") -> dict:
    """Make an authenticated HTTP request; return parsed JSON dict.

    auth="pat"    -> X-aTask-PAT header (kênh /api/ai/execute và các path PAT-gated)
    auth="bearer" -> Authorization: Bearer <ATASK_BEARER_TOKEN> (REST trực tiếp như
                     GET /api/tasks/{id} — PAT bị 401 ở đó; JWT lấy từ session browser)
    """
    if auth == "bearer":
        token = config.get("ATASK_BEARER_TOKEN", "").strip()
        if not token:
            return {"error": True, "status": 401,
                    "message": "ATASK_BEARER_TOKEN chưa đặt trong .env — dán session JWT "
                               "từ browser (DevTools -> request bất kỳ -> header "
                               "Authorization: Bearer ...)"}
        headers = {"Authorization": f"Bearer {token}"}
    else:
        token = config.pat_token()
        if not token:
            print("[ERROR] ATASK_PAT_TOKEN is not set. Run: python3 config.py", file=sys.stderr)
            sys.exit(1)
        headers = {config.pat_header_name(): token}
    headers.update({
        "Content-Type": "application/json",
        "Accept": "application/json",
    })
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)

    try:
        with urllib.request.urlopen(req, context=_ssl_ctx()) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        body_text = ""
        try:
            body_text = e.read().decode("utf-8")
        except Exception:
            pass
        return {"error": True, "status": e.code, "message": body_text or str(e)}
    except urllib.error.URLError as e:
        return {"error": True, "status": 0, "message": str(e.reason)}


# ── Public helpers ─────────────────────────────────────────────────────────────

def execute_tool(tool_name: str, args: dict) -> dict:
    """Call POST /api/ai/execute with a named tool + args.

    Returns the ToolResult JSON from the server.
    """
    url = f"{config.base_url()}/api/ai/execute"
    payload = {"name": tool_name, "arguments": args}
    result = _http(url, method="POST", payload=payload)
    return result


def api_get(path: str) -> dict:
    """HTTP GET against a raw API path (used by auth.py)."""
    return _http(f"{config.base_url()}{path}", method="GET")


def api_get_user(path: str) -> dict:
    """HTTP GET với session Bearer (REST trực tiếp — nhiều field hơn kênh ai/execute)."""
    return _http(f"{config.base_url()}{path}", method="GET", auth="bearer")


def api_delete(path: str) -> dict:
    """HTTP DELETE against a raw API path (used by auth.py)."""
    return _http(f"{config.base_url()}{path}", method="DELETE")


def api_post(path: str, payload: dict) -> dict:
    """HTTP POST against a raw API path (used by auth.py for PAT bootstrap)."""
    return _http(f"{config.base_url()}{path}", method="POST", payload=payload)


# ── Output helpers ─────────────────────────────────────────────────────────────

def print_json(data):
    """Write JSON to stdout; UTF-8 safe on Windows."""
    output = json.dumps(data, indent=2, ensure_ascii=False)
    sys.stdout.buffer.write(output.encode("utf-8"))
    sys.stdout.buffer.write(b"\n")


def check_error(result, tool_name: str):
    """Print error and exit(1) if result contains an error flag.

    A non-dict result (e.g. a JSON list of PATs) is a successful data payload,
    not an error envelope — nothing to check.
    """
    if not isinstance(result, dict):
        return
    if result.get("error") or result.get("success") is False:
        code = result.get("errorCode") or result.get("status") or "ERROR"
        msg = result.get("errorMessage") or result.get("message") or str(result)
        print(f"[ERROR] {tool_name} failed ({code}): {msg}", file=sys.stderr)
        sys.exit(1)
