"""Account heartbeat (auto-dev/tools/accounts.py): throttled, logs state CHANGES only, no model turn."""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", ".claude", "skills", "auto-dev", "tools"))

import accounts  # noqa: E402


def _rep(dead=()):
    rows = []
    for n in ("a", "b"):
        rt = {"state": "skipped", "reason": "login ended"} if n in dead else {"state": "ok"}
        rows.append({"account": f"claude@{n}", "routing": rt,
                     "usage": {"session": {"left": 40}, "weekly": {"left": 70}}})
    return {"accounts": rows}


class HeartbeatTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "account_heartbeat.json")
        self.logged = []
        fake_dc = mock.MagicMock()
        fake_dc.health_log.side_effect = lambda label, event, detail="": self.logged.append((label, event, detail))
        for p in (mock.patch.object(accounts, "heartbeat_path", return_value=self.path),
                  mock.patch.object(accounts, "heartbeat_interval_s", return_value=300),
                  mock.patch.dict(sys.modules, {"daemon_common": fake_dc})):
            p.start()
            self.addCleanup(p.stop)

    def test_throttled_and_logs_only_changes(self):
        with mock.patch.object(accounts, "gather", return_value=_rep()) as g:
            out = accounts.heartbeat(now=1000)
            self.assertEqual((out["ok"], out["total"]), (2, 2))
            self.assertEqual(len(self.logged), 2)                       # first sight of each account
            self.assertEqual(accounts.heartbeat(now=1100)["heartbeat"], "skipped")   # inside the interval
            self.assertEqual(g.call_count, 1)
        self.logged.clear()
        with mock.patch.object(accounts, "gather", return_value=_rep(dead=("b",))):
            out = accounts.heartbeat(now=1400)
        self.assertEqual(out["changes"], ["claude@b: ok → dead (login ended)"])
        self.assertEqual(self.logged, [("accounts", "changed", "claude@b: ok → dead (login ended)")])
        with open(self.path, encoding="utf-8") as f:
            saved = json.load(f)
        self.assertEqual(saved["accounts"]["claude@a"], {"state": "ok", "reason": "", "left": 40})
        self.assertEqual(saved["at"], 1400)

    def test_force_ignores_the_interval(self):
        with mock.patch.object(accounts, "gather", return_value=_rep()) as g:
            accounts.heartbeat(now=1000)
            accounts.heartbeat(force=True, now=1001)
            self.assertEqual(g.call_count, 2)

    def test_limited_state(self):
        row = {"account": "claude@a", "routing": {"state": "ok", "limited": {"opus": 99}}, "usage": None}
        self.assertEqual(accounts.account_state(row)["state"], "limited")


class TokenOwnerTest(unittest.TestCase):
    """24/11: chungtv8 held a copy of dangnd4's token — the token's organization id gives it away."""
    ORCA = {"claude": {"accounts": [{"id": "o-d", "email": "dangnd4@x", "organizationUuid": "org-d"},
                                    {"id": "o-h", "email": "hang@x", "organizationUuid": "org-h"}]}}
    CFG = {"accounts": {"claude": {"dangnd4": {"orca_id": "o-d"}, "hang": {"orca_id": "o-h"}, "chungtv8": {}}}}

    def rows(self):
        return [{"account": f"claude@{n}", "backend": "claude", "name": n} for n in ("dangnd4", "hang", "chungtv8")]

    def test_copied_token_is_flagged(self):
        rows = self.rows()
        api = {"claude@dangnd4": {"org": "org-d"}, "claude@hang": {"org": "org-d"}, "claude@chungtv8": {"org": "org-d"}}
        accounts.check_token_owners(self.CFG, self.ORCA, api, rows)
        self.assertNotIn("warning", rows[0])                      # its own token
        self.assertIn("thuộc dangnd4", rows[1]["warning"])        # Orca says hang is org-h
        self.assertIn("thuộc dangnd4", rows[2]["warning"])        # no orca_id, but org-d is dangnd4's

    def test_shared_organization_is_not_flagged_and_system_default_counts(self):
        orca = {"claude": {"accounts": [{"id": "o-u", "email": "dung@x", "organizationUuid": "org-p2"},
                                        {"id": "o-d", "email": "dang@x", "organizationUuid": "org-d"}],
                           "systemDefaultEmail": "chung@x", "systemDefaultOrganizationUuid": "org-p2"}}
        cfg = {"accounts": {"claude": {"dung": {"orca_id": "o-u"}, "chung": {"orca_id": "system-default"}}}}
        rows = [{"account": f"claude@{n}", "backend": "claude", "name": n} for n in ("dung", "chung")]
        accounts.check_token_owners(cfg, orca, {"claude@dung": {"org": "org-p2"}, "claude@chung": {"org": "org-d"}}, rows)
        self.assertNotIn("warning", rows[0])                      # same org as chung: proves nothing
        self.assertIn("thuộc dang@x", rows[1]["warning"])         # chung = Orca system default, org-p2

    def test_silent_without_a_reading_or_orca(self):
        rows = self.rows()
        accounts.check_token_owners(self.CFG, None, {"claude@hang": {"org": "org-d"}}, rows)
        accounts.check_token_owners(self.CFG, self.ORCA, {}, rows)
        self.assertFalse(any("warning" in r for r in rows))


class ParallelSplitTest(unittest.TestCase):
    """24/11: 'I want 10 sessions in parallel, split over the active accounts'."""
    CFG = {"parallel": {"claude": 10}, "limits": {"codex@main": {"max_parallel": 2}},
           "accounts": {"claude": {n: {} for n in ("a", "b", "c", "d")}, "codex": {"main": {}}}}

    def test_split_over_live_accounts(self):
        import agent_runner as ar
        dead = {"c", "d"}
        with mock.patch.object(ar, "account_dead", side_effect=lambda cfg, b, n: "x" if n in dead else ""):
            self.assertEqual(ar.account_limit(self.CFG, "claude", "a"), 5)          # 10 / 2 live
            dead.discard("c")
            self.assertEqual(ar.account_limit(self.CFG, "claude", "a"), 4)          # ceil(10 / 3)
            dead.update({"a", "b", "c", "d"})
            self.assertEqual(ar.account_limit(self.CFG, "claude", "a"), 10)         # none live: never 0
            self.assertEqual(ar.account_limit(self.CFG, "codex", "main"), 2)        # no total: per-account limit
            self.assertEqual(ar.account_limit({}, "claude", "a"), 99)


if __name__ == "__main__":
    unittest.main()
