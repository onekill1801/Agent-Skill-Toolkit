"""Refactor survey (design §12.3 ①, checklist F.10): READ-ONLY map of a repo before a large refactor —
modules, import dependencies between them, cycles, test files per module, hotspots. No LLM.

    s = survey.run(repo)          # {"modules": [...], "edges": [...], "cycles": [...], "hotspots": [...]}
    md = survey.markdown(s)
"""

import os
import re

from agent_skill_toolkit.workflows.proposal import scan
from agent_skill_toolkit.workflows.refactor import ratchet

JAVA_PKG = re.compile(r"^\s*package\s+([\w.]+)\s*;", re.M)
JAVA_IMPORT = re.compile(r"^\s*import\s+(?:static\s+)?([\w.]+)\s*;", re.M)
PY_IMPORT = re.compile(r"^\s*(?:from\s+(\.*[\w.]*)\s+import|import\s+([\w.]+))", re.M)
TEST_PATH = re.compile(r"(^|/)(tests?|src/test)/|(^|/)test_[^/]*\.py$|Test\.java$|_test\.py$", re.I)


def _read(repo: str, rel: str) -> str:
    path = os.path.join(repo, rel)
    try:
        if os.path.islink(path) or os.path.getsize(path) > ratchet.MAX_FILE:
            return ""
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def _java_modules(files: list, read, depth: int) -> dict:
    """file -> module (package prefix of `depth` segments below the common base package)."""
    pkgs = {}
    for f in files:
        m = JAVA_PKG.search(read(f))
        if m:
            pkgs[f] = m.group(1)
    if not pkgs:
        return {}
    split = [p.split(".") for p in pkgs.values()]
    base = []
    for parts in zip(*split):
        if len(set(parts)) != 1:
            break
        base.append(parts[0])
    return {f: ".".join(p.split(".")[:len(base) + depth]) or p for f, p in pkgs.items()}


def _py_module(rel: str) -> str:
    parts = rel.split("/")
    return parts[0] if len(parts) > 1 else os.path.splitext(parts[0])[0]


def run(repo: str, depth: int = 1, hotspot_days: int = 90) -> dict:
    files = ratchet.tracked_files(repo)
    read = lambda f: _read(repo, f)                  # noqa: E731
    java = [f for f in files if f.endswith(".java") and not TEST_PATH.search(f)]
    py = [f for f in files if f.endswith(".py") and not TEST_PATH.search(f)]
    jmod = _java_modules(java, read, depth)
    mod_of = dict(jmod)
    mod_of.update({f: _py_module(f) for f in py})
    modules = sorted(set(mod_of.values()))
    edges = {}
    java_prefixes = sorted(set(jmod.values()), key=len, reverse=True)
    for f, m in mod_of.items():
        text = read(f)
        targets = set()
        if f.endswith(".java"):
            for imp in JAVA_IMPORT.findall(text):
                t = next((p for p in java_prefixes if imp == p or imp.startswith(p + ".")), None)
                if t:
                    targets.add(t)
        else:
            for a, b in PY_IMPORT.findall(text):
                name = (a or b).lstrip(".").split(".")[0]
                if name in modules:
                    targets.add(name)
        for t in targets - {m}:
            edges[(m, t)] = edges.get((m, t), 0) + 1
    tests = {}
    for f in files:
        if TEST_PATH.search(f):
            # the MOST specific module whose path the test lives under (com/x/a/b beats com/x)
            hits = [m for m in modules if m and (f"/{m.replace('.', '/')}/" in f"/{f}" or f.startswith(m + "/"))]
            hit = max(hits, key=len) if hits else "(khác)"
            tests[hit] = tests.get(hit, 0) + 1
    out_mods = []
    for m in modules:
        out_mods.append({"module": m, "files": sum(1 for v in mod_of.values() if v == m),
                         "fan_out": sum(1 for (a, _) in edges if a == m), "fan_in": sum(1 for (_, b) in edges if b == m),
                         "tests": tests.get(m, 0)})
    return {"repo": repo, "modules": out_mods,
            "edges": [{"from": a, "to": b, "imports": n} for (a, b), n in sorted(edges.items())],
            "cycles": cycles(modules, edges), "hotspots": scan.hotspots(repo, hotspot_days, top=10, min_changes=3)}


def cycles(nodes: list, edges: dict) -> list:
    """Strongly connected components with more than one module (Tarjan, iterative)."""
    graph = {n: [b for (a, b) in edges if a == n] for n in nodes}
    index, low, on, stack, out, counter = {}, {}, set(), [], [], [0]
    for root in nodes:
        if root in index:
            continue
        work = [(root, iter(graph[root]))]
        index[root] = low[root] = counter[0]
        counter[0] += 1
        stack.append(root)
        on.add(root)
        while work:
            v, it = work[-1]
            nxt = next(it, None)
            if nxt is not None:
                if nxt not in index:
                    index[nxt] = low[nxt] = counter[0]
                    counter[0] += 1
                    stack.append(nxt)
                    on.add(nxt)
                    work.append((nxt, iter(graph.get(nxt, []))))
                elif nxt in on:
                    low[v] = min(low[v], index[nxt])
                continue
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[v])
            if low[v] == index[v]:
                comp = []
                while True:
                    w = stack.pop()
                    on.discard(w)
                    comp.append(w)
                    if w == v:
                        break
                if len(comp) > 1:
                    out.append(sorted(comp))
    return sorted(out)


def markdown(s: dict) -> str:
    from agent_skill_toolkit.workflows.intake import lanes
    lit = lambda t: lanes.literal(" ".join(str(t).split()))                     # noqa: E731
    lines = [f"# Khảo sát refactor — {lit(os.path.basename(s['repo']))}", "",
             "| Module | File | Phụ thuộc ra | Bị phụ thuộc | File test |", "|---|---|---|---|---|"]
    lines += [f"| {lit(m['module'])} | {m['files']} | {m['fan_out']} | {m['fan_in']} | {m['tests']} |" for m in s["modules"]]
    lines += ["", "## Vòng phụ thuộc", ""]
    lines += [f"- {' ↔ '.join(lit(x) for x in c)}" for c in s["cycles"]] or ["- (không có)"]
    lines += ["", "## Điểm nóng (git)", ""]
    lines += [f"- {lit(h['title'])}" for h in s["hotspots"]] or ["- (không có)"]
    untested = [m["module"] for m in s["modules"] if not m["tests"] and m["files"] >= 3]
    lines += ["", "## Module chưa có file test (≥ 3 file)", ""]
    lines += [f"- {lit(m)}" for m in untested] or ["- (không có)"]
    return "\n".join(lines) + "\n"
