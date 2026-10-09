#!/usr/bin/env python3
"""vpn.py — no real VPN: subprocess/probes are faked."""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".claude", "skills",
                                                "dev-automation", "tools")))
import vpn  # noqa: E402

CFG = {"host": "vpn.x", "port": "443", "user": "u", "realm": "", "trusted_cert": "ab" * 32, "check": ["h:1"]}


class VpnTest(unittest.TestCase):
    def test_already_up_is_noop(self):
        with mock.patch.object(vpn, "cfg", return_value=CFG), \
             mock.patch.object(vpn, "internal_up", return_value={"reachable": True, "probes": {}}):
            self.assertTrue(vpn.up()["already"])

    def test_password_not_in_argv_and_temp_config_removed(self):
        seen = {}

        class P:
            pid = 42

            def poll(self):
                return None

        def popen(argv, **kw):
            seen["argv"] = argv
            conf = argv[argv.index("-c") + 1]
            seen["conf"] = conf
            seen["content"] = open(conf).read()
            seen["mode"] = oct(os.stat(conf).st_mode & 0o777)
            return P()

        ups = iter([{"reachable": True, "probes": {}}])
        with mock.patch.object(vpn, "_openfortivpn", return_value="/bin/openfortivpn"), \
             mock.patch.object(vpn, "load_password", return_value="s3cret"), \
             mock.patch.object(vpn.subprocess, "Popen", side_effect=popen), \
             mock.patch.object(vpn, "internal_up", side_effect=lambda c=None: next(ups)), \
             mock.patch.object(vpn.time, "sleep"):
            out = vpn._up_openfortivpn(CFG, wait=5)
        self.assertTrue(out["connected"])
        self.assertNotIn("s3cret", " ".join(seen["argv"]))
        self.assertIn("password = s3cret", seen["content"])
        self.assertIn("trusted-cert = " + "ab" * 32, seen["content"])
        self.assertEqual(seen["mode"], "0o600")
        self.assertFalse(os.path.exists(seen["conf"]))

    def test_set_password_needs_tty(self):
        with mock.patch.object(sys.stdin, "isatty", return_value=False):
            self.assertEqual(vpn.main(["set-password"]), 1)


if __name__ == "__main__":
    unittest.main()
