"""Agent accounts view: who runs what + usage left (workflows/execution/accounts.py, auto-dev accounts.py)."""

import json
import os
import runpy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
runpy.run_path(str(ROOT / ".claude" / "skills" / "_runtime" / "bootstrap.py"))["activate"]()
from agent_skill_toolkit.core import store as S  # noqa: E402
from agent_skill_toolkit.workflows.execution import accounts as A  # noqa: E402
from agent_skill_toolkit.workflows.execution import usage as U  # noqa: E402

TOOLS = ROOT / ".claude" / "skills" / "auto-dev" / "tools"
sys.path.insert(0, str(TOOLS))
import accounts as ACC  # noqa: E402
import agent_runner as AR  # noqa: E402

NOW = 2_000_000_000.0
ID1, ID2, ID3 = "11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222", \
    "33333333-3333-3333-3333-333333333333"
CFG = {"accounts": {"claude": {"a": {"orca_id": ID1}, "b": {"orca_id": ID2}, "c": {"orca_id": ID3},
                               "home": {"env": {"HOME": "~/x"}}},
                    "codex": {"main": {"orca_id": "system-default"}}}}


def bucket(used, reset_in):
    return {"usedPercent": used, "resetsAt": (NOW + reset_in) * 1000}


def orca(**over):
    o = {"claude": {"activeAccountId": ID1, "accounts": [
            {"id": ID1, "email": "a@x", "organizationName": "P1"},
            {"id": ID2, "email": "b@x", "organizationName": "S2"},
            {"id": ID3, "email": "c@x"}]},
         "codex": {"systemDefault": {"email": "cx@x", "workspaceLabel": "Plus"}},
         "rateLimits": {
             "claude": {"status": "ok", "updatedAt": NOW * 1000, "session": bucket(10, 3600),
                        "weekly": bucket(40, 86400), "usageMetadata": {"authProvenance": f"managed:{ID1}"}},
             "codex": {"status": "ok", "updatedAt": NOW * 1000, "session": None, "weekly": bucket(59, 3600)},
             "inactiveClaudeAccounts": [
                 {"accountId": ID2, "rateLimits": {"status": "ok", "updatedAt": (NOW - 600) * 1000,
                                                   "session": bucket(96, -60), "weekly": bucket(30, 9000)}}]}}
    o.update(over)
    return o


