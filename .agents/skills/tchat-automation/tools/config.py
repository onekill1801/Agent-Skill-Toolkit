#!/usr/bin/env python3
"""Configuration loader for tchat-automation tools.

Reads settings from environment variables or a .env file in the project root.
Zero external dependencies — Python stdlib only.

Usage:
  python config.py          # validate and print current settings (Windows)
  python3 config.py         # macOS / Linux
"""

import os
import re
import sys

# Cross-platform: force UTF-8 stdout/stderr so JSON with non-ASCII (Vietnamese
# names, group titles) doesn't crash on a Windows cp1252 console. Runs on import;
# every tchat tool imports this via client.py.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

_ENV_LOADED = False


def _strip_inline_comment(value: str) -> str:
    value = value.strip()
    if not value or value[0] in "\"'":
        return value.strip("\"'")
    hash_at = value.find("#")
    if hash_at >= 0:
        value = value[:hash_at]
    return value.strip()


def dotenv_path() -> str:
    """Resolve the .env path: cwd first, else search up to 6 dirs upward from here.

    Returns the cwd/.env path (which may not exist) if none is found, so callers
    can create it.
    """
    env_path = os.path.join(os.getcwd(), ".env")
    if os.path.isfile(env_path):
        return env_path
    search = os.path.dirname(os.path.abspath(__file__))
    for _ in range(6):
        candidate = os.path.join(search, ".env")
        if os.path.isfile(candidate):
            return candidate
        search = os.path.dirname(search)
    return env_path


def _load_dotenv():
    """Parse .env (searching up to 6 dirs upward) into os.environ, once."""
    global _ENV_LOADED
    if _ENV_LOADED:
        return
    _ENV_LOADED = True

    env_path = dotenv_path()
    if not os.path.isfile(env_path):
        return

    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            match = re.match(r'^([A-Za-z_]\w*)=(.*)$', line)
            if match:
                os.environ.setdefault(match.group(1),
                                      _strip_inline_comment(match.group(2)))


def get(key: str, default: str = "") -> str:
    _load_dotenv()
    return os.environ.get(key, default).strip()


def base_url() -> str:
    """REST base, trailing slash stripped (set FCHAT_BASE_API_URL in .env)."""
    return get("FCHAT_BASE_API_URL").rstrip("/")


def web_origin() -> str:
    """Origin/Referer of the chat web app (set FCHAT_WEB_ORIGIN in .env)."""
    return get("FCHAT_WEB_ORIGIN").rstrip("/")


def bearer_token() -> str:
    return get("FCHAT_BEARER_TOKEN")


def x_app() -> str:
    """Value for the required `x-app` header. Copy from a real request in DevTools."""
    return get("FCHAT_X_APP")


def lang() -> str:
    return get("FCHAT_LANG", "vi")


def verify_ssl() -> bool:
    return get("FCHAT_VERIFY_SSL", "true").lower() not in ("false", "0", "no")


def ws_url() -> str:
    """SocketCluster realtime endpoint (set FCHAT_WS_URL in .env)."""
    return get("FCHAT_WS_URL").strip()


def ws_token() -> str:
    """JWT used as Sec-WebSocket-Protocol. Defaults to the REST bearer token."""
    return get("FCHAT_WS_TOKEN") or bearer_token()


def validate() -> list:
    """Required keys for read-only (Bearer) operation."""
    return [k for k in ("FCHAT_BASE_API_URL", "FCHAT_BEARER_TOKEN", "FCHAT_X_APP")
            if not get(k)]


if __name__ == "__main__":
    missing = validate()
    if missing:
        print(f"[ERROR] Missing required config: {', '.join(missing)}")
        print("  Set these in your .env file (repo root):")
        print("    FCHAT_BASE_API_URL=https://<chat-api-host>")
        print("    FCHAT_WEB_ORIGIN=https://<chat-web-host>")
        print("    FCHAT_BEARER_TOKEN=<JWT from a logged-in chat web session>")
        print("    FCHAT_X_APP=<value of the x-app request header, copy from DevTools>")
        print("    FCHAT_LANG=vi            # optional")
        print("    FCHAT_VERIFY_SSL=true    # optional")
        sys.exit(1)
    tok = bearer_token()
    print("[OK] Config valid.")
    print(f"  FCHAT_BASE_API_URL = {base_url()}")
    print(f"  FCHAT_BEARER_TOKEN = {'*' * 8}...{tok[-6:] if len(tok) > 6 else '****'}")
    print(f"  FCHAT_X_APP        = {x_app()}")
    print(f"  FCHAT_LANG         = {lang()}")
    print(f"  FCHAT_VERIFY_SSL   = {verify_ssl()}")
