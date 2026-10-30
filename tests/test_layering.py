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
    ("agent_skill_toolkit.integrations.gsheets", "agent_skill_toolkit.integrations.google_sa"):
        "google_sa.access_token: Sheets calls authenticate as the service account",
    ("agent_skill_toolkit.integrations.gsheets", "agent_skill_toolkit.integrations.google_oauth"):
        "google_oauth.access_token: user-OAuth fallback when no service account is set",
}
# (runtime module, FULL script path it names) — subprocess / path edges AST imports cannot see. The whole
# path is the identity: another `agent_runner.py` elsewhere is a different edge (Codex R R2 #2).
RUNTIME_EDGES = {
    ("agent_skill_toolkit.workflows.verify.ladder", ".claude/skills/auto-dev/tools/agent_runner.py"):
        "agents run through agent_runner",
    ("agent_skill_toolkit.workflows.execution.agent_call", ".claude/skills/auto-dev/tools/agent_runner.py"):
        "agents run through agent_runner",
    ("agent_skill_toolkit.workflows.verify.unittest_junit", "unittest_junit.py"): "its own argparse prog name",
    ("agent_skill_toolkit.core.filesync", "agentkit.py"): "marker file: the peer refuses a root that is not a toolkit",
    # CLI names quoted in instructions shown to a HUMAN (blocked-task messages), never executed
    ("agent_skill_toolkit.workflows.intake.atask_flow", "coordinator.py"): "tells the human how to accept",
    ("agent_skill_toolkit.workflows.proposal.flow", "proposals.py"): "tells the human how to go / decide",
    ("agent_skill_toolkit.workflows.proposal.scan", "proposals.py"): "digest tells the human how to go / reject",
    ("agent_skill_toolkit.workflows.refactor.program", "refactor.py"): "tells the human how to approve",
    ("agent_skill_toolkit.workflows.verify.ladder", "refactor.py"): "tells the human how to (re)record a snapshot",
}
# runtime modules that START processes, whatever the target (a variable argv cannot be resolved statically)
SPAWNERS = {
    "agent_skill_toolkit.core.proc": "the process-tree runner everything else uses",
    "agent_skill_toolkit.core.tls": "PowerShell export of the Windows cert stores (empty-trust-store Pythons)",
    "agent_skill_toolkit.workflows.engine": "StepContext.run: steps run their commands through proc",
    "agent_skill_toolkit.workflows.verify.ladder": "git + the repo's verify commands + agents",
    "agent_skill_toolkit.workflows.refactor.ratchet": "git ls-files + the isolated scan child",
    "agent_skill_toolkit.workflows.refactor.program": "git clone / worktree / fetch for slices",
    "agent_skill_toolkit.workflows.proposal.scan": "git log for hotspots",
    "agent_skill_toolkit.workflows.proposal.flow": "git rev-parse for the input key",
    "agent_skill_toolkit.workflows.execution.agent_call": "agent_runner",
    "agent_skill_toolkit.workflows.security.scan": "git log -p for secrets in history (security audit)",
    "agent_skill_toolkit.integrations.secret_sync": "ssh to the peer machine (filesync ops run there)",
    "agent_skill_toolkit.integrations.google_sa": "openssl dgst -sign for the service-account JWT (stdlib has no RSA)",
}
SPAWN_CALLS = {"subprocess.run", "subprocess.Popen", "subprocess.call", "subprocess.check_call",
               "subprocess.check_output", "os.system", "os.popen", "proc.run", "ctx.run", "os.startfile"}
# (runtime module, loader) — dynamic imports / sys.path changes, each with its reason (design §B.2)
DYNAMIC = {
    ("agent_skill_toolkit.workflows.verify.unittest_junit", "sys.path.insert"):
        "discovers the PRODUCT repo's tests: its cwd goes on sys.path, like `python -m unittest`",
}
LOADERS = {"importlib.import_module", "importlib.__import__", "builtins.__import__", "runpy.run_path",
           "runpy.run_module", "sys.path.insert", "sys.path.append", "sys.path.extend", "imp.load_source",
           "importlib.util.spec_from_file_location"}


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


