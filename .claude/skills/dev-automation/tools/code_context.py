#!/usr/bin/env python3
"""Code context for agents and tools — CodeGraph first, grep fallback (design §8.1, checklist F.12).

    python code_context.py callers <symbol>   --repo DIR [--limit 20]
    python code_context.py impact  <symbol>   --repo DIR [--depth 2]
    python code_context.py locate  <symbol>   --repo DIR            # where it is defined
    python code_context.py trace   --repo DIR < stacktrace.txt      # stack trace -> repo files + lines
    python code_context.py affected <file>... --repo DIR            # tests touched by these files
    python code_context.py status  --repo DIR

Every answer says where it came from: "source": "codegraph" | "grep", plus why codegraph was not
used ("fallback": "...").  CodeGraph is trusted only when its index is FRESH (initialized, state
complete, no pending changes); otherwise `codegraph sync` is tried once (--no-sync to skip) and,
still stale, the answer comes from grep. Grep is always available, never needs network, and
reads code only (never .env / secrets). Cross-repo HTTP/Kafka calls are in neither: the API
contract is the source of truth there.
"""

import argparse
import json
import os
import re
import subprocess
import sys

import cg
import probe_common as pc  # noqa: F401  (UTF-8 stdout on Windows)

CODE_EXT = (".py", ".java", ".kt", ".kts", ".scala", ".groovy", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
            ".go", ".rb", ".cs", ".php", ".xml", ".sql")
SKIP_DIRS = {".git", ".codegraph", "node_modules", "target", "build", "dist", "out", "__pycache__", ".venv",
             "venv", ".idea", ".gradle", ".agents", "temp", "work"}
SECRET_FILE = re.compile(r"(^|[\\/])\.env($|\.)|secret|credential|\.pem$|\.key$", re.I)
TEST_FILE = re.compile(r"(^|/)(tests?|specs?|__tests__)/|(^|/)src/(test|it)/|(^|/)test_[^/]*\.py$|_test\.(py|go|rb)$|"
                       r"(Test|Tests|IT|Spec)\.(java|kt|scala|groovy)$|\.(spec|test)\.[cm]?[jt]sx?$", re.I)
SYMBOL = re.compile(r"^[A-Za-z_$][\w$]*(\.[A-Za-z_$][\w$]*)*$")
DEF_RX = (r"(^|\s)(def|class|interface|enum|record|function|func|fun|trait|object|struct)\s+{name}\b|"
          r"\b(public|private|protected|static|final|abstract|synchronized|async)\b[^;=(){{}}]*\b{name}\s*\(|"
          r"\b(const|let|var)\s+{name}\s*=\s*(async\s*)?(\(|function)")
MAX_FILE = 2 * 2 ** 20


# ----------------------------------------------------------------------------- codegraph side

def index_state(repo: str, sync: bool = True) -> dict:
    """{"usable": bool, "reason": str, "synced": bool, "status": {...}}"""
    if not cg.available():
        return {"usable": False, "reason": "codegraph not installed"}
    if not os.path.isfile(os.path.join(repo, ".codegraph", "codegraph.db")):
        return {"usable": False, "reason": "repo not indexed (run cg.py prepare / codegraph init)"}

    def status():
        try:
            p = cg._run(["status", repo, "-j"], timeout=60)
            return json.loads(p.stdout or "{}")
        except (OSError, ValueError, subprocess.TimeoutExpired) as e:
            return {"error": str(e)}

    def verdict(s):
        if s.get("error") or not s.get("initialized"):
            return f"status unreadable: {s.get('error') or 'not initialized'}"
        idx = s.get("index") or {}
        if idx.get("state") != "complete" or idx.get("reindexRecommended"):
            return f"index state {idx.get('state')!r} (reindex recommended: {idx.get('reindexRecommended')})"
        if s.get("worktreeMismatch"):
            return f"index belongs to another worktree: {s['worktreeMismatch']}"
        pend = s.get("pendingChanges") or {}
        n = sum(int(pend.get(k) or 0) for k in ("added", "modified", "removed"))
        return f"index is stale ({n} pending file changes)" if n else ""

    s = status()
    why = verdict(s)
    synced = False
    if why and sync and "unreadable" not in why:
        try:
            cg._run(["sync", repo], timeout=int(os.environ.get("CODEGRAPH_SYNC_TIMEOUT") or 180))
            synced = True
            s = status()
            why = verdict(s)
        except (OSError, subprocess.TimeoutExpired) as e:
            why = f"{why}; sync failed: {e}"
    return {"usable": not why, "reason": why, "synced": synced,
            "status": {k: s.get(k) for k in ("lastIndexed", "fileCount", "nodeCount", "languages", "pendingChanges")}}


