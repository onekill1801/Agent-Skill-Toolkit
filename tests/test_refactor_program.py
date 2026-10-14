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
          full_suite: true
        arch:
          kind: ratchet
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
        # a human at a terminal (core.human.require) — the decisions under test are human ones
        mock.patch("agent_skill_toolkit.core.human.isatty", return_value=True).start()
        for k in ("TOOLKIT_HEADLESS", "TOOLKIT_AGENT_SANDBOXED"):
            os.environ.pop(k, None)
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(os.path.join(self.repo, "src"))
        os.makedirs(os.path.join(self.repo, ".agent"))
        with open(os.path.join(self.repo, ".agent", "project.yml"), "w") as f:
            f.write(SPEC)
        with open(os.path.join(self.repo, "src", "app.py"), "w") as f:
            f.write("x = 1\n")
        with open(os.path.join(self.repo, ".agent", "architecture.json"), "w") as f:
            json.dump({"rules": [{"id": "no-legacy", "files": r"\.py$", "deny": "^import legacy"}]}, f)
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
        self.assertEqual(len(out["warnings"]), 2)                # no snapshot / metrics safety net (F.10c)
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
        self.assertEqual(rep["progress"], "6/6")                  # implement + verify + publish per slice

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


class Round1Test(ProgramTest):
    """Codex F.10b round 1 (inherits the fixture; its own tests only)."""

    def run_all(self, pid):
        self.work()
        self.approve(pid)
        self.work()

    def test_approval_is_human_only(self):                                   # #5
        from agent_skill_toolkit.core import human
        pid = self.new()["program"]
        self.work()
        with mock.patch.dict(os.environ, {"TOOLKIT_HEADLESS": "1"}):
            with self.assertRaises(PermissionError):
                self.approve(pid)
        with mock.patch.object(human, "isatty", return_value=False):
            with self.assertRaises(PermissionError):
                self.approve(pid)

    def test_multi_dependency_waits_for_merges(self):                        # #1
        with open(self.slices, "w") as f:
            json.dump([{"id": "a", "title": "A"}, {"id": "b", "title": "B"},
                       {"id": "c", "title": "C", "depends_on": ["a", "b"]}], f)
        out = self.new()
        self.run_all(out["program"])
        with S.Store() as st:
            c = st.get(out["slices"][2]["implement"])
        self.assertEqual(c["status"], "blocked")
        self.assertIn("merged into main first", c["last_error"])
        for sl in out["slices"][:2]:                              # the human merges a and b
            git(self.repo, "merge", "-q", "--no-ff", "-m", "merge", sl["branch"])
        with S.Store() as st:
            st.unblock(c["id"])
        self.work()
        with S.Store() as st:
            self.assertEqual(st.get(out["slices"][2]["verify"])["status"], "done")
        git(self.repo, "merge-base", "--is-ancestor", out["slices"][0]["branch"], out["slices"][2]["branch"])

    def test_foreign_branch_is_not_reused(self):                             # #2
        out = self.new()
        git(self.repo, "branch", out["slices"][0]["branch"])      # someone else's branch with that name
        self.run_all(out["program"])
        with S.Store() as st:
            s1 = st.get(out["slices"][0]["implement"])
        self.assertEqual(s1["status"], "blocked")
        self.assertIn("already exists", s1["last_error"])

    def test_identical_program_cannot_start_twice(self):                     # #2
        pid = self.new()["program"]
        with self.assertRaises(ValueError):
            self.new()                                            # still waiting for approval
        self.work()
        self.approve(pid)
        with self.assertRaises(ValueError):
            self.new()                                            # plan approved, slices still open
        with S.Store() as st:
            for k in st.children(pid) + [st.get(pid)]:
                st.cancel(k["id"])
        self.assertFalse(self.new()["duplicate"])                 # all finished: a new run is allowed

    def test_agent_touching_the_shared_clone_is_caught(self):                # #4
        def evil(ctx, role, prompt, cwd="", timeout=900, read_only=False):
            with open(os.path.join(cwd, "src", "x.py"), "w") as f:
                f.write("x\n")
            git(self.repo, "branch", "sneaky")                    # writes to YOUR clone
            return "done"
        mock.patch.object(P.agent_call, "run", side_effect=evil).start()
        out = self.new()
        self.run_all(out["program"])
        with S.Store() as st:
            s1 = st.get(out["slices"][0]["implement"])
        self.assertEqual(s1["status"], "blocked")
        self.assertIn("changed your clone", s1["last_error"])

    def test_branch_checked_before_the_agent_runs(self):                     # #3
        out = self.new()
        self.work()
        self.approve(out["program"])
        calls = []
        orig = P.step_worktree

        def worktree_then_drift(ctx):
            res = orig(ctx)
            git(ctx.data["worktree"], "checkout", "-q", "-b", "elsewhere")
            return res
        mock.patch.object(P, "SLICE_STEPS", [("worktree", worktree_then_drift), ("implement", P.step_implement)]).start()
        mock.patch.dict(P.REGISTRY, {P.SLICE: P.SLICE_STEPS}).start()
        mock.patch.object(P.agent_call, "run", side_effect=lambda *a, **k: calls.append(1) or "x").start()
        self.work()
        self.assertEqual(calls, [])
        with S.Store() as st:
            self.assertIn("before the agent", st.get(out["slices"][0]["implement"])["last_error"])

    def test_base_is_pinned_and_spec_must_prove_refactors(self):             # #6 / #7
        out = self.new()
        self.assertEqual(out["base"], git(self.repo, "rev-parse", "main"))
        with open(os.path.join(self.repo, ".agent", "project.yml"), "w") as f:
            f.write(SPEC.replace("        arch:\n          kind: ratchet\n", "").replace("full_suite: true", "full_suite: false"))
        git(self.repo, "commit", "-qam", "weaker spec")
        with S.Store() as st:
            for k in st.list(limit=50):
                st.cancel(k["id"])
        with self.assertRaises(ValueError) as e:
            self.new()
        self.assertIn("cannot prove a refactor slice", str(e.exception))

    def test_round2_weak_suite_and_pinned_spec(self):                        # Codex F.10b R2 #6/#7
        weak = SPEC.replace("      protects: []\n      full_suite: true\n", "      protects: []\n") \
                   .replace("      kind: ratchet", "      kind: ratchet\n      full_suite: true")
        self.assertNotEqual(weak, SPEC)
        spec_path = os.path.join(self.tmp, "weak.yml")
        with open(spec_path, "w") as f:
            f.write(weak)
        args = type("A", (), {"repo": self.repo, "goal": "g2", "invariants": "", "slices": self.slices,
                              "base": "main", "spec": spec_path, "program_id": ""})()
        with self.assertRaises(ValueError):
            RF.program("new", args)                                # a ratchet with full_suite is not a suite
        good = os.path.join(self.tmp, "good.yml")
        with open(good, "w") as f:
            f.write(SPEC)
        args.spec = good
        out = RF.program("new", args)
        with open(good, "w") as f:
            f.write("verify: {}\n")                                # edited after approval: no effect
        self.run_all(out["program"])
        with S.Store() as st:
            self.assertEqual({k["status"] for k in st.children(out["program"])}, {"done"})

    def test_round2_publish_only_the_verified_commit(self):                  # Codex F.10b R2 new HIGH
        out = self.new()
        pid = out["program"]
        self.work()
        self.approve(pid)
        real = P.step_publish

        def moved_then_publish(ctx):                               # a commit lands AFTER verification
            clone = ctx.data["worktree"]
            with open(os.path.join(clone, "src", "late.py"), "w") as f:
                f.write("late\n")
            git(clone, "add", "-A")
            git(clone, "commit", "-qm", "late")
            return real(ctx)
        mock.patch.dict(P.REGISTRY, {P.PUBLISH: [("publish", moved_then_publish)]}).start()
        self.work()
        with S.Store() as st:
            pub = st.get(out["slices"][0]["publish"])
        self.assertEqual(pub["status"], "blocked")
        self.assertIn("moved after it was verified", pub["last_error"])
        self.assertNotIn(out["slices"][0]["branch"], git(self.repo, "branch", "--list"))   # nothing published

    def test_round3_published_sibling_branch_is_guarded(self):              # Codex F.10b R3 HIGH
        def agent(ctx, role, prompt, cwd="", timeout=900, read_only=False):
            sid = ctx.data["slice"]["id"]
            with open(os.path.join(cwd, "src", f"{sid}.py"), "w") as f:
                f.write(f"# {sid}\n")
            if sid == "s2":                                        # damages s1's PUBLISHED branch
                b1 = ctx.data["branch"].replace("-s2", "-s1")
                git(self.repo, "update-ref", f"refs/heads/{b1}", git(self.repo, "rev-parse", "main"))
            return "done"
        mock.patch.object(P.agent_call, "run", side_effect=agent).start()
        out = self.new()
        self.run_all(out["program"])
        with S.Store() as st:
            s2 = st.get(out["slices"][1]["implement"])
        self.assertEqual(s2["status"], "blocked")
        self.assertIn("changed your clone", s2["last_error"])
        self.assertIn("-s1", s2["last_error"])

    def test_round4_reset_to_the_published_commit_is_not_a_publication(self):   # Codex F.10b R4
        state = {}

        def agent(ctx, role, prompt, cwd="", timeout=900, read_only=False):
            sid = ctx.data["slice"]["id"]
            with open(os.path.join(cwd, "src", f"{sid}.py"), "w") as f:
                f.write(f"# {sid}\n")
            if sid == "s2":                                        # resets s1 from the human's B back to A
                git(self.repo, "update-ref", f"refs/heads/{state['b1']}", state["a"])
            return "done"
        mock.patch.object(P.agent_call, "run", side_effect=agent).start()
        out = self.new()
        state["b1"] = out["slices"][0]["branch"]
        real_worktree = P.step_worktree

        def human_advances_then_worktree(ctx):
            if ctx.data["slice"]["id"] == "s2":                    # the human commits on s1 after publication
                state["a"] = git(self.repo, "rev-parse", state["b1"])
                git(self.repo, "checkout", "-q", state["b1"])
                with open(os.path.join(self.repo, "src", "human.py"), "w") as f:
                    f.write("h\n")
                git(self.repo, "add", "-A")
                git(self.repo, "commit", "-qm", "human work")
                git(self.repo, "checkout", "-q", "main")
            return real_worktree(ctx)
        mock.patch.dict(P.REGISTRY, {P.SLICE: [("worktree", human_advances_then_worktree),
                                               ("implement", P.step_implement)]}).start()
        self.run_all(out["program"])
        with S.Store() as st:
            s2 = st.get(out["slices"][1]["implement"])
        self.assertEqual(s2["status"], "blocked")
        self.assertIn("changed your clone", s2["last_error"])

    def test_round2_open_verifications_keep_the_program_running(self):     # Codex F.10b R2 #2
        out = self.new()
        with S.Store() as st:
            for sl in out["slices"]:
                st.cancel(sl["implement"])
            st.cancel(out["program"])
        with self.assertRaises(ValueError):
            self.new()                                             # verify / publish tasks still open

    def test_plan_is_created_atomically(self):                               # #8
        from agent_skill_toolkit.core import store as store_mod
        real = store_mod.Store._add_locked
        n = {"i": 0}

        def flaky(self_, *a, **k):
            n["i"] += 1
            if n["i"] == 3:
                raise RuntimeError("crash mid-plan")
            return real(self_, *a, **k)
        with mock.patch.object(store_mod.Store, "_add_locked", flaky):
            with self.assertRaises(RuntimeError):
                self.new()
        with S.Store() as st:
            self.assertEqual(st.list(limit=50), [])               # nothing half-created
        self.assertFalse(self.new()["duplicate"])


