"""Sync the files git must never carry (.env, registry, kubeconfigs, portable memory) between this toolkit
checkout and the same toolkit on another machine, over SSH only — nothing goes through GitHub.

The peer is an entry of work/hosts.json (same allowlist as ssh_exec) with the extra key `toolkit_dir`:
    "ubuntu-c": {"host": "10.15.190.190", "user": "chungtv8", "key": "~/.ssh/id_orca",
                 "toolkit_dir": "/home/chungtv8/Desktop/Code/tools/Agent-Skill-Toolkit"}
Optional: "python" (default python3), "port", "sync_paths" (replaces DEFAULT_PATHS).

Three-way by hash: temp/secret-sync/<alias>.json keeps each file's sha at the last sync, so a file changed
on ONE side goes that way, and a file changed on BOTH sides is a conflict (only --force sends it). Nothing is
deleted; the receiving side backs up every file it replaces. Contents are never printed — paths and short
hashes only.

    from agent_skill_toolkit.integrations import secret_sync
    secret_sync.status("ubuntu-c")                  # rows: path, state, local/remote sha
    secret_sync.push("ubuntu-c", apply=False)       # dry run: what would go
"""

import json
import os
import shlex
import subprocess

from agent_skill_toolkit.core import filesync, paths, proc

DEFAULT_PATHS = [".env", "work/hosts.json", "work/agents.json", "work/platforms.json", "work/projects.json",
                 "work/team.json", "work/kube", "work/memory", "work/secrets"]
REMOTE_BACKUPS = "temp/secret-sync/backups"
TIMEOUT = 120


def _hosts_file() -> str:
    return os.path.join(paths.work_dir(), "hosts.json")


def peer(alias: str) -> dict:
    try:
        with open(_hosts_file(), encoding="utf-8") as f:
            spec = json.load(f).get(alias)
    except (OSError, ValueError):
        spec = None
    if not spec:
        raise LookupError(f"host '{alias}' is not in {_hosts_file()}")
    if not spec.get("toolkit_dir"):
        raise LookupError(f"host '{alias}' has no toolkit_dir (the toolkit checkout on that machine)")
    return spec


def sync_paths(spec: dict) -> list:
    return list(spec.get("sync_paths") or DEFAULT_PATHS)


def _ssh_argv(spec: dict, remote_cmd: str) -> list:
    target = f"{spec['user']}@{spec['host']}" if spec.get("user") else spec["host"]
    argv = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=15"]
    if spec.get("port"):
        argv += ["-p", str(spec["port"])]
    if spec.get("key"):
        argv += ["-o", "IdentitiesOnly=yes", "-i", os.path.expanduser(spec["key"])]
    return argv + [target, remote_cmd]


PEER_BOOT = ("import json,sys;m=json.load(sys.stdin);g={'__name__':'filesync_peer'};exec(m['src'],g);"
             "sys.exit(g['main'](m['op'],m['args'],m.get('data')))")


def _remote(spec: dict, op: str, args: dict, data: str = None) -> dict:
    """Run one filesync op on the peer: its own source goes along on stdin, so the peer needs only python3."""
    with open(filesync.__file__, encoding="utf-8") as f:
        envelope = {"src": f.read(), "op": op, "args": {"root": spec["toolkit_dir"], **args}, "data": data}
    cmd = f"{shlex.quote(spec.get('python', 'python3'))} -c {shlex.quote(PEER_BOOT)}"
    try:
        cp = proc.run(_ssh_argv(spec, cmd), input=json.dumps(envelope), timeout=TIMEOUT)
    except FileNotFoundError:
        return {"error": True, "message": "`ssh` not found on PATH (install the OpenSSH client)"}
    except subprocess.TimeoutExpired:
        return {"error": True, "message": f"peer did not answer within {TIMEOUT}s"}
    try:
        return json.loads(cp.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"error": True, "status": cp.returncode, "message": (cp.stderr or cp.stdout).strip()[-500:]}


def _state_file(alias: str) -> str:
    return paths.state_path("secret-sync", f"{alias}.json")


