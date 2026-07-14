#!/usr/bin/env python3
"""Stdlib unittest for triage heuristics + agent path (Phase 2). No pip deps."""

import os
import sys
import unittest
from argparse import Namespace

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(
    _HERE, "..", ".claude", "skills", "auto-dev", "tools")))

import triage  # noqa: E402


def ns(**kw):
    base = dict(type=None, title=None, desc=None, desc_file=None, plan=None,
                backend=None, model=None, dry_run_text=None,
                force_tier=None, force_mode=None)
    base.update(kw)
    return Namespace(**base)


class TriageTest(unittest.TestCase):
    def test_trivial_bugfix(self):
        tier, mode, _, _ = triage.classify("bugfix", "Fix NPE", "short null check fix", 1)
        self.assertEqual(tier, "trivial")
        self.assertEqual(mode, "auto")

    def test_high_risk_forces_complex(self):
        tier, mode, reason, _ = triage.classify("bugfix", "tweak", "fix the auth token refresh", 1)
        self.assertEqual(tier, "complex")
        self.assertEqual(mode, "checkpoint")
        self.assertIn("high-risk", reason)

    def test_high_risk_vietnamese(self):
        tier, _, _, _ = triage.classify("bugfix", "sửa", "đổi luồng phân quyền người dùng", 1)
        self.assertEqual(tier, "complex")

    def test_feature_default_standard(self):
        tier, mode, _, _ = triage.classify("feature", "Add CSV export", "small export endpoint", 2)
        self.assertEqual(tier, "standard")
        self.assertEqual(mode, "checkpoint")

    def test_feature_broad_scope_complex(self):
        tier, _, _, _ = triage.classify("feature", "Big thing", "x" * 900, None)
        self.assertEqual(tier, "complex")

    def test_unknown_type_standard(self):
        tier, _, _, _ = triage.classify(None, "whatever", "desc", None)
        self.assertEqual(tier, "standard")

    def test_agent_path_dry_run(self):
        out = triage.cmd_classify(ns(
            type="bugfix", desc="something", backend="dry-run",
            dry_run_text="<triage><tier>complex</tier><mode>checkpoint</mode>"
                         "<reason>looks risky</reason></triage>"))
        self.assertEqual(out["tier"], "complex")
        self.assertEqual(out["source"], "agent")
        self.assertEqual(out["reason"], "looks risky")

    def test_agent_bad_output_falls_back(self):
        out = triage.cmd_classify(ns(
            type="bugfix", desc="short fix", backend="dry-run",
            dry_run_text="garbage no tags"))
        self.assertEqual(out["source"], "heuristic")
        self.assertIn("note", out)

    def test_force_overrides(self):
        out = triage.cmd_classify(ns(type="bugfix", desc="short",
                                     force_tier="complex", force_mode="checkpoint"))
        self.assertEqual(out["tier"], "complex")
        self.assertEqual(out["mode"], "checkpoint")

    def test_skip_debate_flag(self):
        out = triage.cmd_classify(ns(type="bugfix", desc="tiny"))
        self.assertEqual(out["skip_debate"], out["tier"] == "trivial")


if __name__ == "__main__":
    unittest.main(verbosity=2)
