""".env loading for the runtime (checklist R.M3) — the same rules as dev-automation/tools/config.py, so a
runtime module never has to import a skill's config facade:

  * the process environment always wins (values from .env are only `setdefault`)
  * file: $TOOLKIT_ENV_FILE if set, else ./.env (cwd), else the SAME five places the dev-automation
    loader looks at: dev-automation/tools, dev-automation, skills, .claude, the toolkit root — never a
    parent of the toolkit (Codex R.M3 #2)
  * a key repeated in the file: the FIRST occurrence wins (like the original's per-line setdefault)
  * `KEY=value`, `#` comments, inline `# comment` outside quotes, surrounding quotes stripped

    from agent_skill_toolkit.core import env
    env.get("GITLAB_URL")
"""

import os
import re
import time

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
    root = paths.toolkit_root()
    search = os.path.join(root, ".claude", "skills", "dev-automation", "tools")
    for _ in range(5):
        candidate = os.path.join(search, ".env")
        if os.path.isfile(candidate):
            return candidate
        search = os.path.dirname(search)
    return ""


def parse(text: str) -> dict:
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = _LINE.match(line)
        if m:
            out.setdefault(m.group(1), _strip_inline_comment(m.group(2)))     # first occurrence wins (#3)
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


# ----------------------------------------------------------------------------- writing (settings UI)

# chars every .env reader of the toolkit would mis-read: line breaks of any kind (str.splitlines()
# splits on these), "#" (inline comment), quotes (stripped), control chars
class StaleValue(RuntimeError):
    """A key's current value is not the one the caller based its change on."""

    def __init__(self, keys):
        super().__init__(", ".join(keys))
        self.keys = list(keys)


_BAD_VALUE = re.compile(r"[\x00-\x1f\x7f\x85  #\"']")
MAX_VALUE = 500


def check_value(value) -> str:
    """'' when `value` survives a write/parse round trip unchanged in every toolkit .env reader, else why not."""
    if not isinstance(value, str):
        return "value must be text"
    if len(value) > MAX_VALUE:
        return f"value longer than {MAX_VALUE} characters"
    if value != value.strip():
        return "value starts or ends with a space"
    if _BAD_VALUE.search(value):
        return "value contains a line break, #, a quote or a control character"
    return ""


def _set_line(line: str, key: str, value: str) -> str:
    """`line` (an active `KEY=...` line) with its value replaced; indentation, an inline `# comment`
    and the line ending are kept."""
    body = line.rstrip("\r\n")
    eol = line[len(body):]
    indent = body[:len(body) - len(body.lstrip())]
    raw = _LINE.match(body.strip()).group(2)
    comment = ""
    if raw.strip() and raw.strip()[0] not in "\"'":
        h = raw.find("#")
        if h >= 0:
            pre = raw[:h]
            comment = (pre[len(pre.rstrip()):] or " ") + raw[h:].rstrip()
    return f"{indent}{key}={value}{comment}{eol}"


def set_values(path: str, updates: dict, expected: dict = None) -> dict:
    """Write `updates` {KEY: value} into the .env file at `path` — the FIRST active `KEY=` line (the one
    every reader uses) is changed in place, a missing key is appended; every other byte is kept. Atomic
    (temp file + replace) under a lock; the result is re-parsed and must give exactly the new values with
    every other key unchanged, or nothing is written. `expected` {KEY: value-or-""}: checked UNDER the lock
    (Codex settings R1 #1) — a differing current value raises StaleValue. Returns the previous values."""
    from . import locks
    for k, v in updates.items():
        if not _LINE.match(f"{k}=x"):
            raise ValueError(f"invalid key: {k!r}")
        why = check_value(v)
        if why:
            raise ValueError(f"{k}: {why}")
    with locks.file_lock(paths.state_path("locks", "env-file.lock"), timeout=10):
        with open(path, encoding="utf-8", newline="") as f:
            text = f.read()
        before = parse(text)
        stale = [k for k, v in (expected or {}).items() if before.get(k, "") != v]
        if stale:
            raise StaleValue(stale)
        lines = text.splitlines(keepends=True)
        nl = "\r\n" if "\r\n" in text else "\n"
        for k, v in updates.items():
            for i, line in enumerate(lines):
                s = line.strip()
                m = _LINE.match(s) if s and not s.startswith("#") else None
                if m and m.group(1) == k:
                    lines[i] = _set_line(line, k, v)
                    break
            else:
                if lines and not lines[-1].endswith(("\n", "\r")):
                    lines[-1] += nl
                lines.append(f"{k}={v}{nl}")
        new = "".join(lines)
        after = parse(new)
        if {k: after.get(k) for k in updates} != dict(updates) or \
                {k: v for k, v in after.items() if k not in updates} != {k: v for k, v in before.items() if k not in updates}:
            raise RuntimeError("the edited .env would not read back as intended — nothing written")
        tmp = f"{path}.tmp-{os.getpid()}"
        try:
            with open(tmp, "w", encoding="utf-8", newline="") as f:
                f.write(new)
            for attempt in range(20):
                try:
                    os.replace(tmp, path)
                    break
                except PermissionError:
                    # Windows: a reader holds the file open without delete sharing — readers keep it
                    # open for milliseconds, so retry briefly (Codex settings R1 #2)
                    if attempt == 19:
                        raise
                    time.sleep(0.1)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
    # running processes keep what they loaded at start (os.environ): the caller tells which to restart
    return {k: before.get(k) for k in updates}
