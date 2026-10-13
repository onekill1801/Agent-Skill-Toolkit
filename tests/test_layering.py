#!/usr/bin/env python3
"""Layering ratchet for the runtime package (design §B.2, checklist R.M6).

    core          stdlib + core only
    integrations  core (+ itself); never another integration, never an agent
    workflows     core, integrations, the shared workflow layer (engine, coordinator, execution) and its
                  OWN sub-package; a dependency on ANOTHER workflow needs an entry in EXCEPTIONS with the
                  contract it relies on
    everything    no bare / facade imports (only stdlib and agent_skill_toolkit.*)

EXCEPTIONS only shrink: a new violation fails, and an exception that no longer occurs fails too
(delete it). Runtime edges to skill scripts (subprocess paths) are declared in RUNTIME_EDGES.
    python -m unittest discover -s tests
"""

import ast
import os
import re
import sys
import unittest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
PKG_DIR = os.path.join(_ROOT, ".claude", "skills", "_runtime", "agent_skill_toolkit")
STD = set(sys.stdlib_module_names)
SHARED = ("agent_skill_toolkit.workflows.engine", "agent_skill_toolkit.workflows.coordinator",
          "agent_skill_toolkit.workflows.execution")

# EXACT (importer module, imported module) edges -> the contract relied on. Keyed per MODULE, so another
# module of the same area cannot silently inherit an allowance (Codex R R1 #3). Shrink this list; growing it
# is a reviewed edit of this file (tests/ is protected from the ladder's fixer).
EXCEPTIONS = {
    ("agent_skill_toolkit.workflows.refactor.program", "agent_skill_toolkit.workflows.intake.gate"):
        "gate.admit/submit: every new program enters through the intake gate",
    ("agent_skill_toolkit.workflows.refactor.survey", "agent_skill_toolkit.workflows.proposal.scan"):
        "scan.hotspots: the survey reuses the hotspot scanner",
    ("agent_skill_toolkit.workflows.refactor.program", "agent_skill_toolkit.workflows.verify.ladder"):
        "ladder.load_spec/resolve_base/KIND: slices are proven by the verify ladder",
    ("agent_skill_toolkit.workflows.verify.ladder", "agent_skill_toolkit.workflows.refactor.ratchet"):
        "ratchet.parse_*/scan_isolated/compare: the `kind: ratchet` rung",
}
# (runtime module, script file name it names) — subprocess / path edges AST imports cannot see.
RUNTIME_EDGES = {
    ("agent_skill_toolkit.workflows.verify.ladder", "agent_runner.py"): "agents run through agent_runner",
    ("agent_skill_toolkit.workflows.execution.agent_call", "agent_runner.py"): "agents run through agent_runner",
    ("agent_skill_toolkit.workflows.verify.unittest_junit", "unittest_junit.py"): "its own argparse prog name",
}
# (runtime module, loader) — dynamic imports / sys.path changes, each with its reason (design §B.2)
DYNAMIC = {
    ("agent_skill_toolkit.workflows.verify.unittest_junit", "sys.path.insert"):
        "discovers the PRODUCT repo's tests: its cwd goes on sys.path, like `python -m unittest`",
}
LOADERS = {("importlib", "import_module"), ("importlib", "__import__"), ("runpy", "run_path"),
           ("runpy", "run_module"), ("sys.path", "insert"), ("sys.path", "append"), ("sys.path", "extend")}


def modules():
    for dp, dn, fn in os.walk(PKG_DIR):
        dn[:] = [d for d in dn if d != "__pycache__"]
        for f in fn:
            if f.endswith(".py"):
                path = os.path.join(dp, f)
                rel = os.path.relpath(path, PKG_DIR).replace("\\", "/")[:-3]
                mod = "agent_skill_toolkit." + rel.replace("/", ".")
                if mod.endswith(".__init__"):
                    mod = mod[: -len(".__init__")]
                yield mod, path, f == "__init__.py"


def imports(mod: str, path: str, is_pkg: bool) -> set:
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    pkg = mod if is_pkg else mod.rsplit(".", 1)[0]
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parts = pkg.split(".")
                base = parts[: len(parts) - (node.level - 1)]
                prefix = base + (node.module.split(".") if node.module else [])
                if node.module:
                    out.add(".".join(prefix))
                out |= {".".join(prefix + [a.name]) for a in node.names}   # aliases may be modules (#4)
            elif node.module:
                out.add(node.module)
                out |= {f"{node.module}.{a.name}" for a in node.names}
    # `from pkg import mod` imports the MODULE pkg.mod: the package itself is not a separate dependency
    return {i for i in out if not any(j.startswith(i + ".") for j in out)}


def area(mod: str) -> str:
    """core / integrations / workflows.<sub> / workflows (shared) / root"""
    parts = mod.split(".")
    if len(parts) < 2:
        return "root"
    if parts[1] == "workflows":
        # EXACT shared modules (engine, coordinator, the execution package) — no prefix match (#5)
        if len(parts) == 2 or any(mod == x or mod.startswith(x + ".") for x in SHARED):
            return "workflows"
        return "workflows." + parts[2]
    return parts[1]


