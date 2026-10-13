"""Decisions only a human may take (approve a refactor plan, start / decide a proposal, accept an aTask
proposal). Enforced INSIDE the handler, not only by the Claude hook (Codex F.10b #5): every agent this
toolkit starts runs with TOOLKIT_HEADLESS=1 / TOOLKIT_AGENT_SANDBOXED=1 (agent_runner.agent_env), and a
human decision needs an interactive terminal.

Limit: an agent with a shell could unset variables / fake a TTY; the hard boundary is the separate OS
account of the executor (design §10.2, checklist F.6b).
"""

import os
import sys


def isatty() -> bool:
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def require(action: str) -> None:
    if os.environ.get("TOOLKIT_HEADLESS") == "1" or os.environ.get("TOOLKIT_AGENT_SANDBOXED") == "1":
        raise PermissionError(f"{action}: refused — this process runs under an agent; a human must do this")
    if not isatty():
        raise PermissionError(f"{action}: needs an interactive terminal (a human decision)")
