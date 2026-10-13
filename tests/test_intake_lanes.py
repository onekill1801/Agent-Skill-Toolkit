#!/usr/bin/env python3
"""Intake lanes + one-shot clarification bundle (checklist F.1, design §4)."""

import json
import os
import subprocess
import sys
import unittest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_TOOLS = os.path.join(_ROOT, ".claude", "skills", "auto-dev", "tools")
sys.path.insert(0, _TOOLS)
import triage  # noqa: E402,F401  (activates the runtime package)
from agent_skill_toolkit.workflows.intake import lanes as L  # noqa: E402


def cli(tool, *args):
    p = subprocess.run([sys.executable, os.path.join(_TOOLS, tool), *args], capture_output=True, text=True,
                       encoding="utf-8")
    return json.loads(p.stdout)


class KindTest(unittest.TestCase):
    def test_kinds(self):
        cases = {
            ("", "nhờ review giúp https://git/x/-/merge_requests/12"): "review_mr",
            ("", "review etask !345"): "review_mr",
            ("", "có nên nâng cấp Spring Boot lên 3.x không?"): "proposal",
            ("", "điều tra lỗi prod 500 khi export"): "investigate",
            ("", "soạn tin trả lời group tchat về deadline"): "tchat_draft",
            ("", "tạo task cho đợt kiểm thử tuần sau"): "atask_draft",
            ("feature", "Export Excel"): "code",
            ("", "fix lỗi review !12 bị NPE"): "code",          # a fix that mentions an MR is still code
            ("review", "anything"): "review_mr",
        }
        for (t, text), want in cases.items():
            self.assertEqual(L.kind_of(t, text), want, text)


class LaneTest(unittest.TestCase):
    def test_initial_lanes(self):
        self.assertEqual(L.lane_for("trivial", "code")["lane"], "nho")
        self.assertEqual(L.lane_for("standard", "code")["lane"], "vua")
        self.assertEqual(L.lane_for("complex", "code", "add payment field")["lane"], "vua")
        self.assertEqual(L.lane_for("standard", "code", "refactor toàn bộ codebase module task")["lane"], "lon")
        self.assertEqual(L.lane_for("standard", "proposal")["lane"], "de_xuat")
        self.assertIsNone(L.lane_for("standard", "review_mr")["lane"])
        for lane in L.LANES:
            self.assertIn(lane, L.PROFILES)

    def test_upgrade_only_goes_up(self):
        self.assertEqual(L.upgrade("nho", diff_lines=151)["lane"], "vua")
        self.assertEqual(L.upgrade("nho", files=6)["lane"], "vua")
        self.assertEqual(L.upgrade("nho", red_twice=True)["lane"], "vua")
        self.assertEqual(L.upgrade("nho", new_blocking=1)["lane"], "vua")
        self.assertTrue(L.upgrade("nho", diff_lines=500)["needs_clarify_again"])
        self.assertEqual(L.upgrade("nho", diff_lines=10, files=1)["lane"], "nho")
        self.assertEqual(L.upgrade("vua", diff_lines=2000)["lane"], "lon")
        self.assertEqual(L.upgrade("vua", diff_lines=200, red_twice=True)["lane"], "vua")
        for lane in ("lon", "de_xuat"):                       # never downgraded, never moved sideways
            self.assertEqual(L.upgrade(lane, diff_lines=1)["lane"], lane)


class BundleTest(unittest.TestCase):
    def test_one_message_blocking_first_then_assumptions_then_ac(self):
        qs = [{"ask": "Định dạng file?", "blocking": True, "assumption": ".xlsx"},
              {"ask": "Múi giờ?", "blocking": False, "assumption": "Asia/Ho_Chi_Minh"}]
        b = L.bundle("Export", qs, ["không đổi API cũ"], ["AC: trả .xlsx", ""], lane="vua")
        t = b["text"]
        self.assertLess(t.index("Định dạng file?"), t.index("Asia/Ho_Chi_Minh"))
        self.assertLess(t.index("Asia/Ho_Chi_Minh"), t.index("AC1."))
        self.assertNotIn("Múi giờ?", t)                       # a soft question is STATED as an assumption
        self.assertEqual((b["blocking"], b["assumptions"], b["ac"], b["needs_answer"]), (1, 2, 1, True))
        self.assertFalse(L.bundle("x", [], [], ["AC"])["needs_answer"])


class CliTest(unittest.TestCase):
    def test_triage_reports_kind_lane_profile(self):
        out = cli("triage.py", "classify", "--type", "bugfix", "--title", "Fix NPE", "--desc", "one line")
        self.assertEqual((out["tier"], out["kind"], out["lane"]), ("trivial", "code", "nho"))
        self.assertEqual(out["profile"]["ladder"], "short")
        self.assertEqual(cli("triage.py", "classify", "--type", "feature", "--desc", "x",
                             "--force-lane", "lon")["lane"], "lon")
        self.assertTrue(cli("triage.py", "upgrade", "--lane", "nho", "--files", "9")["upgraded"])

    def test_clarify_bundle_cli(self):
        out = cli("clarify.py", "bundle", "--type", "feature", "--title", "Export", "--desc", "Thêm nút export",
                  "--ac", "trả .xlsx")
        self.assertTrue(out["needs_answer"])
        self.assertIn("AC1. trả .xlsx", out["text"])
        self.assertEqual(out["verdict"], "needs_clarification")


if __name__ == "__main__":
    unittest.main()
