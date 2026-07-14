#!/usr/bin/env python3
"""Stdlib unittest for grounding + review_gate (Phases 3b, 5). No pip deps."""

import os
import sys
import tempfile
import unittest
from argparse import Namespace

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(
    _HERE, "..", ".claude", "skills", "auto-dev", "tools")))

import grounding  # noqa: E402
import review_gate  # noqa: E402


class GroundingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ground-")
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(os.path.join(self.repo, "src"))
        with open(os.path.join(self.repo, "pom.xml"), "w") as f:
            f.write("<project/>")
        with open(os.path.join(self.repo, "src", "A.java"), "w") as f:
            f.write("package x;\nclass A {}\n")
        with open(os.path.join(self.repo, "src", "B.java"), "w") as f:
            f.write("class B {}\n")
        # Redirect artifact writes to the throwaway dir.
        self._orig = grounding._artifact_path
        grounding._artifact_path = lambda rid: os.path.join(self.tmp, f"{rid}_g.md")

    def tearDown(self):
        grounding._artifact_path = self._orig

    def _plan(self, *files):
        p = os.path.join(self.tmp, "plan.xml")
        items = "".join(f"<file>{f}</file>" for f in files)
        with open(p, "w") as f:
            f.write(f"<final_specification><target_files>{items}</target_files></final_specification>")
        return p

    def ns(self, plan, **kw):
        base = dict(run="r1", root=self.repo, plan=plan, backend=None, model=None,
                    dry_run_text=None)
        base.update(kw)
        return Namespace(**base)

    def test_grounds_existing_file(self):
        out = grounding.cmd_run(self.ns(self._plan("src/A.java")))
        self.assertEqual(out["verdict"], "pass")
        self.assertEqual(out["found"], ["src/A.java"])
        self.assertEqual(out["stack"], "maven/java")
        self.assertTrue(os.path.isfile(out["artifact"]))

    def test_missing_file_listed(self):
        out = grounding.cmd_run(self.ns(self._plan("src/A.java", "src/Nope.java")))
        self.assertIn("src/Nope.java", out["missing"])
        self.assertEqual(out["verdict"], "pass")  # at least one found

    def test_no_targets_fails(self):
        p = os.path.join(self.tmp, "empty.xml")
        with open(p, "w") as f:
            f.write("<final_specification></final_specification>")
        out = grounding.cmd_run(self.ns(p))
        self.assertEqual(out["verdict"], "fail")

    def test_agent_note_appended(self):
        out = grounding.cmd_run(self.ns(self._plan("src/A.java"),
                                        backend="dry-run", dry_run_text="follow package x"))
        self.assertTrue(out["agent_note"])
        with open(out["artifact"], encoding="utf-8") as f:
            self.assertIn("follow package x", f.read())


class ReviewGateTest(unittest.TestCase):
    def ns(self, **kw):
        base = dict(root=".", base=None, branch=None, diff_file=None,
                    backend="dry-run", model=None, dry_run_text=None)
        base.update(kw)
        return Namespace(**base)

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="review-")
        self.diff = os.path.join(self.tmp, "d.diff")
        with open(self.diff, "w") as f:
            f.write("--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new\n")

    def test_pass_clean(self):
        out = review_gate.cmd_run(self.ns(
            diff_file=self.diff,
            dry_run_text="<review><verdict>pass</verdict><blockers></blockers>"
                         "<warnings><item>minor naming</item></warnings>"
                         "<summary>ok</summary></review>"))
        self.assertTrue(out["passed"])
        self.assertEqual(out["warnings"], ["minor naming"])

    def test_blocker_forces_fail_even_if_verdict_pass(self):
        out = review_gate.cmd_run(self.ns(
            diff_file=self.diff,
            dry_run_text="<review><verdict>pass</verdict>"
                         "<blockers><item>SQL injection in query</item></blockers>"
                         "<warnings></warnings><summary>risky</summary></review>"))
        self.assertFalse(out["passed"])
        self.assertEqual(out["verdict"], "fail")
        self.assertIn("SQL injection in query", out["blockers"])

    def test_unknown_verdict_fails_safe(self):
        out = review_gate.cmd_run(self.ns(diff_file=self.diff, dry_run_text="garbage"))
        self.assertFalse(out["passed"])

    def test_empty_diff_errors(self):
        empty = os.path.join(self.tmp, "empty.diff")
        with open(empty, "w") as f:
            f.write("   \n")
        out = review_gate.cmd_run(self.ns(diff_file=empty, dry_run_text="<review/>"))
        self.assertTrue(out.get("error"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
