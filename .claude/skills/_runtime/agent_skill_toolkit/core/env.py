""".env loading for the runtime (checklist R.M3) — the same rules as dev-automation/tools/config.py, so a
runtime module never has to import a skill's config facade:

  * the process environment always wins (values from .env are only `setdefault`)
  * file: $TOOLKIT_ENV_FILE if set, else ./.env (cwd), else the first .env found walking up from the
    toolkit root (5 levels)
  * `KEY=value`, `#` comments, inline `# comment` outside quotes, surrounding quotes stripped

    from agent_skill_toolkit.core import env
    env.get("GITLAB_URL")
"""

import os
import re

from . import paths

_LOADED = {"path": None}
_LINE = re.compile(r"^([A-Za-z_]\w*)=(.*)$")


def _strip_inline_comment(value: str) -> str:
    value = value.strip()
    if not value or value[0] in "\"'":
        return value.strip("\"'")
    hash_at = value.find("#")
    if hash_at >= 0:
        value = value[:hash_at]
    return value.strip()


def find_env_file() -> str:
    explicit = os.environ.get("TOOLKIT_ENV_FILE", "").strip()
    if explicit:
        return explicit if os.path.isfile(explicit) else ""
    cwd = os.path.join(os.getcwd(), ".env")
    if os.path.isfile(cwd):
        return cwd
    search = paths.toolkit_root()
    for _ in range(5):
        candidate = os.path.join(search, ".env")
        if os.path.isfile(candidate):
            return candidate
        parent = os.path.dirname(search)
        if parent == search:
            break
        search = parent
    return ""


def parse(text: str) -> dict:
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = _LINE.match(line)
        if m:
            out[m.group(1)] = _strip_inline_comment(m.group(2))
    return out


def load(force: bool = False) -> str:
    """Inject the .env values into os.environ once (setdefault: the environment wins). Returns its path."""
    if _LOADED["path"] is not None and not force:
        return _LOADED["path"]
    path = find_env_file()
    _LOADED["path"] = path
    if path:
        with open(path, encoding="utf-8") as f:
            for k, v in parse(f.read()).items():
                os.environ.setdefault(k, v)
    return path


def get(key: str, default: str = "") -> str:
    load()
    return os.environ.get(key, default).strip()
