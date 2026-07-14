#!/usr/bin/env python3
"""Stdlib unittest cho vòng lặp lên plan của debate_engine.

Bảo đảm tầng Plan có vòng critique↔rebuttal thật sự (không chỉ 1 lượt):
  - APPROVE từ Architect -> hội tụ sớm, bỏ rebuttal thừa.
  - REVISE liên tục      -> chạy đủ max_rounds rồi Moderator chốt.
  - Architect ở vòng > 1 soi CHÍNH bản rebuttal mới nhất (đóng lỗ "rebuttal không ai review").
No pip deps.
"""

import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(
    _HERE, "..", ".claude", "skills", "auto-dev", "tools")))

import debate_engine as de  # noqa: E402


class ScriptedBackend:
    """Backend giả: trả critique APPROVE/REVISE theo kịch bản, ghi lại user_content mỗi lượt."""

    def __init__(self, approve_at_critique=None):
        self.label = "scripted"
        self.approve_at = approve_at_critique  # số thứ tự lượt critique sẽ APPROVE (1-based) hoặc None
        self.crit = 0
        self.seen = []  # (tag, user_content)

    def complete(self, system, user_content, output_tag):
        self.seen.append((output_tag, user_content))
        if output_tag == "dev_proposal":
            return "<dev_proposal><approach>P</approach></dev_proposal>"
        if output_tag == "dev_rebuttal":
            return f"<dev_rebuttal><approach>R{self.crit}</approach></dev_rebuttal>"
        if output_tag == "final_specification":
            return "<final_specification><approach>F</approach></final_specification>"
        if output_tag == "architect_critique":
            self.crit += 1
            verdict = "APPROVE" if self.approve_at == self.crit else "REVISE"
            return (f"<architect_critique><risk category='cache'>c</risk>"
                    f"<verdict>{verdict}</verdict></architect_critique>")
        return "<x/>"


def _engine(backend, rounds):
    return de.DebateEngine(backend, de.Narrator(use_color=False), max_rounds=rounds)


class DebateLoopTest(unittest.TestCase):
    def test_converges_early_on_approve(self):
        b = ScriptedBackend(approve_at_critique=2)
        res = _engine(b, rounds=5).run("T", "demo")
        self.assertTrue(res["converged"])
        self.assertEqual(res["rounds_used"], 2)
        # Vòng 1 REVISE -> có rebuttal; vòng 2 APPROVE -> KHÔNG sinh rebuttal thừa.
        self.assertIsNotNone(res["rounds"][0]["rebuttal"])
        self.assertIsNone(res["rounds"][1]["rebuttal"])

    def test_runs_full_rounds_without_approve(self):
        b = ScriptedBackend(approve_at_critique=None)
        res = _engine(b, rounds=3).run("T", "demo")
        self.assertFalse(res["converged"])
        self.assertEqual(res["rounds_used"], 3)

    def test_rounds_1_reproduces_single_pass(self):
        # --rounds 1: đúng một critique; không approve -> không rebuttal (hết vòng) -> Moderator chốt.
        b = ScriptedBackend(approve_at_critique=None)
        res = _engine(b, rounds=1).run("T", "demo")
        self.assertEqual(res["rounds_used"], 1)
        self.assertIsNone(res["rounds"][0]["rebuttal"])
        self.assertIn("<approach>F</approach>", res["final_specification"])

    def test_architect_reviews_latest_rebuttal(self):
        # Lỗ hổng cũ: rebuttal không ai review. Giờ critique vòng 2 phải chứa bản rebuttal vòng 1.
        b = ScriptedBackend(approve_at_critique=None)
        _engine(b, rounds=2).run("T", "demo")
        critique_inputs = [uc for (tag, uc) in b.seen if tag == "architect_critique"]
        self.assertEqual(len(critique_inputs), 2)
        self.assertIn("R1", critique_inputs[1])  # vòng 2 soi rebuttal "R1" của vòng 1

    def test_max_rounds_floor_is_one(self):
        b = ScriptedBackend(approve_at_critique=None)
        self.assertEqual(_engine(b, rounds=0).max_rounds, 1)

    def test_is_approve_helper(self):
        self.assertTrue(de.DebateEngine._is_approve("<verdict>APPROVE</verdict>"))
        self.assertTrue(de.DebateEngine._is_approve("<verdict> approve </verdict>"))
        self.assertFalse(de.DebateEngine._is_approve("<verdict>REVISE</verdict>"))
        self.assertFalse(de.DebateEngine._is_approve("không có thẻ verdict"))


if __name__ == "__main__":
    unittest.main()
