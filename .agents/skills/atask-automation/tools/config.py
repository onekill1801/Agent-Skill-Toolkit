#!/usr/bin/env python3
"""Configuration loader for atask-automation tools.

Reads settings from environment variables or a .env file in the project root.
Zero external dependencies — uses only Python stdlib.

Usage:
  python3 config.py          # validate and print current settings
"""

import os
import re
import sys

# Cross-platform: force UTF-8 stdout/stderr so JSON output with non-ASCII (Vietnamese
# task names, comments) doesn't crash on a Windows cp1252/cp437 console. Runs once on
# import; every atask tool imports this via client.py.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

_ENV_LOADED = False


def _strip_inline_comment(value: str) -> str:
    """Remove trailing # comment from .env values (not inside quotes)."""
    value = value.strip()
    if not value or value[0] in "\"'":
        return value.strip("\"'")
    hash_at = value.find("#")
    if hash_at >= 0:
        value = value[:hash_at]
    return value.strip()


def _load_dotenv():
    """Parse .env file and inject into os.environ (only once)."""
    global _ENV_LOADED
    if _ENV_LOADED:
        return
    _ENV_LOADED = True

    env_path = os.path.join(os.getcwd(), ".env")
    if not os.path.isfile(env_path):
        search = os.path.dirname(os.path.abspath(__file__))
        for _ in range(6):
            candidate = os.path.join(search, ".env")
            if os.path.isfile(candidate):
                env_path = candidate
                break
            search = os.path.dirname(search)
        else:
            return

    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            match = re.match(r'^([A-Za-z_]\w*)=(.*)$', line)
            if match:
                key = match.group(1)
                value = _strip_inline_comment(match.group(2))
                os.environ.setdefault(key, value)


def get(key: str, default: str = "") -> str:
    """Get a config value from environment, loading .env if needed."""
    _load_dotenv()
    return os.environ.get(key, default).strip()


def base_url() -> str:
    """Return ATASK_BASE_URL with trailing slash stripped."""
    return get("ATASK_BASE_URL", "http://localhost:8080").strip().rstrip("/")


def pat_token() -> str:
    return get("ATASK_PAT_TOKEN").strip()


def pat_header_name() -> str:
    """HTTP header name for PAT authentication (default: X-aTask-PAT)."""
    return get("ATASK_PAT_HEADER", "X-aTask-PAT").strip()


def verify_ssl() -> bool:
    return get("ATASK_VERIFY_SSL", "true").lower() not in ("false", "0", "no")


def validate() -> list:
    """Return list of missing required config keys."""
    required = ["ATASK_BASE_URL", "ATASK_PAT_TOKEN"]
    return [k for k in required if not get(k)]


if __name__ == "__main__":
    missing = validate()
    if missing:
        print(f"[ERROR] Missing required config: {', '.join(missing)}")
        print("  Set these in your .env file:")
        print("    ATASK_BASE_URL=https://atask.example.com")
        print("    ATASK_PAT_TOKEN=<your-personal-access-token>")
    else:
        print("[OK] Config valid.")
        print(f"  ATASK_BASE_URL   = {base_url()}")
        print(f"  ATASK_PAT_TOKEN  = {'*' * 8}...{pat_token()[-4:] if len(pat_token()) > 4 else '****'}")
        print(f"  ATASK_VERIFY_SSL = {verify_ssl()}")