class ReportTest(unittest.TestCase):
    def rows(self, rep):
        return {r["account"]: r for r in rep["accounts"]}

    def test_live_cached_and_unknown_usage(self):
        r = self.rows(A.report(CFG, orca(), [], [], NOW))
        a, b, c = r["claude@a"], r["claude@b"], r["claude@c"]
        self.assertEqual((a["usage_source"], a["usage"]["session"]["left"], a["orca_active"]), ("live", 90, True))
        self.assertEqual((b["usage_source"], b["usage"]["age_min"], b["usage"]["weekly"]["left"]), ("cached", 10, 70))
        self.assertTrue(b["usage"]["session"]["reset_passed"])           # old reading of a window that reset
        self.assertIsNone(b["usage"]["session"]["left"])
        self.assertEqual((c["usage_source"], c["usage"], c["email"]), ("none", None, "c@x"))
        self.assertEqual((r["claude@home"]["email"], r["claude@home"]["usage"]), (None, None))
        self.assertEqual((r["codex@main"]["email"], r["codex@main"]["usage"]["weekly"]["left"]), ("cx@x", 41))

    def test_live_usage_needs_matching_provenance(self):
        o = orca()
        o["rateLimits"]["claude"]["usageMetadata"] = {"authProvenance": "credentials-file"}
        self.assertEqual(self.rows(A.report(CFG, o, [], [], NOW))["claude@a"]["usage_source"], "none")

    def test_running_slots_and_todays_turns(self):
        slots = [{"key": "claude@a", "info": {"pid": 1, "started": NOW - 120, "role": "review", "task_id": "t1",
                                              "step": "review"}},
                 {"key": "claude@b", "info": {}, "mtime": NOW - 4 * 3600},        # crashed run: ignored
                 {"key": "claude@default", "info": {"started": NOW, "role": "x"}}]
        turns = [{"model": "claude:sonnet@a", "seconds": 30, "ts": NOW - 50, "task_id": "t0", "step": "s"},
                 {"model": "claude:haiku@a", "seconds": 5, "ts": NOW - 10, "task_id": "t1", "step": "review"},
                 {"model": "claude:", "seconds": 7, "ts": NOW - 5}]
        rep = A.report(CFG, orca(), slots, turns, NOW)
        a = self.rows(rep)["claude@a"]
        self.assertEqual([(x["role"], x["task_id"], x["minutes"]) for x in a["running"]], [("review", "t1", 2.0)])
        self.assertEqual(a["today"], {"runs": 2, "seconds": 35.0})
        self.assertEqual((a["last"]["task_id"], a["last"]["model"]), ("t1", "haiku"))
        self.assertEqual(self.rows(rep)["claude@b"]["running"], [])
        self.assertEqual((rep["unattributed"]["today"]["runs"], len(rep["unattributed"]["running"])), (1, 1))

    def test_stale_cutoff_matches_agent_runner(self):
        slots = [{"key": "claude@a", "info": {"started": NOW - 60}, "mtime": NOW - 3600},   # 1 h: crashed
                 {"key": "claude@a", "info": {}, "mtime": NOW - 600}]                       # 10 min: live
        a = self.rows(A.report(CFG, orca(), slots, [], NOW, stale_s=AR.SLOT_STALE_S))["claude@a"]
        self.assertEqual([x["minutes"] for x in a["running"]], [10.0])
        self.assertEqual(AR.SLOT_STALE_S, AR.CLI_TIMEOUT * 3)

    def test_orca_unavailable_and_removed_account(self):
        rep = A.report(CFG, None, [], [], NOW)
        self.assertEqual(rep["orca"], "unavailable")
        self.assertNotIn("warning", self.rows(rep)["claude@a"])
        o = orca()
        o["claude"]["accounts"] = o["claude"]["accounts"][:1]
        self.assertIn("warning", self.rows(A.report(CFG, o, [], [], NOW))["claude@b"])

    def test_garbage_numbers_are_unknown(self):
        o = orca()
        o["rateLimits"]["claude"]["session"] = {"usedPercent": True}
        o["rateLimits"]["claude"]["error"] = None
        a = self.rows(A.report(CFG, o, [], [], NOW))["claude@a"]
        self.assertIsNone(a["usage"]["session"])
        o["rateLimits"]["claude"]["error"] = "boom"
        self.assertEqual(self.rows(A.report(CFG, o, [], [], NOW))["claude@a"]["usage_source"], "none")


