#!/usr/bin/env python3
"""Stdlib unittest cho bước Clarify (làm rõ yêu cầu ở Intake) + gate `clarity` trên stage plan.

Phủ hai phần:
  1. clarify.py — phát hiện mơ hồ type-aware (bugfix rõ -> không chặn; feature mơ hồ -> chặn),
     đường agent (parse <clarify>), và brief gấp câu trả lời.
  2. run_log — gate `clarity` là required trên stage 'plan': auto mode chặn khi thiế/fail;
     checkpoint mode chỉ surface.
No pip deps.
"""

import json
import os
import sys
import tempfile
import unittest
from argparse import Namespace

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(
    _HERE, "..", ".claude", "skills", "auto-dev", "tools")))
sys.path.insert(0, os.path.abspath(os.path.join(
    _HERE, "..", ".claude", "skills", "dev-automation", "tools")))

import clarify  # noqa: E402
import run_log  # noqa: E402


def ns(**kw):
    return Namespace(**kw)


class ClarifyDetectTest(unittest.TestCase):
    def _analyze(self, **kw):
        base = dict(type=None, title=None, desc=None, desc_file=None,
                    backend=None, model=None, dry_run_text=None)
        base.update(kw)
        return clarify.cmd_analyze(ns(**base))

    def test_clear_bugfix_passes(self):
        out = self._analyze(
            type="bugfix", title="Fix NPE",
            desc="Fix NullPointerException in UserService.getProfile when avatar field is null, "
                 "return empty string instead of throwing")
        self.assertEqual(out["verdict"], "pass")
        self.assertEqual(out["blocking_count"], 0)

    def test_empty_bugfix_blocks_on_scope(self):
        out = self._analyze(type="bugfix", desc="sửa lỗi login")
        self.assertEqual(out["verdict"], "needs_clarification")
        cats = {q["category"]: q["blocking"] for q in out["questions"]}
        self.assertTrue(cats.get("scope"))            # quá ngắn -> scope blocking

    def test_vague_feature_blocks_multiple(self):
        out = self._analyze(
            type="feature", title="Báo cáo",
            desc="Làm API export báo cáo cho nhanh hơn, tối ưu hiệu năng, v.v.")
        self.assertEqual(out["verdict"], "needs_clarification")
        blocking = {q["category"] for q in out["questions"] if q["blocking"]}
        self.assertIn("scope", blocking)
        self.assertIn("io", blocking)                 # feature + IO marker -> blocking
        self.assertIn("acceptance", blocking)         # feature, no done marker -> blocking

    def test_feature_io_blocking_but_bugfix_not(self):
        feat = self._analyze(type="feature", desc="Add endpoint that returns a report payload now")
        bug = self._analyze(type="bugfix",
                            desc="Endpoint report payload returns wrong total when order is refunded; "
                                 "correct the sum so it excludes refunded lines properly")
        feat_io = next(q for q in feat["questions"] if q["category"] == "io")
        bug_io = next(q for q in bug["questions"] if q["category"] == "io")
        self.assertTrue(feat_io["blocking"])
        self.assertFalse(bug_io["blocking"])

    def test_agent_path_parses_clarify_block(self):
        raw = ("<clarify><question><category>scope</category><blocking>true</blocking>"
               "<ask>Phạm vi?</ask><why>w</why><assumption>a</assumption></question></clarify>")
        out = self._analyze(type="feature", desc="x", backend="dry-run", dry_run_text=raw)
        self.assertEqual(out["source"], "agent")
        self.assertEqual(out["verdict"], "needs_clarification")
        self.assertEqual(out["questions"][0]["ask"], "Phạm vi?")

    def test_agent_failure_falls_back_to_heuristic(self):
        # dry-run trả output không có <question> -> _detect_via_agent ném -> fallback heuristic.
        out = self._analyze(type="feature", desc="x", backend="dry-run",
                            dry_run_text="<clarify>rỗng</clarify>")
        self.assertEqual(out["source"], "heuristic")
        self.assertIn("note", out)


class ClarifyBriefTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="clarify-test-")

    def _brief(self, answers, **kw):
        ans_path = os.path.join(self.tmp, "ans.json")
        with open(ans_path, "w", encoding="utf-8") as f:
            json.dump(answers, f)
        base = dict(title="T", desc="mô tả gốc", desc_file=None,
                    answers_file=ans_path, out=os.path.join(self.tmp, "brief.md"))
        base.update(kw)
        return clarify.cmd_brief(ns(**base))

    def test_brief_splits_resolved_and_assumed(self):
        out = self._brief([
            {"ask": "Phạm vi?", "answer": "Chỉ tháng hiện tại"},
            {"ask": "Edge?", "assumption": "rỗng -> mặc định"},
        ])
        self.assertEqual(out["verdict"], "pass")
        self.assertEqual(out["resolved_count"], 1)
        self.assertEqual(out["assumed_count"], 1)
        self.assertEqual(out["acceptance_seeds"], ["Chỉ tháng hiện tại"])
        self.assertTrue(os.path.isfile(out["brief_path"]))
        with open(out["brief_path"], encoding="utf-8") as f:
            text = f.read()
        self.assertIn("Đã chốt", text)
        self.assertIn("Giả định mặc định", text)


class ClarityGateTest(unittest.TestCase):
    """Gate `clarity` phải là required trên stage 'plan' và tuân hybrid policy."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="claritygate-test-")
        self._orig = run_log._runs_dir
        run_log._runs_dir = lambda: self.tmp

    def tearDown(self):
        run_log._runs_dir = self._orig

    def init(self, mode):
        return run_log.cmd_init(ns(run_id="r1", task="1", project="p", type="bugfix",
                                   title="t", tier=None, mode=mode))

    def test_clarity_is_required_for_plan(self):
        self.assertIn("clarity", run_log.REQUIRED_GATES["plan"])

    def test_auto_mode_blocks_plan_without_clarity(self):
        self.init(mode="auto")
        res = run_log.policy(run_log._read("r1"), "plan")
        self.assertFalse(res["allowed"])
        self.assertIn("clarity:missing", res["missing"])

    def test_auto_mode_allows_plan_after_clarity_pass(self):
        self.init(mode="auto")
        run_log.cmd_record_gate(ns(run_id="r1", gate="clarity", verdict="pass",
                                   summary="0 blocking", json=None, kind=None))
        res = run_log.policy(run_log._read("r1"), "plan")
        self.assertTrue(res["allowed"])

    def test_checkpoint_mode_surfaces_but_does_not_block(self):
        self.init(mode="checkpoint")
        run_log.cmd_record_gate(ns(run_id="r1", gate="clarity", verdict="fail",
                                   summary="2 blocking", json=None, kind=None))
        res = run_log.policy(run_log._read("r1"), "plan")
        self.assertTrue(res["allowed"])                 # checkpoint không chặn
        self.assertIn("clarity:fail", res["missing"])   # nhưng vẫn surface cho người duyệt


if __name__ == "__main__":
    unittest.main()
