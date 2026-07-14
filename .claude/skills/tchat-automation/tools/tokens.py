#!/usr/bin/env python3
"""Token store + auto-refresh for TChat.

Access token (~1 month) is refreshed using the refresh token (~2 months) via
POST /auth/refresh-tokens. The rotated tokens are written straight back into the
project .env (FCHAT_BEARER_TOKEN / FCHAT_REFRESH_TOKEN) so the whole config lives
in ONE file — copy .env to another machine and auto-refresh just continues.

  access:  FCHAT_BEARER_TOKEN
  refresh: FCHAT_REFRESH_TOKEN

Internal module — imported by client.py / auth.py.
"""

import base64
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.request

import config


def _update_env(updates: dict):
    """Update specific KEY=value lines in .env in place (preserve everything else)."""
    path = config.dotenv_path()
    lines = []
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    seen = set()
    for i, line in enumerate(lines):
        m = re.match(r'^(\s*)([A-Za-z_]\w*)=', line)
        if m and m.group(2) in updates:
            lines[i] = f"{m.group(2)}={updates[m.group(2)]}"
            seen.add(m.group(2))
    for key, val in updates.items():
        if key not in seen:
            lines.append(f"{key}={val}")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def _save(access: str, refresh: str):
    try:
        _update_env({"FCHAT_BEARER_TOKEN": access, "FCHAT_REFRESH_TOKEN": refresh})
    except Exception as e:
        print(f"[WARN] could not write tokens to .env: {e}", file=sys.stderr)
    # Reflect into the live process so the current run uses the new values.
    os.environ["FCHAT_BEARER_TOKEN"] = access
    os.environ["FCHAT_REFRESH_TOKEN"] = refresh


def current_access() -> str:
    return config.bearer_token().strip()


def current_refresh() -> str:
    return config.get("FCHAT_REFRESH_TOKEN").strip()


def decode(jwt: str) -> dict:
    try:
        payload = jwt.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def exp(jwt: str):
    return decode(jwt).get("exp")


def is_expiring(jwt: str, buffer_sec: int = 300) -> bool:
    """True if token is missing, undecodable, or expires within buffer_sec."""
    e = exp(jwt)
    if not e:
        return True
    return (e - time.time()) < buffer_sec


def device_id() -> str:
    return decode(current_access()).get("data", {}).get("device", {}).get("deviceId", "")


def _ssl_ctx():
    ctx = ssl.create_default_context()
    if not config.verify_ssl():
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def refresh(verbose: bool = True) -> dict:
    """POST /auth/refresh-tokens with the refresh token; cache + return new tokens.

    Body/response shapes are confirmed against the live endpoint (see auth.py probe).
    Returns {"access_token", "refresh_token"} on success or {"error", "message"}.
    """
    rt = current_refresh()
    if not rt:
        return {"error": True, "message": "no refresh token; set FCHAT_REFRESH_TOKEN in .env"}

    url = f"{config.base_url()}/auth/refresh-tokens"
    body = json.dumps({"refreshToken": rt}).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "x-app": config.x_app(),
        "x-lang": config.lang(),
        "Origin": config.web_origin(),
        "Referer": config.web_origin() + "/",
    }
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, context=_ssl_ctx()) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        txt = ""
        try:
            txt = e.read().decode("utf-8")
        except Exception:
            pass
        return {"error": True, "status": e.code, "message": txt or str(e)}
    except urllib.error.URLError as e:
        return {"error": True, "status": 0, "message": str(e.reason)}

    # Verified response shape: {"access":{"token":...}, "refresh":{"token":...}}.
    # Also accept flat fallbacks just in case the API changes.
    d = data.get("data") or data
    access = (d.get("access", {}).get("token") if isinstance(d.get("access"), dict) else None) \
        or d.get("accessToken") or d.get("access_token")
    new_rt = (d.get("refresh", {}).get("token") if isinstance(d.get("refresh"), dict) else None) \
        or d.get("refreshToken") or d.get("refresh_token") or rt
    if not access:
        return {"error": True, "message": f"no access token in response: {json.dumps(data)[:300]}"}
    # Save IMMEDIATELY — refresh tokens are single-use/rotating; losing the new
    # one locks out further refreshes.
    _save(access, new_rt)
    if verbose:
        print("[OK] token refreshed; cache updated.", file=sys.stderr)
    return {"access_token": access, "refresh_token": new_rt}


def ensure_fresh(buffer_sec: int = 300) -> str:
    """Return a valid access token, refreshing proactively if near expiry."""
    acc = current_access()
    if is_expiring(acc, buffer_sec):
        r = refresh()
        if not r.get("error"):
            return r["access_token"]
    return acc


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "status":
        acc = current_access()
        e = exp(acc)
        left = int(e - time.time()) if e else None
        print(json.dumps({
            "has_access": bool(acc),
            "has_refresh": bool(current_refresh()),
            "access_exp": e,
            "access_seconds_left": left,
            "expiring_soon": is_expiring(acc),
            "device_id": device_id(),
        }, indent=2))
    elif cmd == "refresh":
        import client
        client.print_json(refresh())
