"""The small YAML subset used by `.agent/project.yml`: nested maps, lists of scalars, scalars.
No anchors, no multiline, no list of maps (model those as a map keyed by name; key order is kept).

Same subset as auto-dev/tools/agent_init.py (yaml_dump / yaml_load) — agent_init moves onto this
module in R.M3; until then keep the two in sync.
"""

import re


def _unscalar(s):
    s = s.strip()
    if s in ("null", "~", ""):
        return None
    if s in ("true", "false"):
        return s == "true"
    if len(s) >= 2 and s[0] == s[-1] == '"':
        return s[1:-1].replace('\\"', '"')
    if re.fullmatch(r"-?\d+", s):
        return int(s)
    return s


def load(text: str) -> dict:
    root = {}
    stack = [(-1, root)]
    pending = None
    for raw in text.split("\n"):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        line = raw.strip()
        if line.startswith("- "):
            if pending is None or indent <= pending[0]:
                raise ValueError(f"list item without a key: {raw!r}")
            _, parent, key = pending
            if not isinstance(parent.get(key), list):
                parent[key] = []
            parent[key].append(_unscalar(line[2:]))
            continue
        if pending is not None and indent > pending[0]:
            p_indent, parent, key = pending
            child = {}
            parent[key] = child
            stack.append((p_indent, child))
        pending = None
        while len(stack) > 1 and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]
        key, _, rest = line.partition(":")
        key, rest = key.strip(), rest.strip()
        if not key:
            raise ValueError(f"line is not a key: {raw!r}")
        if rest == "":
            parent[key] = {}
            pending = (indent, parent, key)
        elif rest == "[]":
            parent[key] = []
        elif rest == "{}":
            parent[key] = {}
        else:
            parent[key] = _unscalar(rest)
    return root
