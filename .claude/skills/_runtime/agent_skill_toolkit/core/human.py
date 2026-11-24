"""Decisions only a human may take (approve a refactor plan, start / decide a proposal, accept an aTask
proposal). Enforced INSIDE the handler, not only by the Claude hook (Codex F.10b #5): every agent this
toolkit starts runs with TOOLKIT_HEADLESS=1 / TOOLKIT_AGENT_SANDBOXED=1 (agent_runner.agent_env), and a
human decision needs an interactive terminal.

Limit: an agent with a shell could unset variables / fake a TTY; the hard boundary is the separate OS
account of the executor (design §10.2, checklist F.6b).
"""

import contextlib
import os
import sys
import threading

_web = threading.local()


def isatty() -> bool:
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def under_agent() -> bool:
    return os.environ.get("TOOLKIT_HEADLESS") == "1" or os.environ.get("TOOLKIT_AGENT_SANDBOXED") == "1"


@contextlib.contextmanager
def web_decision(by: str = "dashboard"):
    """A decision taken on the web dashboard by whoever holds its per-run edit key (printed only in the
    terminal that started `dashboard.py serve`). Stands in for the TTY check, for THIS thread only —
    never for a process that runs under an agent."""
    if under_agent():
        raise PermissionError("refused — the dashboard runs under an agent; a human must start it")
    _web.by = by
    try:
        yield
    finally:
        _web.by = None


def web_by() -> str:
    return getattr(_web, "by", None) or ""


def require(action: str) -> None:
    if under_agent():
        raise PermissionError(f"{action}: refused — this process runs under an agent; a human must do this")
    if web_by():
        return
    if not isatty():
        raise PermissionError(f"{action}: needs an interactive terminal (a human decision)")