class F10dTest(ProgramTest):
    """Mechanical slices, re-plan, ratchet trend (checklist F.10d)."""

    def run_all(self, pid):
        self.work()
        self.approve(pid)
        self.work()

    def test_mechanical_slice_runs_its_tool_not_an_agent(self):
        tool = os.path.join(self.tmp, "codemod.py")
        with open(tool, "w") as f:
            f.write("import pathlib\np = pathlib.Path('src/app.py')\np.write_text(p.read_text().replace('x = 1', 'value = 1'))\n")
        with open(self.slices, "w") as f:
            json.dump([{"id": "rename", "title": "Đổi tên biến", "run": [sys.executable, tool]}], f)
        out = self.new()
        self.run_all(out["program"])
        self.assertEqual(self.prompts, [])                             # no agent turn at all
        with S.Store() as st:
            self.assertEqual({k["status"] for k in st.children(out["program"])}, {"done"})
        self.assertIn("value = 1", git(self.repo, "show", f"{out['slices'][0]['branch']}:src/app.py"))

    def test_mechanical_slice_without_effect_blocks(self):
        with open(self.slices, "w") as f:
            json.dump([{"id": "noop", "title": "x", "run": [sys.executable, "-c", "pass"]}], f)
        out = self.new()
        self.run_all(out["program"])
        with S.Store() as st:
            t = st.get(out["slices"][0]["implement"])
        self.assertEqual(t["status"], "blocked")
        self.assertIn("tool made no change", t["last_error"])

    def test_bad_run_is_refused(self):
        with self.assertRaises(ValueError):
            P.parse_slices([{"id": "a", "title": "x", "run": "rm -rf /"}])

    def test_replan_after_a_wave(self):
        out = self.new()
        self.run_all(out["program"])                                  # s1, s2 done
        more = os.path.join(self.tmp, "more.json")
        with open(more, "w") as f:
            json.dump([{"id": "s3", "title": "Dọn dẹp", "depends_on": ["s2"]}], f)
        with S.Store() as st:
            with self.assertRaises(ValueError):                        # ids never collide
                P.extend(st, out["program"], [{"id": "s1", "title": "again"}])
            ext = P.extend(st, out["program"], json.load(open(more)))
        self.work()
        with S.Store() as st:
            gate = st.get(ext["gate"])
            self.assertEqual(gate["status"], "blocked")                # waits for YOUR approval
            self.assertEqual(gate["parent"], out["program"])
        self.approve(ext["gate"])
        self.work()
        b2, b3 = out["slices"][1]["branch"], ext["slices"][0]["branch"]
        git(self.repo, "merge-base", "--is-ancestor", b2, b3)         # stacked on s2's published commit
        rep = RF.program("status", type("A", (), {"program_id": out["program"]})())
        self.assertEqual(rep["progress"], "10/10")                    # 2x3 + gate + 3
        self.assertEqual([r["slice"] for r in rep["slices"]], ["s1", "s2", "s3"])
        self.assertEqual(len(rep["ratchet_trend"]), 3)                # the ratchet rung measured every slice


if __name__ == "__main__":
    unittest.main()
