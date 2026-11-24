#!/usr/bin/env python3
"""claude_token.py: token capture from a terminal screen, secret file, agents.json wiring."""

import json
import os
import stat
import sys
import tempfile
import unittest
from unittest import mock

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(_ROOT, ".claude", "skills", "auto-dev", "tools"))
import claude_token as ct  # noqa: E402

TOK = "sk-ant-oat01-" + "Ab3_-x" * 15 + "AA"


class ExtractTest(unittest.TestCase):
    def test_token_among_ansi_codes(self):
        screen = ("\x1b[2J\x1b[?25l\x1b[1mPaste code here if prompted >\x1b[0m\r\n"
                  "\x1b[32m✓ Long-lived authentication token created successfully!\x1b[39m\r\n"
                  f"\x1b[1m{TOK}\x1b[22m\r\n\x1b]0;claude\x07Store this token securely.\r\n")
        self.assertEqual(ct.extract_token(screen), TOK)

    def test_wrapped_token_is_rejoined(self):
        self.assertEqual(ct.extract_token(f"token:\r\n{TOK[:50]}\r\n{TOK[50:]}\r\n"), TOK)

    def test_browser_code_is_not_a_token(self):
        self.assertEqual(ct.extract_token("dAGHflKrxK9vgQ" * 6 + "#state"), "")
        self.assertIsNone(ct.TOKEN_RX.fullmatch("dAGHflKrxK9vgQ" * 6))


class WireTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ctok-")
        mock.patch.dict(os.environ, {"WORK_DIR": self.tmp}).start()
        mock.patch.object(ct.paths, "toolkit_root", return_value=self.tmp).start()
        self.cfg = {"accounts": {"claude": {"a": {"env": {"CLAUDE_CONFIG_DIR": "/x"}}}}, "roles": {}}
        with open(os.path.join(self.tmp, "agents.json"), "w") as f:
            json.dump(self.cfg, f, indent=2)

    def tearDown(self):
        mock.patch.stopall()

    def test_save_and_wire_then_runner_reads_it(self):
        p = ct.save_token("a", TOK)
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)
        first = ct.wire_account("a")
        self.assertTrue(first["changed"] and os.path.exists(first["backup"]))
        self.assertFalse(ct.wire_account("a")["changed"])                    # idempotent
        cfg = json.load(open(os.path.join(self.tmp, "agents.json")))
        self.assertEqual(cfg["accounts"]["claude"]["a"]["env"], {"CLAUDE_CONFIG_DIR": "/x"})   # kept
        env = ct.agent_runner._account_env(cfg, "claude", "a")
        self.assertEqual(env["CLAUDE_CODE_OAUTH_TOKEN"], TOK)

    def test_unknown_account_is_refused_before_the_browser(self):
        with mock.patch.object(ct, "run_setup_token") as run:
            with self.assertRaises(SystemExit):
                ct.setup_one("nobody", test=False)
            run.assert_not_called()

    def test_setup_with_a_captured_token(self):
        with mock.patch.object(ct, "run_setup_token", return_value=TOK), \
                mock.patch.object(ct, "test_token", return_value=(True, "ok")):
            res = ct.setup_one("a", test=True)
        self.assertTrue(res["ok"])
        self.assertNotIn(TOK, json.dumps(res))                               # never printed in full
        self.assertEqual(open(ct.token_path("a")).read().strip(), TOK)

    def test_pasting_the_browser_code_is_rejected(self):
        with mock.patch.object(ct, "run_setup_token", return_value=""), \
                mock.patch.object(ct.getpass, "getpass", return_value="dAGHflKrxK9vgQ" * 6):
            res = ct.setup_one("a", test=False)
        self.assertFalse(res["ok"])
        self.assertFalse(os.path.exists(ct.token_path("a")))


if __name__ == "__main__":
    unittest.main()