class AttributionTest(unittest.TestCase):
    """agent_runner records the account of every role turn (usage row + slot file)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.env = mock.patch.dict(os.environ, {"TOOLKIT_STORE": os.path.join(self.tmp, "t.db"),
                                                "TOOLKIT_TASK_ID": "", "TOOLKIT_STEP": ""})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_reserve_keeps_the_account(self):
        with S.Store() as st:
            U.reserve(st, "", "", "claude", "sonnet", 60, account="a")
            U.reserve(st, "", "", "claude", "sonnet", 60)
            models = [r["model"] for r in st.db.execute("select model from usage order by id")]
        self.assertEqual(models, ["claude:sonnet@a", "claude:sonnet"])

    def test_run_role_records_account_and_slot_info(self):
        cfg = {"accounts": {"claude": {"a": {"env": {"CLAUDE_CONFIG_DIR": "~/cfg-a"}}}},
               "roles": {"review": ["claude:sonnet@a"]}}
        seen = {}

        def fake_cli(prompt, system, backend, model, tpl, via, timeout, cwd=None, env_extra=None, read_only=False):
            seen["env"] = env_extra
            seen["slots"] = ACC.read_slots(AR.slots_dir())
            return "OK"
        with mock.patch.object(AR, "_agents_config", return_value=cfg), \
                mock.patch.object(AR, "_run_cli", side_effect=fake_cli), \
                mock.patch.object(AR, "slots_dir", return_value=os.path.join(self.tmp, "slots")), \
                mock.patch.dict(os.environ, {"TOOLKIT_TASK_ID": "t9", "TOOLKIT_STEP": "review"}):
            out = AR.run_role("hi", "review")
            after = ACC.read_slots(AR.slots_dir())
        self.assertEqual(out["used"], "claude:sonnet@a")
        self.assertEqual(seen["env"]["CLAUDE_CONFIG_DIR"], os.path.expanduser("~/cfg-a"))
        [slot] = seen["slots"]
        self.assertEqual((slot["key"], slot["info"]["role"], slot["info"]["task_id"], slot["info"]["step"]),
                         ("claude@a", "review", "t9", "review"))
        self.assertEqual(after, [])                                      # released after the turn
        with S.Store() as st:
            self.assertEqual([r["model"] for r in st.db.execute("select model from usage")], ["claude:sonnet@a"])

    def test_read_turns_is_read_only(self):
        path = os.environ["TOOLKIT_STORE"]
        with S.Store() as st:
            U.reserve(st, "", "", "claude", "sonnet", 60, account="a")
        before = (os.path.getsize(path), os.path.getmtime(path))
        with mock.patch.object(S, "Store", side_effect=AssertionError("no Store() in a read-only view")):
            rows = ACC.read_turns()
        self.assertEqual([r["model"] for r in rows], ["claude:sonnet@a"])
        self.assertEqual((os.path.getsize(path), os.path.getmtime(path)), before)
        self.assertEqual(ACC.read_turns(os.path.join(self.tmp, "missing.db")), [])
        import sqlite3
        empty = os.path.join(self.tmp, "empty.db")
        sqlite3.connect(empty).close()
        self.assertEqual(ACC.read_turns(empty), [])                     # no usage table yet

    def test_slot_release_retries_while_a_reader_holds_it(self):
        d = os.path.join(self.tmp, "slots")
        real = os.remove
        calls = []

        def flaky(p):
            calls.append(p)
            if len(calls) == 1:
                raise PermissionError(32, "in use")                   # Windows: a reader has it open
            real(p)
        with mock.patch.object(AR, "slots_dir", return_value=d):
            slot = AR._Slot("claude@a", 1)
            self.assertIsNotNone(slot.__enter__())
            with mock.patch.object(AR.os, "remove", side_effect=flaky), mock.patch.object(AR.time, "sleep"):
                slot.__exit__()
        self.assertEqual(len(calls), 2)
        self.assertEqual(os.listdir(d), [])

    def test_read_slots_accepts_old_pid_only_files(self):
        d = os.path.join(self.tmp, "s")
        os.makedirs(d)
        open(os.path.join(d, "claude@a.0.lock"), "w").write("1234")
        open(os.path.join(d, "codex@main.1.lock"), "w").write("not json")
        open(os.path.join(d, "readme.txt"), "w").write("x")
        got = sorted((s["key"], s["info"]) for s in ACC.read_slots(d))
        self.assertEqual(got, [("claude@a", {"pid": 1234}), ("codex@main", {})])

    def test_render_marks_active_reset_and_missing(self):
        rep = A.report(CFG, orca(), [], [], NOW)
        text = ACC.render(rep)
        self.assertIn("claude@a *", text)
        self.assertIn("reset?", text)
        self.assertIn("rảnh", text)
        self.assertIn("Orca không đọc được", ACC.render(A.report(CFG, None, [], [], NOW)))


if __name__ == "__main__":
    unittest.main()
