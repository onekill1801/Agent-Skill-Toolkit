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

# (importer sub-package, imported module) -> the contract relied on. Shrink this list.
EXCEPTIONS = {
    ("workflows.refactor", "agent_skill_toolkit.workflows.intake.gate"):
        "gate.admit/submit: every new program enters through the intake gate",
    ("workflows.refactor", "agent_skill_toolkit.workflows.proposal.scan"):
        "scan.hotspots: the survey reuses the hotspot scanner",
    ("workflows.refactor", "agent_skill_toolkit.workflows.verify.ladder"):
        "ladder.load_spec/resolve_base/KIND: slices are proven by the verify ladder",
    ("workflows.verify", "agent_skill_toolkit.workflows.refactor.ratchet"):
        "ratchet.parse_*/scan_isolated/compare: the `kind: ratchet` rung",
}
# runtime module -> skill script it starts by path (an edge AST imports cannot see)
RUNTIME_EDGES = {
    "agent_skill_toolkit.workflows.verify.ladder": "auto-dev/tools/agent_runner.py",
    "agent_skill_toolkit.workflows.execution.agent_call": "auto-dev/tools/agent_runner.py",
}


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
                if node.module:
                    out.add(".".join(base + [node.module]))
                else:
                    out |= {".".join(base + [a.name]) for a in node.names}
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
        if mod.startswith(SHARED) or len(parts) == 2:
            return "workflows"
        return "workflows." + parts[2]
    return parts[1]


def violations() -> dict:
    """{(importer area, imported module): [importer modules]}"""
    known = {m for m, _, _ in modules()}
    out = {}
    for mod, path, is_pkg in modules():
        src = area(mod)
        for imp in imports(mod, path, is_pkg):
            top = imp.split(".")[0]
            if top in STD:
                continue
            if top != "agent_skill_toolkit":
                out.setdefault((src, imp), []).append(mod)        # bare / facade import
                continue
            # an imported NAME (from x import y) is not a module: judge the module it comes from
            target = imp if imp in known else imp.rsplit(".", 1)[0]
            dst = area(target)
            ok = (dst == "core" or target == "agent_skill_toolkit" or
                  (src == "core" and dst == "core") or
                  (src == "integrations" and dst in ("core", "integrations") and
                   (dst != "integrations" or target.split(".")[:3] == mod.split(".")[:3])) or
                  (src.startswith("workflows") and (dst in ("core", "integrations", "workflows") or dst == src)))
            if src == "core" and dst != "core" and target != "agent_skill_toolkit":
                ok = False
            if not ok:
                out.setdefault((src, target), []).append(mod)
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
        """String paths the code BUILDS (os.path.join(...) of literals, or one literal) that point into a
        skill's tools/ — docstrings and comments are not edges."""
        found = {}
        for mod, path, _ in modules():
            with open(path, encoding="utf-8") as f:
                tree = ast.parse(f.read())
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and node.args and all(
                        isinstance(a, ast.Constant) and isinstance(a.value, str) for a in node.args):
                    joined = "/".join(a.value for a in node.args)
                    m = re.search(r"([\w-]+/tools/[\w.]+\.py)$", joined)
                    if m:
                        found[mod] = m.group(1)
        self.assertEqual(found, RUNTIME_EDGES)

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
        with mock.patch(f"{__name__}.PKG_DIR", tmp):
            v = violations()
        self.assertEqual(sorted(v), [("core", "agent_skill_toolkit.workflows.engine"), ("core", "review_common"),
                                     ("workflows.a", "agent_skill_toolkit.workflows.b.y")])
        self.assertEqual(area("agent_skill_toolkit.core.store"), "core")
        self.assertEqual(area("agent_skill_toolkit.workflows.engine"), "workflows")
        self.assertEqual(area("agent_skill_toolkit.workflows.verify.ladder"), "workflows.verify")


if __name__ == "__main__":
    unittest.main()
