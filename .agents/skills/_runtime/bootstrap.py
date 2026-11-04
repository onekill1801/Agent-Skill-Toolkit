"""The ONLY place that edits sys.path for the toolkit runtime package.

Skill tools (facades) activate it by path, from any cwd / worktree / installed plugin:

    from pathlib import Path
    import runpy
    runpy.run_path(str(Path(__file__).resolve().parents[2] / "_runtime" / "bootstrap.py"))["activate"]()
    from agent_skill_toolkit.core import store

This directory has no SKILL.md on purpose: it is runtime code, not a skill
(verified: `claude plugin validate` + `--plugin-dir` load, docs/design/agent-ecosystem.md §19).
"""

import os
import sys

RUNTIME_DIR = os.path.dirname(os.path.abspath(__file__))
PACKAGE = "agent_skill_toolkit"


def activate() -> str:
    """Put this _runtime dir first on sys.path; refuse a same-named package from elsewhere."""
    loaded = sys.modules.get(PACKAGE)
    if loaded is not None:
        where = os.path.dirname(os.path.dirname(os.path.abspath(getattr(loaded, "__file__", "") or "")))
        if os.path.normcase(where) != os.path.normcase(RUNTIME_DIR):
            raise ImportError(f"{PACKAGE} already imported from {where}, not {RUNTIME_DIR}")
        return RUNTIME_DIR
    if RUNTIME_DIR not in sys.path:
        sys.path.insert(0, RUNTIME_DIR)
    return RUNTIME_DIR
