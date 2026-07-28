#!/usr/bin/env python3
"""Read/write the shared project registry (`work/projects.json`).

The `new-product` skill registers a freshly scaffolded product here so the
`auto-dev` / `dev-automation` skills can immediately take it over (test gate,
MR flow, probes) — this is the hand-off that turns a greenfield skeleton into a
first-class project in the toolkit.

Zero external dependencies — Python stdlib only. Returns JSON on stdout; errors
follow the repo convention `{"error": true, "message": ...}`.

Usage:
    python registry.py list
    python registry.py get <name>
    python registry.py add <name> --clone-dir <path> [--stack java-spring]
        [--gitlab-project-id 123] [--gitlab-path group/name]
        [--default-target-branch main] [--build-cmd "..."] [--test-cmd "..."]
        [--lint-cmd "..."] [--force]                         # [WRITE]
    python registry.py remove <name>                          # [WRITE]

work_dir = $WORK_DIR or <repo>/work (same resolution as project_config.py).
"""

import argparse
import json
import os
import sys

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass


def _repo_root() -> str:
    search = os.path.dirname(os.path.abspath(__file__))
    for _ in range(6):
        if os.path.isdir(os.path.join(search, ".git")) or os.path.isfile(os.path.join(search, "CLAUDE.md")):
            return search
        search = os.path.dirname(search)
    return os.path.dirname(os.path.abspath(__file__))


def _work_dir() -> str:
    return os.environ.get("WORK_DIR") or os.path.join(_repo_root(), "work")


def _registry_path() -> str:
    return os.path.join(_work_dir(), "projects.json")


def _read() -> dict:
    path = _registry_path()
    if not os.path.isfile(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _write(data: dict) -> None:
    """Atomic write: dump to a temp file in the same dir, then replace."""
    os.makedirs(_work_dir(), exist_ok=True)
    path = _registry_path()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def cmd_list(_args) -> dict:
    data = _read()
    names = [k for k in data.keys() if not k.startswith("_")]
    return {"registry": _registry_path(), "count": len(names), "projects": names}


def cmd_get(args) -> dict:
    data = _read()
    entry = data.get(args.name)
    if entry is None:
        return {"error": True, "message": f"project '{args.name}' not in registry"}
    return {args.name: entry}


def cmd_add(args) -> dict:
    data = _read()
    if args.name in data and not args.force:
        return {"error": True,
                "message": f"project '{args.name}' already exists; pass --force to overwrite"}
    if args.name.startswith("_"):
        return {"error": True, "message": "project name must not start with '_'"}

    clone_dir = os.path.abspath(args.clone_dir)
    entry = {"clone_dir": clone_dir,
             "default_target_branch": args.default_target_branch}
    if args.gitlab_project_id:
        entry["gitlab_project_id"] = str(args.gitlab_project_id)
    if args.gitlab_path:
        entry["gitlab_path"] = args.gitlab_path
    if args.azure_project:
        entry["azure_project"] = args.azure_project
    if args.stack:
        entry["scaffold_stack"] = args.stack
    if args.build_cmd:
        entry["build_cmd"] = args.build_cmd
    if args.test_cmd:
        entry["test_cmd"] = args.test_cmd
    if args.lint_cmd:
        entry["lint_cmd"] = args.lint_cmd

    data[args.name] = entry
    _write(data)
    return {"ok": True, "registered": args.name, "entry": entry, "registry": _registry_path()}


def cmd_remove(args) -> dict:
    data = _read()
    if args.name not in data:
        return {"error": True, "message": f"project '{args.name}' not in registry"}
    removed = data.pop(args.name)
    _write(data)
    return {"ok": True, "removed": args.name, "was": removed}


def _print(obj) -> None:
    out = json.dumps(obj, indent=2, ensure_ascii=False)
    sys.stdout.buffer.write(out.encode("utf-8"))
    sys.stdout.buffer.write(b"\n")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list")

    g = sub.add_parser("get")
    g.add_argument("name")

    a = sub.add_parser("add")
    a.add_argument("name")
    a.add_argument("--clone-dir", required=True)
    a.add_argument("--stack", default="")
    a.add_argument("--gitlab-project-id", default="")
    a.add_argument("--gitlab-path", default="")
    a.add_argument("--azure-project", default="")
    a.add_argument("--default-target-branch", default="main")
    a.add_argument("--build-cmd", default="")
    a.add_argument("--test-cmd", default="")
    a.add_argument("--lint-cmd", default="")
    a.add_argument("--force", action="store_true")

    r = sub.add_parser("remove")
    r.add_argument("name")

    args = p.parse_args()
    handler = {"list": cmd_list, "get": cmd_get, "add": cmd_add, "remove": cmd_remove}[args.cmd]
    try:
        result = handler(args)
    except (OSError, json.JSONDecodeError) as e:
        result = {"error": True, "message": f"{type(e).__name__}: {e}"}
    _print(result)
    return 1 if isinstance(result, dict) and result.get("error") else 0


if __name__ == "__main__":
    sys.exit(main())
