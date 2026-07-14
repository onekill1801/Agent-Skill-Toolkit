#!/usr/bin/env python3
"""Stdlib unittest for the isolation false-pass guard (Phase 6). No pip deps."""

import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(
    _HERE, "..", ".claude", "skills", "dev-automation", "tools")))
sys.path.insert(0, os.path.abspath(os.path.join(
    _HERE, "..", ".claude", "skills", "fork-terminal", "tools")))

import probe_db  # noqa: E402
import runtime_isolator as ri  # noqa: E402


class IsolationGuardTest(unittest.TestCase):
    def test_isolated_name_idempotent(self):
        self.assertEqual(ri.isolated_db_name("atask", 123), "atask_task_123")
        self.assertEqual(ri.isolated_db_name("atask_task_123", 123), "atask_task_123")

    def test_verdict_match_passes(self):
        v = probe_db._check_db_verdict("atask_task_9", "atask_task_9", "postgres", "psql ...")
        self.assertTrue(v["passed"])
        self.assertNotIn("error", v)

    def test_verdict_mismatch_is_error_not_pass(self):
        # connected to the shared DB instead of the isolated one -> false-pass averted
        v = probe_db._check_db_verdict("atask", "atask_task_9", "postgres", "psql ...")
        self.assertFalse(v["passed"])
        self.assertTrue(v["error"])
        self.assertIn("isolation not applied", v["message"])

    def test_verdict_no_expectation_passes(self):
        v = probe_db._check_db_verdict("anything", None, "mysql", "mysql ...")
        self.assertTrue(v["passed"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
