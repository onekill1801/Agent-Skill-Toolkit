#!/usr/bin/env python3
"""Turn a scaffolded directory into a git repo, and optionally publish it to GitLab.

Local git steps use subprocess (git must be installed — `doctor.py` reports it).
GitLab project creation reuses dev-automation's `config.py` for GITLAB_URL /
GITLAB_PRIVATE_TOKEN, so no token is ever hardcoded here.

Stdlib only. Returns JSON; errors follow `{"error": true, "message": ...}`.

Usage:
    python repo_init.py init <dir> [--branch main] [--message "..."]        # [WRITE]
    python repo_init.py create-gitlab <name> [--namespace-id N]
        [--visibility private|internal|public] [--description "..."]         # [WRITE]
    python repo_init.py push <dir> <remote_url> [--branch main]             # [WRITE]

`init` writes a stack-agnostic .gitignore only if one is absent (never clobbers a
generator's own). A first commit is made only when there is something to commit.
"""

import argparse
import json
import os
import ssl
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

# Reuse dev-automation config (walks up to repo root for .env).
_DEV_TOOLS = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "dev-automation", "tools"))
if _DEV_TOOLS not in sys.path:
    sys.path.insert(0, _DEV_TOOLS)
import config  # noqa: E402

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

_GITIGNORE = """\
# dependencies / build output
node_modules/
dist/
build/
target/
__pycache__/
*.pyc
.venv/
venv/

# env & secrets
.env
.env.local
*.log

# IDE
.idea/
.vscode/
.DS_Store
"""


def _git(dirpath: str, *args: str) -> dict:
    try:
        proc = subprocess.run(["git", *args], cwd=dirpath,
                              capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"error": True, "message": f"git {' '.join(args)} failed to run: {e}"}
    if proc.returncode != 0:
        return {"error": True, "message": f"git {' '.join(args)} exited {proc.returncode}",
                "stderr": (proc.stderr or "").strip()[-1500:]}
    return {"ok": True, "stdout": (proc.stdout or "").strip()}


def cmd_init(args) -> dict:
    d = os.path.abspath(args.dir)
    if not os.path.isdir(d):
        return {"error": True, "message": f"not a directory: {d}"}
    steps = []

    if not os.path.isdir(os.path.join(d, ".git")):
        r = _git(d, "init", "-b", args.branch)
        if r.get("error"):
            # older git without -b: init then rename
            r = _git(d, "init")
            if r.get("error"):
                return r
            _git(d, "checkout", "-b", args.branch)
        steps.append("init")
    else:
        steps.append("already-git")

    gitignore = os.path.join(d, ".gitignore")
    if not os.path.isfile(gitignore):
        with open(gitignore, "w", encoding="utf-8", newline="\n") as f:
            f.write(_GITIGNORE)
        steps.append("wrote .gitignore")

    add = _git(d, "add", "-A")
    if add.get("error"):
        return add

    status = _git(d, "status", "--porcelain")
    if status.get("error"):
        return status
    if not status["stdout"]:
        return {"ok": True, "dir": d, "branch": args.branch, "steps": steps,
                "committed": False, "note": "nothing to commit"}

    # Pre-check git identity so a missing user.name/email fails with a clear,
    # actionable message instead of git's raw commit-time stderr.
    email = _git(d, "config", "user.email")
    name = _git(d, "config", "user.name")
    if email.get("error") or not email.get("stdout") or name.get("error") or not name.get("stdout"):
        return {"error": True, "steps": steps, "committed": False,
                "message": "git identity not configured — scaffold staged but not committed. "
                "Set it, then re-run init:\n"
                "  git config --global user.email \"you@example.com\"\n"
                "  git config --global user.name \"Your Name\""}

    commit = _git(d, "commit", "-m", args.message)
    if commit.get("error"):
        return commit
    steps.append("commit")
    head = _git(d, "rev-parse", "HEAD")
    return {"ok": True, "dir": d, "branch": args.branch, "steps": steps,
            "committed": True, "head": head.get("stdout", "")[:12]}


def _gitlab_request(path: str, data: dict) -> dict:
    url = config.gitlab_base_url() + path
    token = config.get("GITLAB_PRIVATE_TOKEN")
    if not token:
        return {"error": True, "message": "GITLAB_PRIVATE_TOKEN missing in .env"}
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"PRIVATE-TOKEN": token})
    ctx = ssl.create_default_context()
    if config.get("SSL_VERIFY", "true").lower() in ("false", "0", "no"):
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=60) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return {"error": True, "status": e.code,
                "message": e.read().decode() if e.fp else str(e)}
    except urllib.error.URLError as e:
        return {"error": True, "message": f"GitLab unreachable: {e}"}


def cmd_create_gitlab(args) -> dict:
    data = {"name": args.name, "visibility": args.visibility,
            "initialize_with_readme": "false"}
    if args.namespace_id:
        data["namespace_id"] = args.namespace_id
    if args.description:
        data["description"] = args.description
    res = _gitlab_request("/projects", data)
    if isinstance(res, dict) and res.get("error"):
        return res
    return {"ok": True, "id": res.get("id"), "path": res.get("path_with_namespace"),
            "http_url": res.get("http_url_to_repo"),
            "ssh_url": res.get("ssh_url_to_repo"), "web_url": res.get("web_url")}


def cmd_push(args) -> dict:
    d = os.path.abspath(args.dir)
    if not os.path.isdir(os.path.join(d, ".git")):
        return {"error": True, "message": f"{d} is not a git repo (run init first)"}
    existing = _git(d, "remote")
    if "origin" not in (existing.get("stdout", "") or "").split():
        r = _git(d, "remote", "add", "origin", args.remote_url)
        if r.get("error"):
            return r
    else:
        _git(d, "remote", "set-url", "origin", args.remote_url)
    push = _git(d, "push", "-u", "origin", args.branch)
    if push.get("error"):
        return push
    return {"ok": True, "dir": d, "remote": args.remote_url, "branch": args.branch,
            "pushed": True}


def _print(obj) -> None:
    out = json.dumps(obj, indent=2, ensure_ascii=False)
    sys.stdout.buffer.write(out.encode("utf-8"))
    sys.stdout.buffer.write(b"\n")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    i = sub.add_parser("init")
    i.add_argument("dir")
    i.add_argument("--branch", default="main")
    i.add_argument("--message", default="chore: initial scaffold")

    g = sub.add_parser("create-gitlab")
    g.add_argument("name")
    g.add_argument("--namespace-id", default="")
    g.add_argument("--visibility", default="private",
                   choices=["private", "internal", "public"])
    g.add_argument("--description", default="")

    pu = sub.add_parser("push")
    pu.add_argument("dir")
    pu.add_argument("remote_url")
    pu.add_argument("--branch", default="main")

    args = p.parse_args()
    handler = {"init": cmd_init, "create-gitlab": cmd_create_gitlab, "push": cmd_push}[args.cmd]
    result = handler(args)
    _print(result)
    return 1 if isinstance(result, dict) and result.get("error") else 0


if __name__ == "__main__":
    sys.exit(main())