def violations() -> dict:
    """{(importer module, imported module): reason}"""
    known = {m for m, _, _ in modules()}
    out = {}
    for mod, path, is_pkg in modules():
        src = area(mod)
        for imp in imports(mod, path, is_pkg):
            top = imp.split(".")[0]
            if top in STD:
                continue
            if top != "agent_skill_toolkit":
                out[(mod, imp)] = "bare / facade import"
                continue
            # an imported NAME (from x import y) is not a module: judge the module it comes from — but an
            # UNKNOWN path is judged as written, never reduced to the always-allowed root (#4)
            parent = imp.rsplit(".", 1)[0]
            target = imp if imp in known else parent if parent in known else imp
            if target == "agent_skill_toolkit":
                continue
            dst = area(target)
            if src == "core":
                ok = dst == "core"
            elif src == "integrations":
                ok = dst == "core" or (dst == "integrations" and target.split(".")[:3] == mod.split(".")[:3])
            elif src.startswith("workflows"):
                ok = dst in ("core", "integrations", "workflows") or dst == src
            else:
                ok = dst in ("core", "workflows", "integrations")
            if not ok:
                out[(mod, target)] = f"{src} -> {dst}"
    return out


def _call_name(node) -> tuple:
    f = node.func
    if isinstance(f, ast.Attribute):
        owner = f.value
        if isinstance(owner, ast.Attribute) and isinstance(owner.value, ast.Name):
            return (f"{owner.value.id}.{owner.attr}", f.attr)          # sys.path.insert
        if isinstance(owner, ast.Name):
            return (owner.id, f.attr)                                  # importlib.import_module
    if isinstance(f, ast.Name) and f.id == "__import__":
        return ("importlib", "__import__")
    return ("", "")


def dynamic_loaders() -> dict:
    out = {}
    for mod, path, _ in modules():
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                owner, fn = _call_name(node)
                if (owner, fn) in LOADERS:
                    out[(mod, f"{owner}.{fn}")] = True
    return out


def script_names() -> set:
    """String literals naming a .py script (not in docstrings): every one is a potential runtime edge
    (subprocess argv, os.path.join / Path pieces) — Codex R R1 #2."""
    out = set()
    for mod, path, _ in modules():
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        docs = {id(n.body[0].value) for n in ast.walk(tree)
                if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.body
                and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
                for m in re.finditer(r"(?:^|[/\\])([A-Za-z][\w-]*\.py)\b", node.value):
                    out.add((mod, m.group(1)))
    return out


class LayeringTest(unittest.TestCase):
    def test_no_new_layering_violation(self):
        v = violations()
        new = {k: m for k, m in v.items() if k not in EXCEPTIONS}
        self.assertEqual(new, {}, "new layering violations (fix them, or add an EXCEPTION with its contract)")

    def test_exceptions_only_shrink(self):
        v = violations()
        stale = [k for k in EXCEPTIONS if k not in v]
        self.assertEqual(stale, [], "these exceptions no longer occur — delete them (the list only shrinks)")

    def test_every_exception_has_a_reason(self):
        for k, why in EXCEPTIONS.items():
            self.assertTrue(why and len(why) > 10, k)

    def test_runtime_edges_to_skill_scripts_are_declared(self):
        self.assertEqual(script_names(), set(RUNTIME_EDGES), "a runtime module names a script: declare the edge")

    def test_dynamic_loaders_are_declared(self):
        self.assertEqual(set(dynamic_loaders()), set(DYNAMIC), "dynamic import / sys.path change: declare it")

    def test_the_checker_itself_catches_violations(self):
        import tempfile
        from unittest import mock
        tmp = tempfile.mkdtemp(prefix="layer-")
        files = {"__init__.py": "", "core/__init__.py": "", "core/bad.py": "from agent_skill_toolkit.workflows import engine\n",
                 "core/bare.py": "import review_common\n", "workflows/__init__.py": "", "workflows/engine.py": "",
                 "workflows/a/__init__.py": "", "workflows/a/x.py": "from agent_skill_toolkit.workflows.b import y\n",
                 "workflows/b/__init__.py": "", "workflows/b/y.py": "from ..engine import run\nfrom ...core import bad\n"}
        for rel, body in files.items():
            p = os.path.join(tmp, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w") as f:
                f.write(body)
        files2 = {"core/rel.py": "from ..workflows import engine\n",
                  "core/dyn.py": "import importlib, sys, subprocess\nimportlib.import_module('x')\n"
                                 "sys.path.insert(0, 'y')\nsubprocess.run(['python', 'tools/review_common.py'])\n",
                  "workflows/engine_extra.py": "", "workflows/a/z.py": "from agent_skill_toolkit.workflows import engine_extra\n"}
        for rel, body in files2.items():
            p = os.path.join(tmp, rel)
            with open(p, "w") as f:
                f.write(body)
        with mock.patch(f"{__name__}.PKG_DIR", tmp):
            v = violations()
            loaders = dynamic_loaders()
            scripts = script_names()
        self.assertEqual(sorted(v), [
            ("agent_skill_toolkit.core.bad", "agent_skill_toolkit.workflows.engine"),
            ("agent_skill_toolkit.core.bare", "review_common"),
            ("agent_skill_toolkit.core.rel", "agent_skill_toolkit.workflows.engine"),
            ("agent_skill_toolkit.workflows.a.x", "agent_skill_toolkit.workflows.b.y"),
            ("agent_skill_toolkit.workflows.a.z", "agent_skill_toolkit.workflows.engine_extra"),
        ])
        self.assertEqual(set(loaders), {("agent_skill_toolkit.core.dyn", "importlib.import_module"),
                                        ("agent_skill_toolkit.core.dyn", "sys.path.insert")})
        self.assertIn(("agent_skill_toolkit.core.dyn", "review_common.py"), scripts)
        self.assertEqual(area("agent_skill_toolkit.core.store"), "core")
        self.assertEqual(area("agent_skill_toolkit.workflows.engine"), "workflows")
        self.assertEqual(area("agent_skill_toolkit.workflows.verify.ladder"), "workflows.verify")


if __name__ == "__main__":
    unittest.main()
