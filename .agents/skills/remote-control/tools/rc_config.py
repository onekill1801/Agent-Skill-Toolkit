#!/usr/bin/env python3
"""Config loader for the remote-control skill (Telegram bridge + SSH fan-out).

Mirrors dev-automation/tools/config.py: reads from environment or the repo-root
.env file, stdlib only. Also exposes repo-root discovery so the bridge and the
approval hook agree on where temp/ and work/ live.
"""

import os
import re
import sys

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

_ENV_LOADED = False
_REPO_ROOT = None


def _strip_inline_comment(value: str) -> str:
    value = value.strip()
    if not value or value[0] in "\"'":
        return value.strip("\"'")
    hash_at = value.find("#")
    if hash_at >= 0:
        value = value[:hash_at]
    return value.strip()


def repo_root() -> str:
    """Find the repo root (dir containing .env, walking up from this file)."""
    global _REPO_ROOT
    if _REPO_ROOT:
        return _REPO_ROOT
    search = os.path.dirname(os.path.abspath(__file__))
    for _ in range(8):
        if os.path.isfile(os.path.join(search, ".env")) or \
           os.path.isdir(os.path.join(search, ".git")):
            _REPO_ROOT = search
            return _REPO_ROOT
        parent = os.path.dirname(search)
        if parent == search:
            break
        search = parent
    _REPO_ROOT = os.getcwd()
    return _REPO_ROOT


def _load_dotenv():
    global _ENV_LOADED
    if _ENV_LOADED:
        return
    _ENV_LOADED = True
    env_path = os.path.join(repo_root(), ".env")
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


def get_list(key: str) -> list:
    """Comma/space separated env value -> list of trimmed non-empty strings."""
    raw = get(key)
    if not raw:
        return []
    return [p.strip() for p in re.split(r"[,\s]+", raw) if p.strip()]


def work_dir() -> str:
    return get("WORK_DIR") or os.path.join(repo_root(), "work")


def temp_dir() -> str:
    d = os.path.join(repo_root(), "temp", "tg_approvals")
    os.makedirs(d, exist_ok=True)
    return d
