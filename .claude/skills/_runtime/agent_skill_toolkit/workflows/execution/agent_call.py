"""One headless agent turn from a workflow step (through agent_runner: role routing, quota pause,
per-turn budgets, model per usage tier). Shared by workflows that need an agent's text.

    text = agent_call.run(ctx, "proposal", prompt, cwd=repo, timeout=900)
"""

import json
import os
import sys
import time

from agent_skill_toolkit.core import paths
from agent_skill_toolkit.workflows import engine as E

AGENT_RUNNER = os.path.join(".claude", "skills", "auto-dev", "tools", "agent_runner.py")   # runtime edge


def run(ctx, role: str, prompt: str, cwd: str = "", timeout: int = 900) -> str:
    runner = os.path.join(paths.toolkit_root(), AGENT_RUNNER)
    argv = [sys.executable, runner, "--role", role, "--timeout", str(timeout)]
    if cwd:
        argv += ["--cwd", cwd]
    p = ctx.run(argv, timeout=timeout + 120, input=prompt)       # agent_runner charges the turn
    if p.returncode != 0:
        try:
            err = json.loads((p.stdout or "").strip().splitlines()[-1])
        except (ValueError, IndexError):
            err = {"message": (p.stderr or p.stdout or "")[-400:]}
        if err.get("kind") == "quota":
            raise E.QuotaExceeded(err.get("resume_at") or time.time() + 1800, role, err.get("message", ""))
        if err.get("kind") == "budget":
            raise E.NeedsHuman(f"agent ({role}): {str(err.get('message', ''))[:300]}")
        raise RuntimeError(f"agent ({role}) failed: {str(err.get('message', ''))[:300]}")
    return p.stdout or ""
