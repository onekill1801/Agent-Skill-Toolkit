#!/usr/bin/env python3
"""Usage throttling + hard budgets (checklist F.11).
    python -m unittest discover -s tests
"""

import contextlib
import io
import json
import os
import runpy
import sys
import tempfile
import time
import unittest
from unittest import mock

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
runpy.run_path(os.path.join(_ROOT, ".claude", "skills", "_runtime", "bootstrap.py"))["activate"]()
from agent_skill_toolkit.core import store as S  # noqa: E402
from agent_skill_toolkit.workflows import engine as E  # noqa: E402
from agent_skill_toolkit.workflows.execution import usage as U  # noqa: E402

sys.path.insert(0, os.path.join(_ROOT, ".claude", "skills", "auto-dev", "tools"))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="usage-")
        mock.patch.dict(os.environ, {"TOOLKIT_STORE": os.path.join(self.tmp, "t.db"), "TOOLKIT_STATE_DIR": self.tmp,
                                     "WORK_DIR": self.tmp, "TOOLKIT_MAX_PARALLEL": "0"}).start()
        os.environ.pop("TOOLKIT_USAGE_TIER", None)
        self.st = S.Store()

    def tearDown(self):
        self.st.close()
        mock.patch.stopall()

    def refs_claimable(self):
        got = []
        while True:
            t = self.st.claim("w")
            if not t:
                return sorted(got)
            got.append(t["ref"])


class TierTest(Base):
    def test_thresholds(self):
        self.assertEqual([U.tier_of(p) for p in (None, 0, 79.9, 80, 94, 95, 100)],
                         ["normal", "normal", "normal", "save", "save", "critical", "critical"])
        with self.assertRaises(ValueError):
            U.set_level(self.st, 120)

    def test_stale_report_means_normal(self):
        U.set_level(self.st, 97)
        self.assertEqual(U.level(self.st)["tier"], "critical")
        with mock.patch("time.time", return_value=time.time() + U.USAGE_TTL_S + 5):
            self.assertEqual(U.level(self.st)["tier"], "normal")
            self.assertEqual(self.st.claim_policy(), {})

    def add_mix(self):
        self.st.add("review_mr", "mr")
        self.st.add("code", "small", data={"lane": "nho"})
        self.st.add("code", "medium", data={"lane": "vua"})
        self.st.add("proposal", "adr")
        self.st.add("code", "adr2", data={"lane": "de_xuat"})
        self.st.add("code", "scan", data={"intake": {"source": "schedule"}})

    def test_save_tier_holds_proposals_and_scans(self):
        self.add_mix()
        U.set_level(self.st, 85, "claude")
        self.assertEqual(self.refs_claimable(), ["medium", "mr", "small"])

    def test_critical_tier_only_reviews_and_small_lane(self):
        self.add_mix()
        U.set_level(self.st, 96)
        self.assertEqual(self.refs_claimable(), ["mr", "small"])

    def test_back_to_normal(self):
        self.add_mix()
        U.set_level(self.st, 99)
        U.set_level(self.st, 10)
        self.assertEqual(len(self.refs_claimable()), 6)

    def test_unreadable_policy_claims_nothing(self):
        self.add_mix()
        self.st.set_setting("claim_policy", "{broken")
        self.assertEqual(self.refs_claimable(), [])

    def test_model_per_step(self):
        self.assertEqual(U.model_for("implement", "opus", "save"), "sonnet")
        self.assertEqual(U.model_for("implement", "sonnet", "save"), "sonnet")
        self.assertEqual(U.model_for("log_read", "sonnet", "critical"), "haiku")
        self.assertEqual(U.model_for("implement", "opus", "normal"), "opus")

    def test_agent_runner_downgrades_claude_models(self):
        import agent_runner as ar
        with open(os.path.join(self.tmp, "agents.json"), "w") as f:
            json.dump({"roles": {"implement": ["claude:opus@work"], "log_read": ["claude:sonnet"]}}, f)
        seen = []
        with mock.patch.object(ar, "run_turn", side_effect=lambda *a, **k: seen.append(k["model"]) or "ok"):
            with mock.patch.dict(os.environ, {"TOOLKIT_USAGE_TIER": "save"}):
                out = ar.run_role("p", "implement")
                ar.run_role("p", "log_read")
            ar.run_role("p", "implement")                        # store has no report -> normal
        self.assertEqual(seen, ["sonnet", "haiku", "opus"])
        self.assertIn("opus -> sonnet", json.dumps(out["tried"]))


class BudgetTest(Base):
    def steps(self):
        def agent_step(ctx):
            ctx.usage(U.AGENT_BACKEND, "implement", seconds=30)
            return {}
        return [("a1", agent_step), ("a2", agent_step), ("a3", agent_step)]

    def test_task_budget_blocks(self):
        t = self.st.add("k", "x", data={"budget": {"agent_runs": 2}})
        res = E.run(self.st, self.st.claim("w"), self.steps())
        self.assertEqual(res["status"], "blocked")
        self.assertEqual(res["step"], "a3")
        self.assertIn("2/2 agent runs", self.st.get(t["id"])["last_error"])

    def test_task_seconds_budget(self):
        self.st.add("k", "x", data={"budget": {"agent_seconds": 45}})
        res = E.run(self.st, self.st.claim("w"), self.steps())
        self.assertEqual((res["status"], res["step"]), ("blocked", "a3"))

    def test_daily_cap_pauses_until_midnight(self):
        self.st.set_setting("daily_agent_runs", "1")
        t = self.st.add("k", "x")
        res = E.run(self.st, self.st.claim("w"), self.steps())
        self.assertEqual((res["status"], res["step"]), ("paused_quota", "a2"))
        got = self.st.get(t["id"])
        self.assertEqual(got["attempts"], 0)
        self.assertGreater(got["resume_at"], time.time())
        self.assertLessEqual(got["resume_at"], U._local_midnight() + 86400 + 61)

    def test_no_budget_runs_everything(self):
        self.st.add("k", "x")
        self.assertEqual(E.run(self.st, self.st.claim("w"), self.steps())["status"], "done")


class CliTest(Base):
    def cli(self, *argv):
        import coordinator as cf
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cf.main(list(argv))
        return json.loads(buf.getvalue())

    def test_usage_and_budget_commands(self):
        out = self.cli("usage", "88", "--source", "claude")
        self.assertEqual((out["tier"], out["claim_policy"]["tier"]), ("save", "save"))
        self.assertEqual(self.cli("usage")["pct"], 88)
        self.assertEqual(self.cli("budget", "--daily-agent-runs", "30")["daily_agent_runs"], 30)
        self.assertTrue(self.cli("usage", "150").get("error"))


if __name__ == "__main__":
    unittest.main()
