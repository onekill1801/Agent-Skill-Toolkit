#!/usr/bin/env python3
"""Large-refactor program of slices (checklist F.10b): plan gate -> per-slice worktree + agent ->
verify ladder per slice -> stacked slices; real git, fake agent.
    python -m unittest discover -s tests
"""

import json
import os
import runpy
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
runpy.run_path(os.path.join(_ROOT, ".claude", "skills", "_runtime", "bootstrap.py"))["activate"]()
from agent_skill_toolkit.core import store as S  # noqa: E402
from agent_skill_toolkit.workflows import coordinator as C  # noqa: E402
from agent_skill_toolkit.workflows.refactor import program as P  # noqa: E402

sys.path.insert(0, os.path.join(_ROOT, ".claude", "skills", "auto-dev", "tools"))
import refactor as RF  # noqa: E402
import verify as V  # noqa: E402

PY = sys.executable.replace("\\", "/")
SPEC = textwrap.dedent(f"""
    verify:
      base: main
      ladder:
        compile:
          run:
            - {PY}
            - -c
            - "import pathlib, sys; sys.exit(0 if pathlib.Path('src/app.py').exists() else 1)"
          protects: []
      budgets:
        max_fix_rounds_per_rung: 1
    """)


def git(repo, *a):
    return subprocess.run(["git", "-C", repo, *a], check=True, capture_output=True, text=True, encoding="utf-8",
                          errors="replace").stdout.strip()


class ProgramTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="prog-")
        mock.patch.dict(os.environ, {"TOOLKIT_STORE": os.path.join(self.tmp, "t.db"), "TOOLKIT_STATE_DIR": self.tmp,
                                     "WORK_DIR": self.tmp, "TOOLKIT_MAX_PARALLEL": "0"}).start()
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(os.path.join(self.repo, "src"))
        os.makedirs(os.path.join(self.repo, ".agent"))
        with open(os.path.join(self.repo, ".agent", "project.yml"), "w") as f:
            f.write(SPEC)
        with open(os.path.join(self.repo, "src", "app.py"), "w") as f:
            f.write("x = 1\n")
        with open(os.path.join(self.repo, ".gitignore"), "w") as f:
            f.write("__pycache__/\n")
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "t@t")
        git(self.repo, "config", "user.name", "t")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "init")
        self.slices = os.path.join(self.tmp, "slices.json")
        with open(self.slices, "w") as f:
            json.dump([{"id": "s1", "title": "Tách TaskService", "acs": ["hành vi không đổi"]},
                       {"id": "s2", "title": "Bỏ phụ thuộc vòng", "depends_on": ["s1"]}], f)
        self.prompts = []
        mock.patch.object(P.agent_call, "run", side_effect=self.fake_agent).start()
        from agent_skill_toolkit.workflows.verify import ladder as L
        # no real agent may ever start from a test (the ladder fixer would)
        mock.patch.object(L, "_agent", side_effect=AssertionError("ladder fixer must not run here")).start()

    def tearDown(self):
        mock.patch.stopall()

    def fake_agent(self, ctx, role, prompt, cwd="", timeout=900, read_only=False):
        self.prompts.append((role, cwd, prompt))
        sid = ctx.data["slice"]["id"]
        with open(os.path.join(cwd, "src", f"{sid}.py"), "w") as f:
            f.write(f"# {sid}\n")
        return f"done {sid}"

    def registry(self):
        return {**P.REGISTRY, **V.REGISTRY}

    def work(self):
        return C.Coordinator(self.registry(), parallel=1).run(once=True)["ran"]

    def new(self):
        args = type("A", (), {"repo": self.repo, "goal": "Tách module task", "invariants": "API giữ nguyên",
                              "slices": self.slices, "base": "main", "spec": "", "program_id": ""})()
        return RF.program("new", args)

    def approve(self, pid):
        return RF.program("approve", type("A", (), {"program_id": pid})())

    def test_plan_gate_then_stacked_slices_verified(self):
        out = self.new()
        pid = out["program"]
        self.work()
        with S.Store() as st:
            t = st.get(pid)
            self.assertEqual(t["status"], "blocked")
            self.assertTrue(t["last_error"].startswith(P.WAIT_PLAN))
        self.assertEqual(self.prompts, [])                       # nothing ran before the plan was approved
        self.approve(pid)
        self.work()
        with S.Store() as st:
            kids = st.children(pid)
            self.assertEqual({k["status"] for k in kids}, {"done"}, [(k["title"], k["status"], k["last_error"]) for k in kids])
            self.assertEqual(st.get(pid)["status"], "done")
        b1, b2 = out["slices"][0]["branch"], out["slices"][1]["branch"]
        git(self.repo, "merge-base", "--is-ancestor", b1, b2)        # s2 stacked on s1 (raises if not)
        self.assertIn("refactor(s1)", git(self.repo, "log", "--format=%s", b2))
        self.assertEqual([p[0] for p in self.prompts], ["implement", "implement"])
        self.assertIn("«", self.prompts[0][2])                    # plan text quoted as data
        self.assertNotEqual(git(self.repo, "rev-parse", "main"), git(self.repo, "rev-parse", b1))   # main untouched
        rep = RF.program("status", type("A", (), {"program_id": pid})())
        self.assertEqual(rep["progress"], "4/4")

    def test_slice_without_change_blocks_its_dependents(self):
        mock.patch.object(P.agent_call, "run", side_effect=lambda *a, **k: "nothing to do").start()
        pid = self.new()["program"]
        self.work()
        self.approve(pid)
        self.work()
        with S.Store() as st:
            kids = {k["title"]: k for k in st.children(pid)}
        s1 = next(k for t, k in kids.items() if t.startswith("s1:"))
        s2 = next(k for t, k in kids.items() if t.startswith("s2:"))
        self.assertEqual(s1["status"], "blocked")
        self.assertIn("no change", s1["last_error"])
        self.assertEqual(s2["status"], "queued")                 # waits; nothing runs on a broken base

    def test_slices_are_validated(self):
        for bad in ([], [{"id": "S 1", "title": "x"}], [{"id": "a", "title": "x", "depends_on": ["b"]}],
                    [{"id": "a", "title": ""}], [{"id": "a", "title": "x"}, {"id": "a", "title": "y"}]):
            with self.assertRaises(ValueError, msg=bad):
                P.parse_slices(bad)

    def test_headless_agent_cannot_approve(self):
        sys.path.insert(0, os.path.join(_ROOT, ".claude", "hooks"))
        import guard_write
        with mock.patch.dict(os.environ, {"TOOLKIT_HEADLESS": "1"}):
            self.assertEqual(guard_write.decide({"tool_name": "Bash", "tool_input": {
                "command": "python refactor.py program approve abc"}})[0], "deny")


if __name__ == "__main__":
    unittest.main()
