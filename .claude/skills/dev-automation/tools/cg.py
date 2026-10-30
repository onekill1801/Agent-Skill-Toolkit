#!/usr/bin/env python3
"""Codegraph for worktrees — cheap per-worktree index + diff impact, for any agent.

Indexing a big repo from scratch takes minutes; codegraph's `sync` is content-based, so
COPYING an existing index into a fresh worktree and syncing only touches the files
that differ (measured on etask: same commit 0.6 s; 395 files apart ≈ 70 s).

Seed (the index we copy), first match wins:
  1. work/cache/codegraph-seeds/<project_id>/      (refreshed after every prepare)
  2. <clone_dir>/.codegraph                          (registry clone already indexed)
  3. projects.json "codegraph_seed": "<dir with .codegraph>"
  none → full `codegraph init` (bounded by CODEGRAPH_INIT_TIMEOUT, default 600 s)

Diff analysis (for reviews / verify): map the diff's new-side hunks to the methods they
touch (read-only SQLite read of the index), list their callers OUTSIDE the diff, the
affected tests (`codegraph affected`), and a small task context. Written to
<worktree>.codegraph.md for the agent; agents then query with the CLI:
    codegraph callers|impact|context|query ... -p <worktree>

    python cg.py prepare <worktree> --project-id 5401 [--clone-dir DIR] [--seed DIR]
    python cg.py analyze <worktree> --base <sha> --head <sha> [--title "MR title"] [--out FILE]
    python cg.py status <worktree>
"""

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time

import probe_common as pc
import project_config

DB_FILES = ("codegraph.db", "codegraph.db-wal", "codegraph.db-shm")
CODE_KINDS = ("method", "function", "constructor")
_SHA = re.compile(r"^[0-9a-fA-F]{7,64}$")


def _cmd() -> list:
    """argv prefix for codegraph, or [] if not installed.

    Windows npm installs a `codegraph.cmd` shim: subprocess cannot run it without a shell, and
    running it through cmd.exe would re-parse arguments (MR titles!) — command injection. So
    we call node on the script the shim points at."""
    found = shutil.which("codegraph")
    if not found:
        return []
    if os.path.splitext(found)[1].lower() not in (".cmd", ".bat"):
        return [found]
    try:
        with open(found, encoding="utf-8", errors="replace") as f:
            m = re.search(r'"%dp0%\\([^"]+?\.js)"', f.read())
    except OSError:
        return []
    if not m:
        return []
    base = os.path.dirname(found)
    script = os.path.normpath(os.path.join(base, m.group(1)))
    node = os.path.normpath(os.path.join(base, "node.exe"))
    node = node if os.path.isfile(node) else shutil.which("node")
    return [node, script] if node and os.path.isfile(script) else []


def _env() -> dict:
    # code is company code: no usage telemetry, whatever the user-level codegraph setting is
    return {**os.environ, "CODEGRAPH_TELEMETRY": "0", "DO_NOT_TRACK": "1"}


def available() -> bool:
    return bool(_cmd())


def _run(args, cwd=None, timeout=120, input=None):
    cmd = _cmd()
    if not cmd:
        raise FileNotFoundError("codegraph not installed")
    return subprocess.run([*cmd, *args], cwd=cwd, capture_output=True, text=True, input=input,
                          encoding="utf-8", errors="replace", timeout=timeout, env=_env(),
                          stdin=None if input is not None else subprocess.DEVNULL)


def _exclude_index(worktree: str) -> None:
    """Keep `.codegraph/` (hundreds of MB) out of `git add -A` in this worktree and its siblings."""
    try:
        common = subprocess.run(["git", "-C", worktree, "rev-parse", "--git-common-dir"], capture_output=True,
                                text=True, timeout=30).stdout.strip()
        if not common:
            return
        path = os.path.join(common if os.path.isabs(common) else os.path.join(worktree, common), "info", "exclude")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        cur = open(path, encoding="utf-8", errors="replace").read() if os.path.isfile(path) else ""
        if ".codegraph/" not in cur.split():
            with open(path, "a", encoding="utf-8") as f:
                f.write(("" if cur.endswith("\n") or not cur else "\n") + ".codegraph/\n")
    except (OSError, subprocess.SubprocessError):
        pass


def _seed_store(project_id) -> str:
    return os.path.join(project_config._work_dir(), "cache", "codegraph-seeds", str(project_id))


def _has_db(d: str) -> bool:
    return bool(d) and os.path.isfile(os.path.join(d, "codegraph.db"))