def _aliases(tree) -> dict:
    """local name -> qualified name, from `import x as y` / `from x import y as z` (Codex R R2 #1)."""
    out = {"__import__": "builtins.__import__"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for al in node.names:
                out[al.asname or al.name.split(".")[0]] = al.name if al.asname else al.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            for al in node.names:
                out[al.asname or al.name] = f"{node.module}.{al.name}"
    return out


def _qualname(expr, aliases: dict) -> str:
    parts = []
    while isinstance(expr, ast.Attribute):
        parts.append(expr.attr)
        expr = expr.value
    if isinstance(expr, ast.Name):
        parts.append(aliases.get(expr.id, expr.id))
        return ".".join(reversed(parts))
    return ""


def _calls(tree):
    aliases = _aliases(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            yield node, _qualname(node.func, aliases)
        elif isinstance(node, (ast.AugAssign, ast.Assign)):            # sys.path += [...] / sys.path = [...]
            targets = [node.target] if isinstance(node, ast.AugAssign) else node.targets
            for t in targets:
                if _qualname(t, aliases) == "sys.path":
                    yield node, "sys.path.extend"


def dynamic_loaders() -> dict:
    out = {}
    for mod, path, _ in modules():
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        for _, name in _calls(tree):
            if name in LOADERS:
                out[(mod, name)] = True
    return out


def _spawns(name: str) -> bool:
    """subprocess / os launchers, `ctx.run`, and the runtime's own runner however it is imported
    (`proc.run`, `proc_mod.run`, `from agent_skill_toolkit.core.proc import run`) — Codex R R3."""
    return (name in SPAWN_CALLS or name.startswith("agent_skill_toolkit.core.proc.") or name.startswith("subprocess.")
            or (name.endswith(".run") and name.split(".")[-2] in ("proc", "proc_mod", "ctx")))


def spawners() -> set:
    out = set()
    for mod, path, _ in modules():
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        if any(_spawns(name) for _, name in _calls(tree)):
            out.add(mod)
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
        joined = set()
        for node in ast.walk(tree):                      # os.path.join(".claude", ..., "x.py") -> one full path
            if isinstance(node, ast.Call) and node.args and all(
                    isinstance(a, ast.Constant) and isinstance(a.value, str) for a in node.args):
                full = "/".join(a.value.strip("/\\") for a in node.args)
                if re.search(r"(^|/)[A-Za-z][\w-]*\.py$", full):
                    out.add((mod, full.replace("\\", "/")))
                    joined |= {id(a) for a in node.args}
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs \
                    and id(node) not in joined:
                for m in re.finditer(r"((?:[\w.-]+[/\\])*(?<![\w])[A-Za-z][\w-]*\.py)\b", node.value):
                    out.add((mod, m.group(1).replace("\\", "/")))
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

    def test_process_spawners_are_declared(self):
        self.assertEqual(spawners(), set(SPAWNERS), "a runtime module starts processes: declare it in SPAWNERS")

    def test_exceptions_never_grow_beyond_main(self):
        """Independent baseline (Codex R R2 #3): the EXCEPTIONS of this file on `main` (or LAYERING_BASE).
        A branch may remove entries, never add: growing the list needs the change to land on main first."""
        import subprocess
        base = os.environ.get("LAYERING_BASE", "main")
        p = subprocess.run(["git", "-C", _ROOT, "show", f"{base}:tests/test_layering.py"], capture_output=True,
                           text=True, encoding="utf-8", errors="replace")
        if p.returncode != 0:
            self.skipTest(f"no {base}:tests/test_layering.py to compare with")
        tree = ast.parse(p.stdout)
        old = next((ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == "EXCEPTIONS" for t in n.targets)), {})
        old_keys = {k for k in old}
        grown = [k for k in EXCEPTIONS if k not in old_keys and
                 (k[0], k[1]) not in {(a.split(".")[-1], b) for a, b in old_keys}]
        # enforced EVEN when main's list is empty (it may never grow back — Codex R R3); the old area-keyed
        # format (first round) is the only bootstrap exemption
        if all(isinstance(k[0], str) and k[0].startswith("agent_skill_toolkit.") for k in old):
            self.assertEqual(grown, [], f"exceptions added since {base} — they may only shrink")

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
                  "core/alias.py": "from importlib import import_module as im\nimport runpy as r\nimport sys\n"
                                   "im('x')\nr.run_path('y')\nsys.path += ['z']\n",
                  "workflows/sp1.py": "from agent_skill_toolkit.core import proc\nproc.run(argv)\n",
                  "workflows/sp2.py": "from agent_skill_toolkit.core.proc import run as go\ngo(x)\n",
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
                                        ("agent_skill_toolkit.core.dyn", "sys.path.insert"),
                                        ("agent_skill_toolkit.core.alias", "importlib.import_module"),
                                        ("agent_skill_toolkit.core.alias", "runpy.run_path"),
                                        ("agent_skill_toolkit.core.alias", "sys.path.extend")})
        self.assertIn(("agent_skill_toolkit.core.dyn", "tools/review_common.py"), scripts)
        with mock.patch(f"{__name__}.PKG_DIR", tmp):
            spawn = spawners()
        self.assertTrue({"agent_skill_toolkit.workflows.sp1", "agent_skill_toolkit.workflows.sp2",
                         "agent_skill_toolkit.core.dyn"} <= spawn, spawn)

    def test_empty_baseline_on_main_is_still_enforced(self):
        """Codex R R3: once main has no exceptions, adding one fails."""
        import subprocess
        from unittest import mock
        fake = subprocess.CompletedProcess([], 0, "EXCEPTIONS = {}\n", "")
        with mock.patch("subprocess.run", return_value=fake):
            with mock.patch.dict(f"{__name__}.EXCEPTIONS", {("agent_skill_toolkit.workflows.a.x", "y"): "a reason long enough"},
                                 clear=True):
                with self.assertRaises(AssertionError):
                    self.test_exceptions_never_grow_beyond_main()
        self.assertEqual(area("agent_skill_toolkit.core.store"), "core")
        self.assertEqual(area("agent_skill_toolkit.workflows.engine"), "workflows")
        self.assertEqual(area("agent_skill_toolkit.workflows.verify.ladder"), "workflows.verify")


if __name__ == "__main__":
    unittest.main()