def _cg_json(args: list, timeout: int = 120):
    """(data, "") or (None, reason) — never raises: grep is the fallback (Codex F.12 #3)."""
    try:
        p = cg._run(args, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        return None, f"codegraph {args[0]} failed: {type(e).__name__}: {e}"[:300]
    if p.returncode != 0:
        return None, f"codegraph {args[0]} exit {p.returncode}: {(p.stderr or p.stdout or '')[-200:]}"
    text = (p.stdout or "").strip()
    if not text.startswith(("{", "[")):
        return None, text[:300]          # e.g. 'Symbol "x" not found — did you mean: ...'
    try:
        data = json.loads(text)
    except ValueError:
        return None, text[:300]
    if args[0] in ("callers", "impact") and not (isinstance(data, dict) and isinstance(data.get("definitions", []), list)):
        return None, f"codegraph {args[0]}: unexpected answer shape"
    if args[0] == "query" and not isinstance(data, list):
        return None, "codegraph query: unexpected answer shape"
    return data, ""


def _rows(defs: list, key: str) -> list:
    out = []
    for d in defs or []:
        if not isinstance(d, dict) or not isinstance(d.get(key) or [], list):
            continue
        for r in d.get(key) or []:
            if isinstance(r, dict) and isinstance(r.get("filePath"), str) and r.get("filePath"):
                out.append({"name": r.get("name"), "kind": r.get("kind"), "file": r["filePath"].replace("\\", "/"),
                            "line": r.get("startLine")})
    return out


# ----------------------------------------------------------------------------- grep side

def _inside(real: str, root_real: str) -> bool:
    try:
        return os.path.commonpath([os.path.normcase(real), os.path.normcase(root_real)]) == os.path.normcase(root_real)
    except ValueError:                               # different drives
        return False


def _iter_code(repo: str):
    """Code files of the repo. Never follows a symlink, never leaves the repo, never yields a file
    whose real path looks like a secret (a `config.py` link to ~/.aws/credentials is skipped)."""
    root_real = os.path.realpath(repo)
    for root, dirs, files in os.walk(repo):                    # os.walk does not follow dir links
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not os.path.islink(os.path.join(root, d))]
        for f in files:
            path = os.path.join(root, f)
            rel = os.path.relpath(path, repo).replace("\\", "/")
            if not f.endswith(CODE_EXT) or SECRET_FILE.search(rel) or os.path.islink(path):
                continue
            real = os.path.realpath(path)
            if not _inside(real, root_real) or SECRET_FILE.search(os.path.relpath(real, root_real).replace("\\", "/")):
                continue
            try:
                if os.path.getsize(real) > MAX_FILE:
                    continue
            except OSError:
                continue
            yield rel, real


def grep(repo: str, rx: re.Pattern, limit: int = 50) -> list:
    out = []
    for rel, path in _iter_code(repo):
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for n, line in enumerate(f, 1):
                    if rx.search(line):
                        out.append({"file": rel, "line": n, "text": line.strip()[:200]})
                        if len(out) >= limit:
                            return out
        except OSError:
            continue
    return out


def _name(symbol: str) -> str:
    if not SYMBOL.match(symbol or ""):
        raise ValueError(f"not a symbol name: {symbol!r}")
    return symbol.split(".")[-1]