def find_seed(project_id, clone_dir: str = "", seed: str = "") -> str:
    reg = {}
    for p in _registry().values():
        if isinstance(p, dict) and str(p.get("gitlab_project_id")) == str(project_id):
            reg = p
            break
    for cand in (seed and os.path.join(os.path.expanduser(seed), ".codegraph"),
                 _seed_store(project_id),
                 clone_dir and os.path.join(clone_dir, ".codegraph"),
                 reg.get("clone_dir") and os.path.join(reg["clone_dir"], ".codegraph"),
                 reg.get("codegraph_seed") and os.path.join(os.path.expanduser(reg["codegraph_seed"]), ".codegraph")):
        if cand and _has_db(cand):
            return cand
    return ""


def _registry() -> dict:
    try:
        with open(os.path.join(project_config._work_dir(), "projects.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _copy_db(src: str, dst: str) -> None:
    os.makedirs(dst, exist_ok=True)
    for n in DB_FILES:
        s = os.path.join(src, n)
        if os.path.isfile(s):
            shutil.copy2(s, os.path.join(dst, n))


def save_seed(worktree: str, project_id) -> bool:
    """Snapshot the worktree's (synced) index as the project's seed, atomically."""
    src = os.path.join(worktree, ".codegraph")
    if not _has_db(src):
        return False
    store = _seed_store(project_id)
    tmp = store + f".tmp-{os.getpid()}"
    shutil.rmtree(tmp, ignore_errors=True)
    _run(["sync", "-q"], cwd=worktree, timeout=120)          # flush WAL state before copying
    _copy_db(src, tmp)
    old = store + f".old-{os.getpid()}"
    if os.path.isdir(store):
        os.replace(store, old)
    os.replace(tmp, store)
    shutil.rmtree(old, ignore_errors=True)
    return True


def prepare(worktree: str, project_id, clone_dir: str = "", seed: str = "") -> dict:
    if not available():
        return {"status": "unavailable", "hint": "install codegraph to get impact analysis"}
    t0 = time.time()
    local = os.path.join(worktree, ".codegraph")
    src = "" if _has_db(local) else find_seed(project_id, clone_dir, seed)
    _exclude_index(worktree)
    try:
        if src:
            _copy_db(src, local)
            p = _run(["sync", "-q"], cwd=worktree, timeout=int(os.environ.get("CODEGRAPH_SYNC_TIMEOUT", "300")))
            mode = "seed+sync"
        elif _has_db(local):
            p = _run(["sync", "-q"], cwd=worktree, timeout=300)
            mode = "sync"
        else:
            p = _run(["init"], cwd=worktree, timeout=int(os.environ.get("CODEGRAPH_INIT_TIMEOUT", "600")))
            mode = "init"
    except subprocess.TimeoutExpired:
        shutil.rmtree(local, ignore_errors=True)             # never leave a half-built index behind
        return {"status": "timeout", "seconds": round(time.time() - t0, 1)}
    except OSError as e:                                     # review must go on without the graph
        return {"status": "unavailable", "message": str(e)[:200]}
    if p.returncode != 0 or not _has_db(local):
        return {"status": "error", "message": (p.stderr or p.stdout).strip()[-300:]}
    saved = save_seed(worktree, project_id)
    return {"status": "ready", "mode": mode, "seed": src or None, "seed_saved": saved,
            "seconds": round(time.time() - t0, 1)}


# --------------------------------------------------------------------------- diff analysis

def _hunks(worktree: str, base: str, head: str) -> dict:
    if not (_SHA.match(base or "") and _SHA.match(head or "")):
        return {}
    out = subprocess.run(["git", "-C", worktree, "diff", "-U0", "-M", base, head, "--"], capture_output=True,
                         text=True, encoding="utf-8", errors="replace", timeout=120).stdout
    ranges, f = {}, None
    for ln in out.splitlines():
        if ln.startswith("+++ "):
            f = None if ln.endswith("/dev/null") else ln[6:]
        m = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", ln)
        if m and f:
            start, n = int(m[1]), int(m[2] if m[2] is not None else 1)
            ranges.setdefault(f, []).append((start, start + max(n, 1) - 1))
    return ranges


def changed_symbols(worktree: str, base: str, head: str, limit: int = 200) -> list:
    db = os.path.join(worktree, ".codegraph", "codegraph.db")
    ranges = _hunks(worktree, base, head)
    if not ranges or not os.path.isfile(db):
        return []
    syms, seen = [], set()
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        for f, rs in ranges.items():
            for a, b in rs:
                for kind, name, qn, s, e in con.execute(
                        "select kind, name, qualified_name, start_line, end_line from nodes "
                        f"where file_path=? and kind in ({','.join('?' * len(CODE_KINDS))}) "
                        "and start_line<=? and end_line>=?", (f, *CODE_KINDS, b, a)):
                    if qn not in seen:
                        seen.add(qn)
                        syms.append({"kind": kind, "name": name, "qualified": qn, "file": f, "lines": [s, e]})
        con.close()
    except sqlite3.Error:
        return []                                          # index schema changed: degrade, don't fail
    return syms[:limit]


def external_callers(worktree: str, syms: list, changed_files: set, per_symbol: int = 10, max_symbols: int = 25) -> list:
    out = []
    for s in syms[:max_symbols]:
        try:
            p = _run(["callers", s["qualified"], "-p", worktree, "-j", "-l", str(per_symbol * 2)], timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            continue
        try:
            data = json.loads(p.stdout or "[]")
        except ValueError:
            continue
        rows = data.get("callers", data) if isinstance(data, dict) else data
        ext = [{"name": r.get("name"), "file": r.get("filePath"), "line": r.get("startLine")}
               for r in rows if r.get("filePath") and r.get("filePath") not in changed_files][:per_symbol]
        if ext:
            out.append({"symbol": s["qualified"], "file": s["file"], "callers": ext})
    return out


def affected_tests(worktree: str, files: list) -> list:
    try:
        p = _run(["affected", "--stdin", "-j", "-p", worktree], input="\n".join(files), timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return []
    try:
        return (json.loads(p.stdout or "{}").get("affectedTests") or [])[:50]
    except ValueError:
        return []


def analyze(worktree: str, base: str, head: str, title: str = "", out: str = "") -> dict:
    if not _has_db(os.path.join(worktree, ".codegraph")):
        return {"status": "no-index"}
    files = sorted(_hunks(worktree, base, head))
    syms = changed_symbols(worktree, base, head)
    callers = external_callers(worktree, syms, set(files))
    tests = affected_tests(worktree, files)
    ctx = ""
    if title:   # `--` : an MR title starting with "-" must not be read as an option
        try:
            p = _run(["context", "-p", worktree, "-n", "20", "--", title], timeout=90)
            ctx = (p.stdout or "")[:6000] if p.returncode == 0 else ""
        except (OSError, subprocess.TimeoutExpired):
            ctx = ""
    res = {"status": "ready", "changed_files": len(files), "changed_methods": len(syms),
           "methods_with_external_callers": len(callers), "affected_tests": tests,
           "external_callers": callers}
    if out:
        lines = [f"# Codegraph — impact of {base[:8]}..{head[:8]}", "",
                 f"- {len(files)} file thay đổi · {len(syms)} method bị sửa · "
                 f"{len(callers)} method có nơi gọi NGOÀI diff (kiểm tra hợp đồng/hành vi ở đó)", ""]
        if callers:
            lines.append("## Nơi gọi ngoài diff (static graph — Spring/reflection có thể thiếu)")
            for c in callers:
                lines.append(f"- `{c['symbol']}` ({c['file']})")
                lines += [f"  - {x['file']}:{x['line']} `{x['name']}`" for x in c["callers"]]
            lines.append("")
        if tests:
            lines += ["## Test bị ảnh hưởng (chạy những test này)", *[f"- {t}" for t in tests], ""]
        if syms:
            lines += ["## Method bị sửa", *[f"- `{s['qualified']}` {s['file']}:{s['lines'][0]}-{s['lines'][1]}"
                                           for s in syms[:60]], ""]
        if ctx:
            lines += ["## Ngữ cảnh (codegraph context)", ctx]
        lines += ["", f"Truy vấn thêm: `codegraph callers|impact|query <symbol> -p {worktree}`"]
        with open(out, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        res["report"] = out
    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="cg.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("worktree")
    p.add_argument("--project-id", required=True)
    p.add_argument("--clone-dir", default="")
    p.add_argument("--seed", default="")
    p = sub.add_parser("analyze")
    p.add_argument("worktree")
    p.add_argument("--base", required=True)
    p.add_argument("--head", required=True)
    p.add_argument("--title", default="")
    p.add_argument("--out", default="")
    p = sub.add_parser("status")
    p.add_argument("worktree")
    a = ap.parse_args(argv)
    if a.cmd == "prepare":
        out = prepare(a.worktree, a.project_id, a.clone_dir, a.seed)
    elif a.cmd == "analyze":
        out = analyze(a.worktree, a.base, a.head, a.title, a.out)
    else:
        out = {"indexed": _has_db(os.path.join(a.worktree, ".codegraph")), "codegraph": available()}
    return pc.emit(out)


if __name__ == "__main__":
    sys.exit(main())
