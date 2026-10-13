#!/usr/bin/env python3
"""Repo inventory (checklist R.M1): what exists and how it is wired — regenerated, never hand-edited.

    python .claude/skills/_runtime/inventory.py [--out docs/design/INVENTORY.md]

Sections: runtime modules + import graph between areas (core / integrations / workflows.*), runtime
edges to skill scripts, layering exceptions (tests/test_layering.py), skill tools that bootstrap the
runtime (facades) vs. legacy tools, each tool's CLI subcommands, tests per area.
"""

import argparse
import ast
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
SKILLS = os.path.join(ROOT, ".claude", "skills")
sys.path.insert(0, os.path.join(ROOT, "tests"))


def _subcommands(tree) -> list:
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "add_parser" and node.args and \
                isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
            out.append(node.args[0].value)
    return sorted(dict.fromkeys(out))


def tools() -> list:
    rows = []
    for skill in sorted(os.listdir(SKILLS)):
        tdir = os.path.join(SKILLS, skill, "tools")
        if skill.startswith("_") or not os.path.isdir(tdir):
            continue
        for f in sorted(os.listdir(tdir)):
            if not f.endswith(".py"):
                continue
            path = os.path.join(tdir, f)
            text = open(path, encoding="utf-8", errors="replace").read()
            try:
                tree = ast.parse(text)
            except SyntaxError:
                tree = ast.parse("")
            rows.append({"skill": skill, "tool": f, "facade": "bootstrap.py" in text and "activate" in text,
                         "cli": "__main__" in text, "subcommands": _subcommands(tree),
                         "lines": text.count("\n") + 1})
    return rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="inventory.py")
    ap.add_argument("--out", default=os.path.join(ROOT, "docs", "design", "INVENTORY.md"))
    a = ap.parse_args(argv)
    import test_layering as T
    mods = list(T.modules())
    edges = {}
    for mod, path, is_pkg in mods:
        for imp in T.imports(mod, path, is_pkg):
            if imp.startswith("agent_skill_toolkit."):
                a1, a2 = T.area(mod), T.area(imp if imp in {m for m, _, _ in mods} else imp.rsplit(".", 1)[0])
                if a1 != a2:
                    edges[(a1, a2)] = edges.get((a1, a2), 0) + 1
    rows = tools()
    tests = sorted(f for f in os.listdir(os.path.join(ROOT, "tests")) if f.startswith("test_") and f.endswith(".py"))
    n_tests = sum(len(re.findall(r"^\s+def test_", open(os.path.join(ROOT, "tests", f), encoding="utf-8").read(), re.M))
                  for f in tests)
    L = ["# Kiểm kê repo (tự sinh — `python .claude/skills/_runtime/inventory.py`)", "",
         "## Runtime `agent_skill_toolkit`", "", "| Module | Vùng |", "|---|---|"]
    L += [f"| `{m}` | {T.area(m)} |" for m, _, _ in sorted(mods)]
    L += ["", "## Phụ thuộc giữa các vùng (số cạnh import)", "", "| Từ | Tới | Cạnh |", "|---|---|---|"]
    L += [f"| {x} | {y} | {n} |" for (x, y), n in sorted(edges.items())]
    L += ["", "## Ngoại lệ layering (chỉ được giảm — `tests/test_layering.py`)", "", "| Vùng | Phụ thuộc | Hợp đồng |",
          "|---|---|---|"]
    L += [f"| {a_} | `{b_}` | {why} |" for (a_, b_), why in sorted(T.EXCEPTIONS.items())]
    L += ["", "## Cạnh runtime → script skill (subprocess)", ""]
    L += [f"- `{m}` → `{t}`" for m, t in sorted(T.RUNTIME_EDGES.items())]
    facades = [r for r in rows if r["facade"]]
    L += ["", f"## Tool của skill ({len(rows)} file; {len(facades)} là vỏ dùng runtime)", "",
          "| Skill | Tool | Vỏ runtime | Lệnh con | Dòng |", "|---|---|---|---|---|"]
    L += [f"| {r['skill']} | `{r['tool']}` | {'✅' if r['facade'] else ''} | {', '.join(r['subcommands'])[:120]} | "
          f"{r['lines']} |" for r in rows]
    L += ["", f"## Test: {len(tests)} file, {n_tests} hàm test", ""]
    L += [f"- `{t}`" for t in tests]
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    print(a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
