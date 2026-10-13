#!/usr/bin/env python3
"""Verify ladder (checklist F.2) on a throw-away git repo with REAL subprocess rungs and fake
fixer / reviewer agents (separate processes, prompt on stdin — the real ctx.run path).
    python -m unittest discover -s tests
"""

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest import mock

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(_ROOT, ".claude", "skills", "auto-dev", "tools"))
import verify as V  # noqa: E402  (bootstraps the runtime)
from agent_skill_toolkit.core import store as S  # noqa: E402
from agent_skill_toolkit.workflows.verify import ladder as L  # noqa: E402

PY = sys.executable.replace("\\", "/")

FAKE_AGENT = textwrap.dedent('''
    import json, os, re, subprocess, sys
    prompt = sys.stdin.read()
    mode = os.environ.get("FAKE_MODE", "fix")
    with open(os.environ["FAKE_LOG"], "a") as f:
        f.write(("review" if "strict code reviewer" in prompt else "fix") + "\\n")
    if "strict code reviewer" in prompt:
        n = open(os.environ["FAKE_LOG"]).read().count("review")
        go = os.environ.get("FAKE_REVIEW", "go") == "go" or (os.environ.get("FAKE_REVIEW") == "nogo-once" and n > 1)
        print("thinking... {not json}")
        print(json.dumps({"verdict": "GO" if go else "NO-GO", "findings": [] if go else
              [{"severity": "HIGH", "file": "src/calc.py", "line": 2, "title": "bad", "detail": "x"}]}))
        sys.exit(0)
    if mode == "quota":
        print(json.dumps({"error": True, "kind": "quota", "resume_at": %(future)s, "message": "usage limit reached"}))
        sys.exit(1)
    repo = re.search(r"repo at (.+)\\.", prompt).group(1).strip()
    calc = os.path.join(repo, "src", "calc.py")
    if mode == "fix":
        open(calc, "w").write("def add(a, b):\\n    return a + b\\n")
    elif mode == "noop":
        pass
    elif mode == "useless":
        open(calc, "a").write("# tried\\n")
    elif mode == "delete_test":
        os.remove(os.path.join(repo, "tests", "test_calc.py"))
    if mode != "nocommit":
        subprocess.run(["git", "-C", repo, "add", "-A"])
        subprocess.run(["git", "-C", repo, "commit", "-qm", "wip(verify): fix"], capture_output=True)
    else:
        open(calc, "w").write("def add(a, b):\\n    return a + b\\n")
    print("done")
''')

SPEC = textwrap.dedent(f'''
    verify:
      base: main
      ladder:
        compile:
          run:
            - {PY}
            - -m
            - py_compile
            - src/calc.py
        suite:
          run:
            - {PY}
            - -m
            - unittest
            - discover
            - -s
            - tests
            - -q
      budgets:
        max_fix_rounds_per_rung: 2
        max_fix_rounds_total: 4
''')


def git(repo, *a):
    return subprocess.run(["git", "-C", repo, *a], capture_output=True, text=True, check=True).stdout.strip()


class LadderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ladder-")
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(os.path.join(self.repo, "src"))
        os.makedirs(os.path.join(self.repo, "tests"))
        os.makedirs(os.path.join(self.repo, ".agent"))
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "t@t")
        git(self.repo, "config", "user.name", "t")
        open(os.path.join(self.repo, "src", "calc.py"), "w").write("def add(a, b):\n    return a + b\n")
        open(os.path.join(self.repo, "tests", "test_calc.py"), "w").write(
            "import sys, os, unittest\nsys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))\n"
            "import calc\nclass T(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(calc.add(2, 3), 5)\n")
        open(os.path.join(self.repo, ".agent", "project.yml"), "w").write(SPEC)
        open(os.path.join(self.repo, ".gitignore"), "w").write("__pycache__/\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "base")
        git(self.repo, "checkout", "-q", "-b", "feat/x")
        self.agent = os.path.join(self.tmp, "fake_agent.py")
        open(self.agent, "w").write(FAKE_AGENT % {"future": time.time() + 7200})
        self.log = os.path.join(self.tmp, "agent.log")
        mock.patch.dict(os.environ, {"TOOLKIT_STATE_DIR": self.tmp, "TOOLKIT_STORE": os.path.join(self.tmp, "t.db"),
                                     "FAKE_LOG": self.log, "FAKE_MODE": "fix", "FAKE_REVIEW": "go"}).start()
        mock.patch.object(L, "AGENT_RUNNER", self.agent).start()

    def tearDown(self):
        mock.patch.stopall()

    def break_code(self):
        open(os.path.join(self.repo, "src", "calc.py"), "w").write("def add(a, b):\n    return a - b\n")
        git(self.repo, "commit", "-qam", "feature (buggy)")

    def start_and_work(self):
        V.start(self.repo, "main", "", "implement", False)
        return V.work("w", once=True, interval=0)["ran"]

    def task(self):
        with S.Store() as st:
            return st.list(kind=L.KIND)[0]

    def calls(self, what):
        return open(self.log).read().split().count(what) if os.path.exists(self.log) else 0

    # ------------------------------------------------------------------ tests
    def test_green_branch_passes_without_fixer(self):
        open(os.path.join(self.repo, "src", "calc.py"), "a").write("# docs\n")
        git(self.repo, "commit", "-qam", "docs")
        ran = self.start_and_work()
        self.assertEqual(ran[-1]["status"], "done")
        self.assertEqual(self.calls("fix"), 0)

    def test_red_test_is_fixed_then_everything_reruns_on_the_new_head(self):
        self.break_code()
        ran = self.start_and_work()
        self.assertEqual([r["status"] for r in ran], ["queued", "done"])
        self.assertEqual(self.calls("fix"), 1)
        self.assertEqual(self.task()["data"]["fix_rounds"], {"suite": 1})
        with S.Store() as st:
            compiles = [c for c in st.checkpoints(self.task()["id"]) if c["step"] == "compile"]
        self.assertEqual(len(compiles), 2)                      # compile re-ran on the fixed HEAD
        self.assertIn("wip(verify)", git(self.repo, "log", "-1", "--format=%s"))

    def test_uncommitted_fix_is_committed_by_the_ladder(self):
        self.break_code()
        os.environ["FAKE_MODE"] = "nocommit"
        self.assertEqual(self.start_and_work()[-1]["status"], "done")
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")

    def test_fixer_that_changes_nothing_blocks(self):
        self.break_code()
        os.environ["FAKE_MODE"] = "noop"
        self.assertEqual(self.start_and_work()[-1]["status"], "blocked")
        self.assertIn("made no change", self.task()["last_error"])

    def test_budget_exhausted_blocks_with_the_failure_summary(self):
        self.break_code()
        os.environ["FAKE_MODE"] = "useless"
        ran = self.start_and_work()
        self.assertEqual(ran[-1]["status"], "blocked")
        self.assertEqual(self.calls("fix"), 2)                  # max_fix_rounds_per_rung: 2
        self.assertIn("AssertionError", self.task()["last_error"])

    def test_fixer_deleting_tests_is_stopped(self):
        self.break_code()
        os.environ["FAKE_MODE"] = "delete_test"
        self.assertEqual(self.start_and_work()[-1]["status"], "blocked")
        self.assertIn("deleted tests", self.task()["last_error"])

    def test_default_branch_is_refused(self):
        git(self.repo, "checkout", "-q", "main")
        with self.assertRaises(PermissionError):
            V.start(self.repo, "main", "", "implement", False)

    def test_spec_is_read_from_the_base_ref_not_the_branch(self):
        evil = SPEC.replace("py_compile", "this_would_run_attacker_code")
        open(os.path.join(self.repo, ".agent", "project.yml"), "w").write(evil)
        git(self.repo, "commit", "-qam", "change the checks")
        spec = L.load_spec(self.repo, "main")
        self.assertIn("py_compile", json.dumps(spec["rungs"]))
        self.assertNotIn("attacker", json.dumps(spec["rungs"]))

    def test_cross_review_nogo_then_fixed_then_go(self):
        spec = SPEC.replace("  budgets:", "    review:\n      kind: cross_review\n      role: review_cross\n  budgets:")
        self.assertIn("cross_review", json.dumps(L.yamlsub.load(spec)))
        spec_file = os.path.join(self.tmp, "spec.yml")
        open(spec_file, "w").write(spec)
        open(os.path.join(self.repo, "src", "calc.py"), "a").write("# change\n")
        git(self.repo, "commit", "-qam", "change")
        os.environ["FAKE_REVIEW"] = "nogo-once"
        os.environ["FAKE_MODE"] = "useless"                    # any change counts as an attempt to fix
        V.start(self.repo, "main", spec_file, "implement", False)
        ran = V.work("w", once=True, interval=0)["ran"]
        self.assertEqual(ran[-1]["status"], "done")
        self.assertEqual((self.calls("review"), self.calls("fix")), (2, 1))

    def test_fixer_quota_pauses_without_attempt(self):
        self.break_code()
        os.environ["FAKE_MODE"] = "quota"
        self.assertEqual(self.start_and_work()[-1]["status"], "paused_quota")
        self.assertEqual(self.task()["attempts"], 0)

    def test_affected_tests_mapping_and_unavailable_codegraph(self):
        self.assertEqual(L.format_tests(["tests/test_a.py", "README.md"], "unittest"), ["tests.test_a"])
        self.assertEqual(L.format_tests(["src/test/java/x/FooTest.java"], "maven"), ["FooTest"])
        self.assertEqual(L._expand(["mvn", "-Dtest={tests_csv}", "{tests}"], ["A", "B"]),
                         ["mvn", "-Dtest=A,B", "A", "B"])
        with mock.patch("shutil.which", return_value=None):
            self.assertIsNone(L._affected(self.repo, ["src/calc.py"]))

    def test_summary_keeps_errors_and_tail_only(self):
        log = "\n".join(f"line {i}" for i in range(5000)) + "\nAssertionError: 1 != 5\nFAILED (failures=1)"
        s = L.summarize(log)
        self.assertIn("AssertionError", s)
        self.assertLess(len(s), 7000)


if __name__ == "__main__":
    unittest.main()