def grep_definitions(repo: str, symbol: str, limit: int = 20) -> list:
    name = re.escape(_name(symbol))
    return grep(repo, re.compile(DEF_RX.format(name=name)), limit)


def grep_usages(repo: str, symbol: str, limit: int = 50) -> list:
    """Lines that mention the name and are not its definition."""
    name = re.escape(_name(symbol))
    defs = re.compile(DEF_RX.format(name=name))
    return [r for r in grep(repo, re.compile(rf"(?<![\w$]){name}(?![\w$])"), limit * 3)
            if not defs.search(r["text"])][:limit]


# ----------------------------------------------------------------------------- queries

def _answer(source: str, state: dict, **data) -> dict:
    out = {"source": source, **data}
    if source == "grep" and state.get("reason"):
        out["fallback"] = state["reason"]
    if state.get("synced"):
        out["synced"] = True
    return out


def _nodes(data) -> list:
    """Query answer -> node dicts; anything malformed is skipped (Codex F.12 R2 #3)."""
    out = []
    for r in data if isinstance(data, list) else []:
        n = r.get("node") if isinstance(r, dict) else None
        if isinstance(n, dict) and isinstance(n.get("name"), str):
            out.append(n)
    return out


def _same_symbol(node: dict, symbol: str) -> bool:
    """`Foo.bar` must match the qualified name (suffix on a segment boundary); a bare `bar` matches by
    name. Imports / files / variables are not definitions (Codex F.12 #4)."""
    if node.get("kind") in ("file", "import", "variable"):
        return False
    if "." not in symbol:
        return node.get("name") == symbol
    q = (node.get("qualifiedName") or "").replace("::", ".")
    return q == symbol or q.endswith("." + symbol)


def callers(repo: str, symbol: str, limit: int = 20, sync: bool = True) -> dict:
    state = index_state(repo, sync)
    if state["usable"]:
        data, note = _cg_json(["callers", symbol, "-p", repo, "-j", "-l", str(limit)])
        if data is not None:
            rows = _rows(data.get("definitions"), "callers")
            return _answer("codegraph", state, symbol=symbol, callers=rows[:limit],
                           ambiguous=bool(data.get("ambiguous")))
        state = {**state, "reason": f"codegraph: {note}"}
    return _answer("grep", state, symbol=symbol, callers=grep_usages(repo, symbol, limit),
                   note="text matches of the name (not resolved calls)")


def impact(repo: str, symbol: str, depth: int = 2, sync: bool = True) -> dict:
    state = index_state(repo, sync)
    if state["usable"]:
        data, note = _cg_json(["impact", symbol, "-p", repo, "-j", "-d", str(depth)])
        if data is not None:
            rows = _rows(data.get("definitions"), "affected")
            files = sorted({r["file"] for r in rows})
            return _answer("codegraph", state, symbol=symbol, affected=rows, files=files,
                           tests=[f for f in files if TEST_FILE.search(f)], ambiguous=bool(data.get("ambiguous")),
                           definitions=len(data.get("definitions") or []))
        state = {**state, "reason": f"codegraph: {note}"}
    rows = grep_usages(repo, symbol, 200)
    files = sorted({r["file"] for r in rows} | {r["file"] for r in grep_definitions(repo, symbol)})
    return _answer("grep", state, symbol=symbol, affected=rows[:50], files=files,
                   tests=[f for f in files if TEST_FILE.search(f)], note="one level only (text matches)")


