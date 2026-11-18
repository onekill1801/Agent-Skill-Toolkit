#!/usr/bin/env python3
"""Workspace registry helper: find the git clones under work/ that projects.json does not know yet.

    python workspace.py discover [--root work] [--out temp/registry-draft.json] [--no-api]
    python workspace.py apply --draft temp/registry-draft.json [--only k1,k2]   # [WRITE] merge into projects.json
    python workspace.py where [DIR]                                             # which project a dir belongs to

`discover` only READS: local git metadata + (unless --no-api) GET /projects/<path> on GitLab for the id and
default branch. It writes a DRAFT for a human to review — projects.json is untouched. Repos that look like
environment configuration (config / config-dev / config_uat …) are listed under "config_repos" as
platforms.json candidates, not as projects. `apply` merges the reviewed draft (existing keys are never
overwritten; a timestamped backup of projects.json is written first).
"""

import argparse
import json
import os
import re
import runpy
import shutil
import subprocess
import sys
import time

_HERE = os.path.dirname(os.path.realpath(__file__))
runpy.run_path(os.path.join(os.path.dirname(os.path.dirname(_HERE)), "_runtime", "bootstrap.py"))["activate"]()
from agent_skill_toolkit.integrations import gitlab as _gitlab  # noqa: E402

sys.path.insert(0, _HERE)
import probe_common as pc  # noqa: E402,F401  (UTF-8 stdout on Windows)
import project_config  # noqa: E402

CONFIG_REPO = re.compile(r"^config(?:[-_].+)?$|^.+[-_]config$", re.I)
SKIP_DIRS = {"node_modules", "target", "build", "dist", ".git", ".codegraph", ".claude"}


