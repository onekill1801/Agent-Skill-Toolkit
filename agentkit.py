#!/usr/bin/env python3
"""agentkit — one entry point for every tool in this toolkit.

Why this exists
---------------
The tools are split across `.claude/skills/*/tools/`, but they are one library,
not nine: `auto-dev` alone reaches into four other skills. Today each tool
re-discovers the layout with its own hand-copied `sys.path.insert` block (~45
copies). That works, but it means the directory layout is hard-coded in 45
places, so nothing can move.

This module is the ONE place that knows the layout. Tools keep working exactly
as before when called directly; called through here they get a correct path set
for free, and a later reorganisation only has to update this file.

Name collisions are real (`config.py` exists in three skills). The owning
skill's tools directory is therefore placed FIRST on sys.path, so a tool always
imports its own `config`, never a sibling's.

Usage
-----
    python agentkit.py <tool> [args...]        # e.g. run_log list --open
    python agentkit.py <skill>:<tool> [args]   # e.g. atask-automation:config
    python agentkit.py --list [substring]      # what can I call?
    python agentkit.py --where <tool>          # which file would run?

Stdlib only. Windows: `python` · macOS/Linux: `python3`.
"""

import os
import runpy
import sys

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

ROOT = os.path.dirname(os.path.abspath(__file__))
SKILLS_DIR = os.path.join(ROOT, ".claude", "skills")


def tool_dirs():
    """Every `<skill>/tools` directory, sorted by skill name."""
    if not os.path.isdir(SKILLS_DIR):
        return []
    out = []
    for skill in sorted(os.listdir(SKILLS_DIR)):
        d = os.path.join(SKILLS_DIR, skill, "tools")
        if os.path.isdir(d):
            out.append((skill, d))
    return out


def tool_index():
    """{tool_name: [(skill, path), ...]} — a list because names collide."""
    index = {}
    for skill, d in tool_dirs():
        for fn in sorted(os.listdir(d)):
            if fn.endswith(".py") and not fn.startswith("_"):
                index.setdefault(fn[:-3], []).append((skill, os.path.join(d, fn)))
    return index


def resolve(name, index=None):
    """'run_log' or 'dev-automation:config' -> (skill, path). Raises on ambiguity."""
    index = index if index is not None else tool_index()
    skill_hint, _, tool = name.rpartition(":")
    hits = index.get(tool, [])
    if skill_hint:
        hits = [h for h in hits if h[0] == skill_hint]
        if not hits:
            raise ValueError(f"no tool '{tool}' in skill '{skill_hint}'")
    if not hits:
        raise ValueError(f"unknown tool '{tool}' (try --list)")
    if len(hits) > 1:
        opts = ", ".join(f"{s}:{tool}" for s, _ in hits)
        raise ValueError(f"'{tool}' exists in several skills — qualify it: {opts}")
    return hits[0]


def build_path(owner_dir):
    """Owner directory first, then every other tools dir. Order is the contract."""
    others = [d for _, d in tool_dirs() if d != owner_dir]
    return [owner_dir] + others


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__.strip())
        return 0

    if argv[0] == "--list":
        needle = argv[1] if len(argv) > 1 else ""
        for tool, hits in sorted(tool_index().items()):
            if needle and needle not in tool:
                continue
            for skill, _ in hits:
                label = f"{skill}:{tool}" if len(hits) > 1 else tool
                print(f"  {label:<34} {skill}")
        return 0

    if argv[0] == "--where":
        if len(argv) < 2:
            print("--where needs a tool name", file=sys.stderr)
            return 2
        try:
            skill, path = resolve(argv[1])
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 2
        print(path)
        return 0

    try:
        skill, path = resolve(argv[0])
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2

    for d in reversed(build_path(os.path.dirname(path))):
        if d in sys.path:
            sys.path.remove(d)
        sys.path.insert(0, d)

    sys.argv = [path] + argv[1:]
    runpy.run_path(path, run_name="__main__")
    return 0


if __name__ == "__main__":
    sys.exit(main())
