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
    elif mode == "weaken":                    # make it green by gutting the assertion
        t = os.path.join(repo, "tests", "test_calc.py")
        open(t, "w").write(open(t).read().replace("self.assertEqual(calc.add(2, 3), 5)", "pass"))
    elif mode == "rename_test":
        os.replace(os.path.join(repo, "tests", "test_calc.py"), os.path.join(repo, "tests", "skip_calc.txt"))
    elif mode == "switch_branch":             # sneaky: commit on main, come back, commit the fix here too
        subprocess.run(["git", "-C", repo, "checkout", "-q", "main"])
        open(os.path.join(repo, "src", "extra.py"), "w").write("X = 1\\n")
        subprocess.run(["git", "-C", repo, "add", "-A"])
        subprocess.run(["git", "-C", repo, "commit", "-qm", "sneaky on main"], capture_output=True)
        subprocess.run(["git", "-C", repo, "checkout", "-q", "feat/x"])
        open(calc, "w").write("def add(a, b):\\n    return a + b\\n")
    elif mode == "weaken_then_quota":         # commits a weakened test, then dies on a provider limit
        t = os.path.join(repo, "tests", "test_calc.py")
        open(t, "w").write(open(t).read().replace("self.assertEqual(calc.add(2, 3), 5)", "pass"))
        subprocess.run(["git", "-C", repo, "commit", "-qam", "wip"], capture_output=True)
        print(json.dumps({"error": True, "kind": "quota", "resume_at": %(future)s, "message": "usage limit reached"}))
        sys.exit(1)
    elif mode == "die_before_change":
        sys.exit(1)
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
          protects: []
        suite:
          run:
            - {PY}
            - -m
            - unittest
            - discover
            - -s
            - tests
            - -q
          protects: []
          full_suite: true
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

    def test_fixer_touching_tests_is_stopped(self):
        """Codex F.2 #3: deletion, gutted assertions and renames out of discovery all stop for a human."""
        for mode in ("delete_test", "weaken", "rename_test"):
            with self.subTest(mode=mode):
                self.setUp()
                self.break_code()
                os.environ["FAKE_MODE"] = mode
                self.assertEqual(self.start_and_work()[-1]["status"], "blocked")
                self.assertIn("changes tests", self.task()["last_error"])
                mock.patch.stopall()

    def test_branch_switch_after_start_never_commits_elsewhere(self):
        """Codex F.2 #1."""
        self.break_code()
        V.start(self.repo, "main", "", "implement", False)
        git(self.repo, "checkout", "-q", "main")
        ran = V.work("w", once=True, interval=0)["ran"]
        self.assertEqual(ran[-1]["status"], "blocked")
        self.assertIn("started for `feat/x`", self.task()["last_error"])
        self.assertEqual(git(self.repo, "log", "-1", "--format=%s", "main"), "base")   # main untouched

    def test_fixer_committing_on_another_branch_is_caught(self):
        """Codex F.2 R2: the fixer committed on `main` and came back — detected, restore command given."""
        self.break_code()
        main_before = git(self.repo, "rev-parse", "main")
        os.environ["FAKE_MODE"] = "switch_branch"
        self.assertEqual(self.start_and_work()[-1]["status"], "blocked")
        err = self.task()["last_error"]
        self.assertIn("refs/heads/main", err)
        self.assertIn(main_before, err)                                   # how to restore it

    def test_weakened_test_committed_before_a_quota_stop_is_still_caught(self):
        """Codex F.2 R2 NEW HIGH: the pending fix is validated on resume."""
        self.break_code()
        os.environ["FAKE_MODE"] = "weaken_then_quota"
        self.assertEqual(self.start_and_work()[-1]["status"], "paused_quota")
        self.assertTrue(self.task()["data"]["pending_fix"])
        with mock.patch("time.time", return_value=time.time() + 3 * 3600):
            ran = V.work("w", once=True, interval=0)["ran"]
        self.assertEqual(ran[-1]["status"], "blocked")
        self.assertIn("changes tests", self.task()["last_error"])

    def test_fixer_dying_before_any_change_just_retries(self):
        self.break_code()
        os.environ["FAKE_MODE"] = "die_before_change"
        ran = self.start_and_work()
        self.assertNotIn("made no change", self.task().get("last_error") or "")
        self.assertIn(ran[-1]["status"], ("queued", "failed"))           # counted as agent failures, not stuck

    def test_protection_covers_configs_launchers_and_odd_names(self):
        """Codex F.2 R2 #3."""
        for path in ("vitest.config.ts", "jest.config.cjs", "Makefile", "mvnw.cmd", "tests/test_é.py",
                     "gradle/wrapper/gradle-wrapper.properties", "checks/run.cmd"):
            with self.subTest(path=path):
                before = git(self.repo, "rev-parse", "HEAD")
                full = os.path.join(self.repo, path)
                os.makedirs(os.path.dirname(full) or self.repo, exist_ok=True)
                open(full, "w", encoding="utf-8").write("x\n")
                git(self.repo, "add", "-A")
                git(self.repo, "commit", "-qm", f"touch {path}")
                extra = L.spec_launchers({"rungs": [{"run": ["checks/run.cmd"], "protects": []}]})
                self.assertTrue(L.protected_changes(self.repo, before, "HEAD", extra), path)
        before = git(self.repo, "rev-parse", "HEAD")
        open(os.path.join(self.repo, "src", "calc.py"), "a").write("# prod change\n")
        git(self.repo, "commit", "-qam", "prod")
        self.assertEqual(L.protected_changes(self.repo, before, "HEAD"), [])   # production code is fine

    def test_interpreter_rungs_must_declare_what_they_execute(self):
        """Codex F.2 R3-R5: guessing the executed script from argv can always be fooled, so an
        interpreter / package-runner rung must DECLARE `protects`; argv paths only add to it."""
        import re as _re

        def hits(pats, path):
            return any(_re.search(p, path) for p in pats)
        for argv in (["python", "checks/run.py", "-m"], ["python", "ci/check", "data.py"], ["node", "."],
                     ["npx", "jest"], ["uv", "run", "pytest"], ["bash", "-e", "ci/check.sh"]):
            with self.assertRaises(ValueError, msg=argv):                       # undeclared -> rejected
                L.spec_launchers({"rungs": [{"name": "r", "run": argv}]})
        pats = L.spec_launchers({"rungs": [{"name": "r", "run": ["python", "ci/check", "data.py"],
                                            "protects": ["ci/check", "ci/lib/"]}]})
        self.assertTrue(hits(pats, "ci/check"))                                 # the declared executable
        self.assertTrue(hits(pats, "ci/lib/util.py"))                         # a declared directory
        self.assertFalse(hits(pats, "data.py"))                                 # args are NOT guessed
        pats = L.spec_launchers({"rungs": [{"name": "r", "run": ["python", "x.py"],
                                            "protects": ["ci/../tools/check.py", "./ci/x.sh"]}]})
        self.assertTrue(hits(pats, "tools/check.py"))                           # normalized like git reports it
        self.assertTrue(hits(pats, "ci/x.sh"))
        self.assertEqual(L.spec_launchers({"rungs": [{"name": "r", "run": ["python", "-m", "unittest"],
                                                      "protects": []}]}),
                         ["(^|/)python$"])          # an explicit "nothing" + the bare launcher (harmless if absent)
        pats = L.spec_launchers({"rungs": [{"name": "c", "run": ["mvnw.cmd", "-q", "package"], "protects": []}]})
        self.assertTrue(hits(pats, "mvnw.cmd"))                                 # the repo launcher
        with self.assertRaises(ValueError):
            L.spec_launchers({"rungs": [{"name": "r", "run": ["python", "x.py"], "protects": "x.py"}]})
        # Codex F.2 R6: no list of interpreter names to sidestep — EVERY command rung declares
        for argv in (["python3.13t", "checks/run.py"], ["verify"], ["mvnw.cmd", "test"], ["./gradlew", "check"]):
            with self.assertRaises(ValueError, msg=argv):
                L.spec_launchers({"rungs": [{"name": "r", "run": argv}]})
        pats = L.spec_launchers({"rungs": [{"name": "r", "run": ["verify"], "protects": []}]})
        self.assertTrue(hits(pats, "verify"))                                   # bare launcher resolved in repo
        for root in ("./", ".", "ci/..", "../outside"):
            with self.assertRaises(ValueError, msg=root):
                L.spec_launchers({"rungs": [{"name": "r", "run": ["x"], "protects": [root]}]})
        self.assertEqual(L.spec_launchers({"rungs": [{"name": "rv", "kind": "cross_review"}]}), [])

    def test_protection_fails_closed_on_git_errors(self):
        """Codex F.2 R3 NEW HIGH."""
        from agent_skill_toolkit.workflows import engine as E
        with self.assertRaises(E.NeedsHuman):
            L.protected_changes(self.repo, "deadbeef" * 5, "HEAD")

    def test_covered_by_requires_a_declared_full_suite(self):
        """Codex F.2 R3 MED: `compile -DskipTests` must not count as coverage."""
        base = ("verify:\n  ladder:\n    t:\n      kind: affected_tests\n      covered_by: {cov}\n      run: x\n"
                "      protects: []\n    compile:\n      run: mvn -DskipTests package\n      protects: []\n"
                "    suite:\n      run: mvn test\n      protects: []\n{flag}")
        for cov, flag, ok in (("compile", "", False), ("suite", "", False), ("suite", "      full_suite: true\n", True)):
            f = os.path.join(self.tmp, f"c_{cov}_{ok}.yml")
            open(f, "w").write(base.format(cov=cov, flag=flag))
            if ok:
                L.load_spec(self.repo, "main", f)
            else:
                with self.assertRaises(ValueError, msg=(cov, flag)):
                    L.load_spec(self.repo, "main", f)

    def test_finding_schema_and_covered_by_must_be_a_cmd_rung(self):
        self.assertEqual(L.parse_verdict('{"verdict":"GO","findings":["HIGH: broken authentication"]}'), {})
        self.assertEqual(L.parse_verdict('{"verdict":"GO","findings":[{"severity":"BAD"}]}'), {})
        bad = os.path.join(self.tmp, "cov.yml")
        open(bad, "w").write("verify:\n  ladder:\n    t:\n      kind: affected_tests\n      covered_by: review\n"
                             "      run: x\n    review:\n      kind: cross_review\n")
        with self.assertRaises(ValueError):
            L.load_spec(self.repo, "main", bad)

    def test_custom_and_release_default_branches_refused(self):
        git(self.repo, "config", "init.defaultBranch", "feat/x")
        with self.assertRaises(PermissionError):
            V.start(self.repo, "main", "", "implement", False)
        git(self.repo, "config", "--unset", "init.defaultBranch")
        git(self.repo, "checkout", "-q", "-b", "release/2.1")
        with self.assertRaises(PermissionError):
            V.start(self.repo, "main", "", "implement", False)

    def test_dirty_worktree_is_never_committed(self):
        """Codex F.2 #2: user edits (tracked, staged, untracked) are not swept into a fix commit."""
        self.break_code()
        open(os.path.join(self.repo, "notes.txt"), "w").write("my private notes")
        with self.assertRaises(PermissionError):
            V.start(self.repo, "main", "", "implement", False)
        os.remove(os.path.join(self.repo, "notes.txt"))
        V.start(self.repo, "main", "", "implement", False)
        open(os.path.join(self.repo, "README.md"), "w").write("wip")   # user starts editing meanwhile
        git(self.repo, "add", "README.md")
        ran = V.work("w", once=True, interval=0)["ran"]
        self.assertEqual(ran[-1]["status"], "blocked")
        self.assertIn("not clean", self.task()["last_error"])
        self.assertNotIn("README.md", git(self.repo, "show", "--name-only", "--format=", "HEAD"))

    def test_bad_base_fails_fast(self):
        """Codex F.2 #4: a missing base is an error, never an 'empty diff' that passes review."""
        with self.assertRaises((ValueError, RuntimeError)):
            V.start(self.repo, "no-such-branch", "", "implement", False)

    def test_verdict_parser_ignores_nested_verdicts_and_bad_schema(self):
        """Codex F.2 #6."""
        nested = json.dumps({"verdict": "NO-GO", "findings": [{"severity": "HIGH", "verdict": "GO", "title": "x"}]})
        self.assertEqual(L.parse_verdict("noise " + nested)["verdict"], "NO-GO")
        self.assertEqual(L.parse_verdict('{"verdict": "GO"}'), {})                 # findings missing
        self.assertEqual(L.parse_verdict('{"verdict": "maybe", "findings": []}'), {})
        self.assertEqual(L.parse_verdict('{"verdict":"NO-GO","findings":[]} {"verdict":"GO","findings":[]}')["verdict"], "GO")

    def test_large_diff_is_split_and_oversized_file_refused(self):
        """Codex F.2 #5."""
        d = "".join(f"diff --git a/f{i}.py b/f{i}.py\n" + "+x\n" * 50 for i in range(10))
        units = L.split_diff(d, 400)
        self.assertGreater(len(units), 1)
        self.assertEqual("".join(units), d)                                       # nothing dropped
        from agent_skill_toolkit.workflows import engine as E
        with self.assertRaises(E.NeedsHuman):
            L.split_diff("diff --git a/big.py b/big.py\n" + "+y\n" * 1000, 400)

    def test_affected_selection_failure_needs_cover_or_human(self):
        """Codex F.2 #7."""
        from agent_skill_toolkit.workflows import engine as E
        job = {"repo": self.repo, "base": "main"}
        self.break_code()
        with mock.patch.object(L, "_affected", return_value=None):
            res = L.rung_affected(None, {"name": "t", "covered_by": "suite", "run": ["x"]}, job)
            self.assertIn("covers it", res["skipped"])
            with self.assertRaises(E.NeedsHuman):
                L.rung_affected(None, {"name": "t", "run": ["x"]}, job)
        with self.assertRaises(ValueError):                                      # covered_by must be LATER
            open(os.path.join(self.tmp, "bad.yml"), "w").write(
                "verify:\n  ladder:\n    suite:\n      run: x\n    t:\n      kind: affected_tests\n"
                "      covered_by: suite\n      run: y\n")
            L.load_spec(self.repo, "main", os.path.join(self.tmp, "bad.yml"))

    @unittest.skipUnless(os.name == "nt", "Windows launcher resolution")
    def test_relative_launcher_resolves_in_the_repo(self):
        """Codex F.2 #8: `checks/ok.cmd` is the repo's, whatever the worker's cwd."""
        os.makedirs(os.path.join(self.repo, "checks"))
        open(os.path.join(self.repo, "checks", "ok.cmd"), "w").write("@exit /b 0\r\n")
        resolved = L.in_repo(self.repo, ["checks/ok.cmd", "--x"])
        self.assertEqual(resolved[0], os.path.normpath(os.path.join(self.repo, "checks", "ok.cmd")))
        out = subprocess.run(L.winexec.command(resolved), cwd=self.repo, capture_output=True)
        self.assertEqual(out.returncode, 0)

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
