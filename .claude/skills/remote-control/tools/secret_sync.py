#!/usr/bin/env python3
"""Secret sync facade (agent_skill_toolkit.integrations.secret_sync): carry .env, the registry, kubeconfigs
and work/memory to / from the same toolkit on another machine over SSH — never through GitHub.

    python secret_sync.py paths  <alias>                              # what is synced for that host
    python secret_sync.py status <alias> [--only work/kube,.env]      # per-file state, contents never shown
    python secret_sync.py push   <alias> [--only ...] [--force] [--yes]   # [WRITE on the peer]
    python secret_sync.py pull   <alias> [--only ...] [--force] [--yes]   # [WRITE here]

push / pull without --yes is a dry run. A file changed on both sides is a conflict and is skipped
unless --force. The receiving side backs up what it replaces (temp/secret-sync/backups/); nothing is deleted.
Host = an entry of work/hosts.json with `toolkit_dir` (see the module docstring).
"""

import argparse
import json
import runpy
import sys
from pathlib import Path

runpy.run_path(str(Path(__file__).resolve().parents[2] / "_runtime" / "bootstrap.py"))["activate"]()
from agent_skill_toolkit.core import console  # noqa: E402
from agent_skill_toolkit.integrations import secret_sync  # noqa: E402


def main(argv=None) -> int:
    console.utf8()
    ap = argparse.ArgumentParser(prog="secret_sync.py")
    ap.add_argument("action", choices=["paths", "status", "push", "pull"])
    ap.add_argument("alias")
    ap.add_argument("--only", help="comma-separated rel paths (file or folder) to limit the sync to")
    ap.add_argument("--force", action="store_true", help="also send files changed on both sides")
    ap.add_argument("--yes", action="store_true", help="really write (default: dry run)")
    a = ap.parse_args(argv)
    only = [p for p in (a.only or "").split(",") if p.strip()]
    try:
        if a.action == "paths":
            out = {"host": a.alias, "paths": secret_sync.sync_paths(secret_sync.peer(a.alias))}
        elif a.action == "status":
            out = secret_sync.status(a.alias, only)
        else:
            out = getattr(secret_sync, a.action)(a.alias, only, force=a.force, apply=a.yes)
    except LookupError as e:
        out = {"error": True, "message": str(e)}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 1 if out.get("error") else 0


if __name__ == "__main__":
    sys.exit(main())
