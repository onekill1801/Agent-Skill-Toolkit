import os
import sys
import unittest
import tempfile
import json
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".claude", "skills", "dev-automation", "tools"))
import claude_account_rotation as rotation

NOW = 1_800_000_000_000
ACCOUNTS = [{"id": str(i)} for i in range(5)]


def quota(session, weekly, age=0):
    return {"status": "ok", "updatedAt": NOW - age,
            "session": {"usedPercent": session, "resetsAt": NOW + 10000},
            "weekly": {"usedPercent": weekly, "resetsAt": NOW + 100000}}


class RotationTests(unittest.TestCase):
    def decide(self, values, active="0"):
        return rotation.choose(ACCOUNTS, {str(i): v for i, v in enumerate(values)}, active, NOW)

    def test_either_window_triggers_round_robin(self):
        for usage in (quota(80, 5), quota(5, 80)):
            decision = self.decide([usage] + [quota(10, 10)] * 4)
            self.assertEqual((decision["action"], decision["accountId"], decision["threshold"]), ("switch", "1", 80))

    def test_keeps_current_until_boundary(self):
        self.assertEqual(self.decide([quota(79.99, 79.99)] * 5)["action"], "keep")

    def test_all_five_at_80_enters_95_tier(self):
        decision = self.decide([quota(80, 20)] * 5)
        self.assertEqual((decision["action"], decision["threshold"]), ("keep", 95))

    def test_95_tier_switches_to_next_available(self):
        decision = self.decide([quota(95, 80), quota(95, 80), quota(80, 90), quota(90, 80), quota(80, 80)])
        self.assertEqual((decision["accountId"], decision["threshold"]), ("2", 95))

    def test_exhaustion_waits_instead_of_looping(self):
        decision = self.decide([quota(95, 95)] * 5)
        self.assertEqual((decision["action"], decision["reason"]), ("wait", "all_accounts_at_95"))

    def test_reset_returns_to_80_tier_and_skips_weekly_exhaustion(self):
        decision = self.decide([quota(95, 90), quota(0, 95), quota(0, 79), quota(95, 80), quota(95, 80)])
        self.assertEqual((decision["accountId"], decision["threshold"]), ("2", 80))

    def test_unknown_does_not_promote_tier(self):
        decision = self.decide([quota(80, 80)] * 4 + [None])
        self.assertEqual((decision["action"], decision["threshold"], decision["reason"]), ("wait", 80, "quota_unknown"))

    def test_stale_failed_invalid_or_reset_usage_not_eligible(self):
        invalid = [quota(0, 0, 180001), quota(float("nan"), 0), quota(True, 0), quota(-1, 0), quota(101, 0)]
        failed = quota(0, 0)
        failed["error"] = "network"
        invalid.append(failed)
        expired = quota(0, 0)
        expired["session"]["resetsAt"] = NOW
        invalid.append(expired)
        for value in invalid:
            with self.subTest(value=value):
                self.assertIsNone(rotation.valid_usage(value, NOW))

    def test_round_robin_wraps(self):
        decision = self.decide([quota(0, 0)] * 4 + [quota(80, 0)], active="4")
        self.assertEqual(decision["accountId"], "0")

    def test_human_selecting_outside_pool_is_respected(self):
        self.assertEqual(self.decide([quota(0, 0)] * 5, active="other")["reason"], "active_account_outside_pool")

    def test_live_data_is_not_attributed_to_wrong_account(self):
        snapshot = {"claude": {"accounts": [{"id": "0", "managedAuthRuntime": "host"}], "activeAccountId": "0"},
                    "rateLimits": {"claude": dict(quota(0, 0), usageMetadata={"authProvenance": "managed:other"})}}
        with mock.patch.object(rotation, "rpc", return_value=snapshot), mock.patch.object(rotation, "host_usage", return_value=None):
            result = rotation.inspect_pool(["0"])
        self.assertEqual(result["accounts"][0]["usage"], None)
        self.assertEqual(result["decision"]["action"], "wait")

    def test_account_order_follows_configured_pool(self):
        snapshot = {"claude": {"accounts": [{"id": "1"}, {"id": "0"}], "activeAccountId": "0"},
                    "rateLimits": {}}
        with mock.patch.object(rotation, "rpc", return_value=snapshot), mock.patch.object(rotation, "host_usage", return_value=None):
            result = rotation.inspect_pool(["0", "1"])
        self.assertEqual([a["id"] for a in result["accounts"]], ["0", "1"])

    def test_apply_does_not_overwrite_a_human_account_switch(self):
        initial = {"decision": {"action": "switch", "accountId": "target"}, "activeAccountId": "old"}
        args = ["rotation", "--accounts", "00000000-0000-0000-0000-000000000000", "--apply"]
        with mock.patch.object(sys, "argv", args), mock.patch.object(rotation, "inspect_pool", return_value=initial), \
                mock.patch.object(rotation, "rpc", return_value={"claude": {"activeAccountId": "human-choice"}}) as rpc, \
                mock.patch("builtins.print"):
            self.assertEqual(rotation.main(), 0)
        rpc.assert_called_once_with("accounts.list", {"refreshUsage": False})
        self.assertEqual(initial["decision"]["reason"], "selection_changed")


class RefreshTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / ".credentials.json"
        self.original = {"claudeAiOauth": {"accessToken": "old", "refreshToken": "old-refresh",
                                          "expiresAt": 0, "subscriptionType": "team"}, "mcpOAuth": {"keep": True}}
        self.path.write_text(json.dumps(self.original), encoding="utf-8")
        rotation.REFRESH_RETRY_AT.clear()
        rotation.AUTH_STATUS.clear()
        self.rpc = mock.patch.object(rotation, "rpc", return_value={"claude": {
            "activeAccountIdsByRuntime": {"host": "other", "wsl": {}}}})
        self.rpc.start()
        self.addCleanup(self.rpc.stop)

    def opener(self, response):
        opener = mock.Mock()
        opener.open.return_value.__enter__ = mock.Mock(return_value=response)
        opener.open.return_value.__exit__ = mock.Mock(return_value=False)
        return mock.patch.object(rotation.urllib.request, "build_opener", return_value=opener)

    def test_rotated_token_is_persisted_and_other_fields_preserved(self):
        response = mock.Mock()
        with self.opener(response), mock.patch.object(rotation.json, "load", return_value={
            "access_token": "new", "refresh_token": "new-refresh", "expires_in": 3600, "scope": "user:inference"}):
            updated = rotation.renew_inactive_credentials(self.path, "test")
        stored = json.loads(self.path.read_text())
        self.assertEqual(stored, updated)
        self.assertEqual(stored["mcpOAuth"], {"keep": True})
        self.assertEqual(stored["claudeAiOauth"]["refreshToken"], "new-refresh")
        self.assertEqual(stored["claudeAiOauth"]["subscriptionType"], "team")

    def test_active_account_is_not_refreshed_by_watcher(self):
        with mock.patch.object(rotation, "rpc", return_value={"claude": {
                "activeAccountIdsByRuntime": {"host": "test", "wsl": {}}}}), \
                mock.patch.object(rotation.urllib.request, "build_opener") as opener:
            self.assertIsNone(rotation.renew_inactive_credentials(self.path, "test"))
        opener.assert_not_called()
        self.assertEqual(json.loads(self.path.read_text()), self.original)

    def test_concurrent_refresh_result_is_not_overwritten(self):
        replacement = dict(self.original, otherClient=True)
        def result(_):
            self.path.write_text(json.dumps(replacement), encoding="utf-8")
            return {"access_token": "new", "expires_in": 3600}
        with self.opener(mock.Mock()), mock.patch.object(rotation.json, "load", side_effect=result):
            rotation.renew_inactive_credentials(self.path, "test")
        self.assertEqual(json.loads(self.path.read_text()), replacement)

    def test_rate_limit_waits_before_retrying_and_keeps_credentials(self):
        error = rotation.urllib.error.HTTPError("https://test", 429, "limited", {}, None)
        opener = mock.Mock()
        opener.open.side_effect = error
        with mock.patch.object(rotation.urllib.request, "build_opener", return_value=opener):
            self.assertIsNone(rotation.renew_inactive_credentials(self.path, "test"))
            self.assertIsNone(rotation.renew_inactive_credentials(self.path, "test"))
        self.assertEqual(opener.open.call_count, 1)
        self.assertEqual(json.loads(self.path.read_text()), self.original)

    def test_single_instance_lock_releases_on_close(self):
        path = Path(self.temp.name) / "watch.lock"
        with rotation.FileLock(path):
            with self.assertRaises(RuntimeError):
                with rotation.FileLock(path):
                    pass
        with rotation.FileLock(path):
            pass


if __name__ == "__main__":
    unittest.main()