def locate(repo: str, symbol: str, sync: bool = True) -> dict:
    state = index_state(repo, sync)
    if state["usable"]:
        data, note = _cg_json(["query", _name(symbol), "-p", repo, "-j", "-l", "25"])
        if isinstance(data, list):
            hits = [{"name": n.get("qualifiedName") or n.get("name"), "kind": n.get("kind"),
                     "file": (n.get("filePath") or "").replace("\\", "/"), "line": n.get("startLine")}
                    for n in _nodes(data) if _same_symbol(n, symbol)]
            if hits:
                return _answer("codegraph", state, symbol=symbol, definitions=hits, ambiguous=len(hits) > 1)
            note = f"no definition matching {symbol!r} exactly"
        state = {**state, "reason": f"codegraph: {note}"}
    found = grep_definitions(repo, symbol)
    if "." in symbol:
        # text search sees only the last name: these are CANDIDATES, not proven definitions of `symbol`
        return _answer("grep", state, symbol=symbol, definitions=[], candidates=found, resolved=False,
                       note=f"name {_name(symbol)!r} only — qualified identity not verified")
    return _answer("grep", state, symbol=symbol, definitions=found, ambiguous=len(found) > 1)


def definitions_many(repo: str, symbols: list, sync: bool = False, per: int = 5) -> dict:
    """{symbol: [definitions]} for several names with ONE index check — codegraph only (an empty
    dict when the index is not usable: callers keep their own text search)."""
    state = index_state(repo, sync)
    if not state["usable"]:
        return {}
    out = {}
    for sym in symbols:
        try:
            name = _name(sym)
        except ValueError:
            continue
        data, _ = _cg_json(["query", name, "-p", repo, "-j", "-l", "25"], timeout=60)
        hits = [{"name": n.get("qualifiedName") or n.get("name"), "kind": n.get("kind"),
                 "file": (n.get("filePath") or "").replace("\\", "/"), "line": n.get("startLine")}
                for n in _nodes(data) if _same_symbol(n, sym)]
        if hits:
            out[sym] = hits[:per]
    return out


# stack trace frames: java `at a.b.C.m(C.java:12)`, python `File "x/y.py", line 3, in f`,
# node `at fn (C:\x\y.js:10:5)` / `at /abs/x.js:10:5`, go `/abs/x.go:42 +0x1d`
JVM = (".java", ".kt", ".scala", ".groovy")
_FRAMES = (re.compile(r"\bat\s+(?P<sym>[\w$.<>]+)\((?P<file>[\w$.-]+\.(?:java|kt|scala|groovy)):(?P<line>\d+)\)"),
           re.compile(r'File "(?P<file>[^"]+\.py)", line (?P<line>\d+)(?:, in (?P<sym>[\w<>]+))?'),
           re.compile(r"\bat\s+(?:(?P<sym>[\w$.<>\[\] ]+?)\s+\()?(?P<file>(?:[A-Za-z]:[\\/]|[\\/]|\.{0,2}[\\/]?)"
                      r"[^\n():]*?\.[cm]?[jt]sx?):(?P<line>\d+):\d+\)?"),
           re.compile(r"(?P<file>(?:[A-Za-z]:[\\/])?[^\s:]+\.go):(?P<line>\d+)\b"))


def _is_abs(f: str) -> bool:
    return bool(re.match(r"^([A-Za-z]:[\\/]|/|\\\\)", f))


def parse_frames(text: str, limit: int = 200) -> list:
    out = []
    for rx in _FRAMES:
        for m in rx.finditer(text or ""):
            out.append({"file": m.group("file").strip(), "line": int(m.group("line")),
                        "symbol": ((m.groupdict().get("sym") or "") if "sym" in rx.groupindex else "").strip()})
            if len(out) >= limit:
                return out
    return out