def _load_state(alias: str) -> dict:
    try:
        with open(_state_file(alias), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_state(alias: str, base: dict) -> None:
    paths.state_dir("secret-sync")
    with open(_state_file(alias), "w", encoding="utf-8") as f:
        json.dump(base, f, indent=1, sort_keys=True)


def _selected(rel: str, only) -> bool:
    return not only or any(rel == o.strip("/") or rel.startswith(o.strip("/") + "/") for o in only)


def classify(local: dict, remote: dict, base: dict, only=None) -> list:
    """One row per file: same | local-only | remote-only | local-changed | remote-changed | conflict."""
    rows = []
    for rel in sorted(set(local) | set(remote)):
        if not _selected(rel, only):
            continue
        lo, re_, b = local.get(rel), remote.get(rel), base.get(rel)
        if lo and re_ and lo["sha"] == re_["sha"]:
            state = "same"
        elif lo and not re_:
            state = "local-only"
        elif re_ and not lo:
            state = "remote-only"
        elif b == re_["sha"]:
            state = "local-changed"
        elif b == lo["sha"]:
            state = "remote-changed"
        else:
            state = "conflict"
        row = {"path": rel, "state": state, "local": lo and lo["sha"][:8], "remote": re_ and re_["sha"][:8]}
        if state == "conflict":
            row["newer"] = "local" if lo["mtime"] >= re_["mtime"] else "remote"
        rows.append(row)
    return rows


def _scan(alias: str, only=None):
    spec = peer(alias)
    root = paths.toolkit_root()
    remote = _remote(spec, "manifest", {"paths": sync_paths(spec)})
    if remote.get("error"):
        return spec, None, None, remote
    local = filesync.manifest(root, sync_paths(spec))
    base = _load_state(alias)
    rows = classify(local, remote["files"], base, only)
    # files already equal on both sides become the base of the next comparison
    agreed = {r["path"]: local[r["path"]]["sha"] for r in rows if r["state"] == "same"}
    if any(base.get(k) != v for k, v in agreed.items()):
        _save_state(alias, {**base, **agreed})
    return spec, local, remote["files"], rows


def status(alias: str, only=None) -> dict:
    _, _, _, rows = _scan(alias, only)
    if isinstance(rows, dict):
        return rows
    counts = {}
    for r in rows:
        counts[r["state"]] = counts.get(r["state"], 0) + 1
    return {"host": alias, "counts": counts, "files": [r for r in rows if r["state"] != "same"]}


def _transfer(alias: str, direction: str, only, force: bool, apply: bool) -> dict:
    spec, local, remote, rows = _scan(alias, only)
    if isinstance(rows, dict):
        return rows
    mine = ("local-only", "local-changed") if direction == "push" else ("remote-only", "remote-changed")
    send = [r["path"] for r in rows if r["state"] in mine or (force and r["state"] == "conflict")]
    conflicts = [r for r in rows if r["state"] == "conflict" and not force]
    out = {"host": alias, "direction": direction, "files": send, "conflicts": conflicts}
    if not apply or not send:
        return {**out, "dry_run": not apply}
    if direction == "push":
        res = _remote(spec, "apply", {"backup_dir": REMOTE_BACKUPS}, data=filesync.pack(paths.toolkit_root(), send))
        shas = {rel: local[rel]["sha"] for rel in send}
    else:
        got = _remote(spec, "fetch", {"rels": send})
        if got.get("error"):
            return {**out, **got}
        try:
            res = filesync.unpack(paths.toolkit_root(), got["tar"], paths.state_dir("secret-sync", "backups"))
        except (OSError, ValueError) as e:
            return {**out, "error": True, "message": f"{type(e).__name__}: {e}"}
        shas = {rel: remote[rel]["sha"] for rel in send}
    if res.get("error"):
        return {**out, **res}
    _save_state(alias, {**_load_state(alias), **shas})
    return {**out, "written": res["written"], "backup": res["backup"]}


def push(alias: str, only=None, force: bool = False, apply: bool = False) -> dict:
    return _transfer(alias, "push", only, force, apply)


def pull(alias: str, only=None, force: bool = False, apply: bool = False) -> dict:
    return _transfer(alias, "pull", only, force, apply)
