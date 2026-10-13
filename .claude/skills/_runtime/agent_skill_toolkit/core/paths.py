"""Three roots, kept apart (design §B.3 compatibility checklist):

  toolkit_root()  where the toolkit code lives (read-only in an installed plugin)
  state_dir()     where runtime state is written   — $TOOLKIT_STATE_DIR, else <toolkit>/temp
  work_dir()      product-repo registry + clones   — $WORK_DIR, else <toolkit>/work
"""

import os

_HERE = os.path.dirname(os.path.abspath(__file__))
# .../.claude/skills/_runtime/agent_skill_toolkit/core -> toolkit root is 5 levels up
_DEFAULT_ROOT = os.path.abspath(os.path.join(_HERE, "..", "..", "..", "..", ".."))


def toolkit_root() -> str:
    return os.environ.get("TOOLKIT_ROOT") or _DEFAULT_ROOT


def state_dir(*parts: str) -> str:
    base = os.environ.get("TOOLKIT_STATE_DIR") or os.path.join(toolkit_root(), "temp")
    path = os.path.join(base, *parts)
    os.makedirs(path if not os.path.splitext(path)[1] else os.path.dirname(path), exist_ok=True)
    return path


def work_dir() -> str:
    return os.environ.get("WORK_DIR") or os.path.join(toolkit_root(), "work")