def _git(path: str, *args) -> str:
    try:
        out = subprocess.run(["git", "-C", path, *args], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


_REMOTE = re.compile(r"^(?:[a-z+]+://(?:[^@/]+@)?(?P<h1>[^/:]+)(?::\d+)?/|[^@]+@(?P<h2>[^:]+):)(?P<path>.+?)(?:\.git)?/?$")


def remote(path: str) -> tuple:
    """(host, 'group/sub/repo') of origin (https or ssh), ('', '') when unknown."""
    m = _REMOTE.match(_git(path, "remote", "get-url", "origin"))
    return ((m.group("h1") or m.group("h2") or "").lower(), m.group("path")) if m else ("", "")


def remote_path(path: str) -> str:
    return remote(path)[1]


def gitlab_host() -> str:
    """Host of the configured GITLAB_URL (the instance discover may query and registry paths refer to)."""
    from urllib.parse import urlparse
    return (urlparse(_gitlab.base_url()).hostname or "").lower()


def find_repos(root: str, max_depth: int = 5) -> list:
    """Every directory under root that is the top of a NON-bare git work tree (main clones only)."""
    out = []
    root = os.path.realpath(root)
    for d, dirs, files in os.walk(root):
        depth = os.path.relpath(d, root).count(os.sep)
        if os.path.isdir(os.path.join(d, ".git")):           # main clone (.git dir, not a worktree .git file)
            out.append(d)
            dirs[:] = []
            continue
        dirs[:] = [x for x in dirs if x not in SKIP_DIRS and depth < max_depth]
    return sorted(out)


def detect_stack(path: str) -> dict:
    has = lambda *names: any(os.path.exists(os.path.join(path, n)) for n in names)
    if has("pom.xml"):
        st = {"lang": "java", "build": "maven"}
        if has(".yo-rc.json"):
            st["framework"] = "jhipster-spring-boot"
        return st
    if has("build.gradle", "build.gradle.kts"):
        return {"lang": "java", "build": "gradle"}
    if has("package.json"):
        st = {"lang": "javascript", "build": "npm"}
        try:
            with open(os.path.join(path, "package.json"), encoding="utf-8") as f:
                deps = {**(json.load(f).get("dependencies") or {})}
            for key, fw in (("@angular/core", "angular"), ("react", "react"), ("vue", "vue"), ("next", "next"),
                            ("@nestjs/core", "nestjs"), ("express", "express")):
                if key in deps:
                    st["framework"] = fw
                    break
        except (OSError, ValueError, AttributeError):
            pass
        if has("tsconfig.json"):
            st["lang"] = "typescript"
        return st
    if has("go.mod"):
        return {"lang": "go", "build": "go"}
    if any(f.endswith((".csproj", ".sln")) for f in os.listdir(path)):
        return {"lang": "csharp", "build": "dotnet"}
    if has("pyproject.toml", "requirements.txt", "setup.py"):
        return {"lang": "python", "build": "pip"}
    return {}


def _key_for(rpath: str, taken: set) -> str:
    """A name shared by several repos is never used bare: group-repo (deeper as needed)."""
    parts = [p for p in rpath.split("/") if p]
    for n in range(min(2, len(parts)), len(parts) + 1):
        key = "-".join(parts[-n:]).lower()
        if key not in taken:
            return key
    base, n = rpath.replace("/", "-").lower(), 2
    if base not in taken:
        return base
    while f"{base}-{n}" in taken:
        n += 1
    return f"{base}-{n}"


def discover(root: str, use_api: bool = True) -> dict:
    reg = project_config._registry()
    # paths registered for OUR GitLab only (an entry with a foreign remote_host is another repository;
    # legacy entries without remote_host belong to the configured instance) — Codex R2 #1
    known_paths = {(e.get("repo_path") or "").lower() for e in reg.values()
                   if isinstance(e, dict) and not e.get("remote_host")}
    known_dirs = {project_config._norm(project_config.clone_path(e)) for e in reg.values()
                  if isinstance(e, dict) and project_config.clone_path(e)}
    taken = set(reg)
    names = {}
    repos = []
    home = gitlab_host()
    for path in find_repos(root):
        host, rpath = remote(path)
        ours = bool(rpath) and host == home                 # a path only means something on OUR GitLab
        if project_config._norm(path) in known_dirs or (ours and rpath.lower() in known_paths):
            continue
        repos.append((path, rpath, host, ours))
        name = (rpath or path).replace("\\", "/").rstrip("/").rsplit("/", 1)[-1].lower()
        names[name] = names.get(name, 0) + 1
    projects, configs, problems = {}, [], []
    for path, rpath, host, ours in repos:
        name = (rpath or path).replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
        entry = {"repo_path": rpath, "clone_dir": path.replace("\\", "/")}
        if not rpath:
            problems.append({"clone_dir": entry["clone_dir"], "problem": "no origin remote"})
        elif not ours:
            entry["remote_host"] = host
            problems.append({"clone_dir": entry["clone_dir"], "problem":
                             f"origin ở host khác ({host}) — không tra GitLab, không có id"})
        if CONFIG_REPO.match(name):
            configs.append({**entry, "note": "cấu hình môi trường — khai trong platforms.json, không phải project"})
            continue
        branch = _git(path, "symbolic-ref", "--short", "refs/remotes/origin/HEAD").split("/", 1)[-1]
        if ours and use_api:
            info = _gitlab.project_info(rpath)
            if isinstance(info, dict) and not info.get("error"):
                entry["gitlab_project_id"] = info.get("id")
                branch = info.get("default_branch") or branch
            else:
                problems.append({"clone_dir": entry["clone_dir"], "problem":
                                 f"GitLab: {info.get('status') if isinstance(info, dict) else '?'}"})
        if branch:
            entry["default_branch"] = branch
        entry.update(default_env="dev", protected_envs=["prod", "production"])
        stack = detect_stack(path)
        if stack:
            entry["stack"] = stack
        group = rpath.split("/", 1)[0] if rpath else ""
        if group and group in _platforms():
            entry["platform"] = group
        key = name.lower() if names.get(name.lower(), 0) == 1 and name.lower() not in taken \
            else _key_for(rpath or entry["clone_dir"], taken)
        if key in projects:                                # never overwrite another discovered repo
            raise ValueError(f"duplicate registry key {key!r}")
        taken.add(key)
        projects[key] = entry
    return {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"), "root": os.path.realpath(root).replace("\\", "/"),
            "projects": dict(sorted(projects.items())), "config_repos": configs, "problems": problems}


def _platforms() -> dict:
    try:
        with open(os.path.join(project_config._work_dir(), "platforms.json"), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def apply(draft_path: str, only: list = None) -> dict:
    with open(draft_path, encoding="utf-8") as f:
        draft = json.load(f)
    path = os.path.join(project_config._work_dir(), "projects.json")
    from agent_skill_toolkit.core import locks
    with locks.file_lock(path + ".lock", timeout=30):       # read-merge-backup-replace as one (R1 #2)
        return _apply_locked(path, draft, only)


def _apply_locked(path: str, draft: dict, only: list) -> dict:
    with open(path, encoding="utf-8") as f:
        reg = json.load(f)
    if not isinstance(reg, dict):
        raise ValueError("projects.json is not an object")
    added, skipped = [], []
    for key, entry in (draft.get("projects") or {}).items():
        if only and key not in only:
            continue
        if key in reg:
            skipped.append(key)
            continue
        reg[key] = entry
        added.append(key)
    if not added:
        return {"added": [], "skipped_existing": skipped, "written": False}
    backup = f"{path}.bak-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}-{os.urandom(3).hex()}"
    shutil.copy2(path, backup)
    tmp = f"{path}.tmp-{os.getpid()}"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(reg, f, ensure_ascii=False, indent=2)
        f.write("\n")
    with open(tmp, encoding="utf-8") as f:
        json.load(f)                                       # verify before replacing
    os.replace(tmp, path)
    return {"added": added, "skipped_existing": skipped, "written": True, "backup": backup}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("discover")
    p.add_argument("--root", default=project_config._work_dir())
    p.add_argument("--out", default=os.path.join(project_config._repo_root(), "temp", "registry-draft.json"))
    p.add_argument("--no-api", action="store_true")
    p = sub.add_parser("apply")
    p.add_argument("--draft", required=True)
    p.add_argument("--only", default="")
    p = sub.add_parser("where")
    p.add_argument("dir", nargs="?", default="")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "discover":
            res = discover(a.root, use_api=not a.no_api)
            os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
            with open(a.out, "w", encoding="utf-8", newline="\n") as f:
                json.dump(res, f, ensure_ascii=False, indent=2)
                f.write("\n")
            print(json.dumps({"draft": a.out, "projects": len(res["projects"]), "config_repos": len(res["config_repos"]),
                              "problems": res["problems"]}, ensure_ascii=False, indent=2))
        elif a.cmd == "apply":
            print(json.dumps(apply(a.draft, [k for k in a.only.split(",") if k]), ensure_ascii=False, indent=2))
        else:
            d = a.dir or os.getcwd()
            print(json.dumps({"dir": os.path.realpath(d), "project": project_config.project_from_cwd(d)},
                             ensure_ascii=False))
        return 0
    except (OSError, ValueError) as e:
        print(json.dumps({"error": True, "message": f"{type(e).__name__}: {e}"}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    sys.exit(main())