def trace(repo: str, text: str, limit: int = 30) -> dict:
    """Stack trace -> frames that live in THIS repo. A frame counts only when it can be tied to ONE
    repo file for sure (Codex F.12 #2): JVM frames by package path + file name, absolute paths by
    lying inside the repo, relative paths by a unique suffix. Library frames are dropped."""
    raw = parse_frames(text)
    if not raw:
        return {"source": "trace", "frames": [], "files": []}          # no walk when there is no trace
    root_real = os.path.realpath(repo)
    by_base = {}
    want = {os.path.basename(f["file"].replace("\\", "/")) for f in raw}
    for rel, _ in _iter_code(repo):
        b = rel.rsplit("/", 1)[-1]
        if b in want:
            by_base.setdefault(b, []).append(rel)
    frames, seen = [], set()
    for fr in raw:
        f = fr["file"].replace("\\", "/")
        base = f.rsplit("/", 1)[-1]
        cands = by_base.get(base, [])
        if f.endswith(JVM) and "/" not in f:
            parts = fr["symbol"].split(".")                         # a.b.C$Inner.m -> package a.b
            pkg = "/".join(parts[:-2])                              # a.b.C.m -> a/b ; C.m -> ""
            cands = [r for r in cands if (f"/{r}".endswith(f"/{pkg}/{base}") if pkg else r == base or
                                          re.search(r"(^|/)(src/(main|test)/(java|kotlin|scala|groovy)/)" + re.escape(base) + "$", r))]
        elif _is_abs(fr["file"]):
            real = os.path.realpath(fr["file"])
            if not _inside(real, root_real):
                continue                                             # outside the repo: not ours
            rel = os.path.relpath(real, root_real).replace("\\", "/")
            cands = [r for r in cands if r == rel]
        else:
            if ".." in f.split("/"):
                continue                                             # climbs out of wherever it ran: not provable
            rel = f[2:] if f.startswith("./") else f
            cands = [r for r in cands if r == rel or r.endswith("/" + rel)]
        if len(cands) != 1:
            continue                                                 # not ours, or ambiguous
        key = (cands[0], fr["line"])
        if key in seen:
            continue
        seen.add(key)
        frames.append({"file": cands[0], "line": fr["line"], "symbol": fr["symbol"]})
        if len(frames) >= limit:
            break
    return {"source": "trace", "frames": frames, "files": list(dict.fromkeys(x["file"] for x in frames))}


def affected(repo: str, files: list, sync: bool = True) -> dict:
    files = [f.replace("\\", "/") for f in files if f]
    state = index_state(repo, sync)
    if state["usable"]:
        tests = cg.affected_tests(repo, files)
        if tests or not files:
            return _answer("codegraph", state, files=files, tests=tests)
        state = {**state, "reason": "codegraph found no affected test"}
    stems = {os.path.splitext(os.path.basename(f))[0] for f in files}
    tests = []
    for rel, path in _iter_code(repo):
        if not TEST_FILE.search(rel):
            continue
        base = os.path.splitext(os.path.basename(rel))[0]
        if any(s and (s in base) for s in stems):
            tests.append(rel)
            continue
        try:
            body = open(path, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        if any(s and re.search(rf"(?<![\w$]){re.escape(s)}(?![\w$])", body) for s in stems):
            tests.append(rel)
    return _answer("grep", state, files=files, tests=sorted(tests)[:50], note="tests naming the changed modules")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="code_context.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("callers", "impact", "locate"):
        p = sub.add_parser(name)
        p.add_argument("symbol")
    p = sub.add_parser("affected")
    p.add_argument("files", nargs="+")
    sub.add_parser("trace")
    sub.add_parser("status")
    for p in sub.choices.values():
        p.add_argument("--repo", default=os.getcwd())
        p.add_argument("--no-sync", action="store_true", help="never run `codegraph sync`")
        p.add_argument("--limit", type=int, default=20)
        p.add_argument("--depth", type=int, default=2)
    a = ap.parse_args(argv)
    repo, sync = os.path.abspath(a.repo), not a.no_sync
    try:
        if a.cmd == "callers":
            out = callers(repo, a.symbol, a.limit, sync)
        elif a.cmd == "impact":
            out = impact(repo, a.symbol, a.depth, sync)
        elif a.cmd == "locate":
            out = locate(repo, a.symbol, sync)
        elif a.cmd == "affected":
            out = affected(repo, a.files, sync)
        elif a.cmd == "trace":
            out = trace(repo, sys.stdin.read(), a.limit)
        else:
            out = index_state(repo, sync)
    except ValueError as e:
        out = {"error": True, "message": str(e)}
    return pc.emit(out)


if __name__ == "__main__":
    sys.exit(main())
