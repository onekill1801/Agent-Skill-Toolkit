"""Usage of long-lived token accounts from rate-limit headers + login expiry (integrations.claude_usage)."""

import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", ".claude", "skills", "_runtime"))

from agent_skill_toolkit.integrations import claude_usage as CU  # noqa: E402


class HeadersTest(unittest.TestCase):
    def test_headers_as_seen_on_2026_11_24(self):
        h = {"anthropic-ratelimit-unified-5h-utilization": "0.01", "anthropic-ratelimit-unified-5h-reset": "1795524600",
             "anthropic-ratelimit-unified-7d-utilization": "0.23", "anthropic-ratelimit-unified-7d-reset": "1795903200",
             "anthropic-ratelimit-unified-status": "allowed"}
        u = CU.usage_from_headers(h)
        self.assertEqual(u["session"], {"used": 1.0, "left": 99.0, "resets_at": 1795524600.0})
        self.assertEqual(u["weekly"], {"used": 23.0, "left": 77.0, "resets_at": 1795903200.0})

    def test_missing_or_garbage_headers(self):
        with self.assertRaises(ValueError):
            CU.usage_from_headers({})
        u = CU.usage_from_headers({"anthropic-ratelimit-unified-7d-utilization": "x",
                                   "anthropic-ratelimit-unified-5h-utilization": "1.0"})
        self.assertEqual(u["session"]["left"], 0.0)
        self.assertIsNone(u["weekly"])
        self.assertIsNone(u["session"]["resets_at"])


class LoginInfoTest(unittest.TestCase):
    def write(self, oauth):
        d = tempfile.mkdtemp()
        with open(os.path.join(d, ".credentials.json"), "w") as f:
            json.dump({"claudeAiOauth": oauth}, f)
        return d

    def test_blanked_login(self):
        d = self.write({"accessToken": "", "refreshTokenExpiresAt": 1793791747282, "loginEnded": "time_limit"})
        self.assertEqual(CU.login_info(d), {"expires_at": 1793791747.282, "ended": "time_limit"})

    def test_live_login_and_missing_file(self):
        d = self.write({"accessToken": "a", "refreshTokenExpiresAt": 1893791747000})
        self.assertEqual(CU.login_info(d), {"expires_at": 1893791747.0})
        self.assertEqual(CU.login_info(tempfile.mkdtemp()), {})


if __name__ == "__main__":
    unittest.main()
