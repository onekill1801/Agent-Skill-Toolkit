"""F.22: agent turns go to the account with the most usage left (per model group), not always the first."""

import json
import os
import runpy
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
runpy.run_path(str(ROOT / ".claude" / "skills" / "_runtime" / "bootstrap.py"))["activate"]()
from agent_skill_toolkit.workflows.execution import accounts as A  # noqa: E402

sys.path.insert(0, str(ROOT / ".claude" / "skills" / "auto-dev" / "tools"))
import accounts as tool  # noqa: E402
import agent_runner  # noqa: E402

ROLE = ["claude:opus@a", "claude:opus@b", "claude:opus@c", "claude:sonnet@a", "claude:sonnet@b", "codex@main"]


class OrderTest(unittest.TestCase):
    def test_most_left_first_inside_each_model_group(self):
        usage = {"claude@a": {"left": 20}, "claude@b": {"left": 80}, "claude@c": {"left": 50}}
        self.assertEqual(A.order_by_load(ROLE, usage, {}),
                         ["claude:opus@b", "claude:opus@c", "claude:opus@a",
                          "claude:sonnet@b", "claude:sonnet@a", "codex@main"])     # opus still before sonnet

    def test_unknown_after_known_good_and_low_last(self):
        usage = {"claude@a": {"left": 5}, "claude@b": {"left": None, "error": "expired"}, "claude@c": {"left": 60}}
        self.assertEqual(A.order_by_load(ROLE[:3], usage, {}), ["claude:opus@c", "claude:opus@b", "claude:opus@a"])

    def test_ties_go_to_fewer_runs_today_then_written_order(self):
        usage = {"claude@a": {"left": 70}, "claude@b": {"left": 70}, "claude@c": {"left": 70}}
        self.assertEqual(A.order_by_load(ROLE[:3], usage, {"claude@a": 5, "claude@b": 1, "claude@c": 1}),
                         ["claude:opus@b", "claude:opus@c", "claude:opus@a"])
        self.assertEqual(A.order_by_load(ROLE, {}, {}), ROLE)               # no data: written order
        self.assertEqual(sorted(A.order_by_load(ROLE, {"x": {}}, {})), sorted(ROLE))   # nothing dropped

    def test_normalize_takes_the_tighter_window_and_a_reset_is_full(self):
        now = time.time()
        api = {"claude@a": {"usage": {"session": {"left": 90, "resets_at": now + 100},
                                      "weekly": {"left": 30, "resets_at": now + 1000}}, "at": now},
               "claude@b": {"usage": {"session": {"left": 1, "resets_at": now - 5},       # reset since: full
                                      "weekly": {"left": 70, "resets_at": now + 1000}}, "at": now - 60},
               "claude@c": {"error": "expired"}}
        n = A.normalize_readings(api, now)
        self.assertEqual(n["claude@a"]["left"], 30)
        self.assertEqual(n["claude@b"]["left"], 70)
        self.assertEqual(n["claude@c"], {"left": None, "error": "expired"})

    def test_cache_round_trip_and_staleness(self):
        path = os.path.join(tempfile.mkdtemp(), "u.json")
        now = time.time()
        A.save_usage(path, {"claude@a": {"usage": {"session": {"left": 40}}, "at": now}}, now)
        with open(path, encoding="utf-8") as f:
            self.assertNotIn("token", f.read().lower())
        self.assertEqual(A.load_usage(path, now=now)["claude@a"]["left"], 40)
        self.assertIsNone(A.load_usage(path, now=now + A.USAGE_MAX_AGE_S + 1))
        self.assertIsNone(A.load_usage(path + ".missing"))


class RunnerTest(unittest.TestCase):
    def test_runs_today_by_account(self):
        turns = [{"model": "claude:opus@a"}, {"model": "claude:sonnet@a"}, {"model": "codex@main"}, {"model": ""}]
        self.assertEqual(tool.runs_today_by_account(turns), {"claude@a": 2, "codex@main": 1})

    def test_balanced_uses_the_cache_and_can_be_switched_off(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "u.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"at": time.time(), "accounts": {"claude@c": {"left": 99}}}, f)
        with mock.patch.object(tool, "usage_cache_path", return_value=path), \
             mock.patch.object(tool, "runs_today_by_account", return_value={}), \
             mock.patch.object(tool, "read_api_usage") as fetch:
            self.assertEqual(agent_runner._balanced(ROLE[:3], {})[0], "claude:opus@c")
            fetch.assert_not_called()                                         # fresh cache: no API call
            with mock.patch.object(agent_runner.config, "get",
                                   side_effect=lambda k, d=None: "0" if k == "ACCOUNT_BALANCE" else d):
                self.assertEqual(agent_runner._balanced(ROLE[:3], {}), ROLE[:3])

    def test_balancing_never_blocks_a_turn(self):
        with mock.patch.object(tool, "usage_cache_path", side_effect=RuntimeError("boom")):
            self.assertEqual(agent_runner._balanced(ROLE, {}), ROLE)


if __name__ == "__main__":
    unittest.main()
