"""Direct Claude usage reading (integrations/claude_usage.py, core/tls.py, accounts merge) — checklist F.15.
No network: fetch / TLS are mocked."""

import io
import json
import os
import runpy
import sys
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
runpy.run_path(str(ROOT / ".claude" / "skills" / "_runtime" / "bootstrap.py"))["activate"]()
from agent_skill_toolkit.core import tls  # noqa: E402
from agent_skill_toolkit.integrations import claude_usage as CU  # noqa: E402
from agent_skill_toolkit.workflows.execution import accounts as A  # noqa: E402

sys.path.insert(0, str(ROOT / ".claude" / "skills" / "auto-dev" / "tools"))
import accounts as ACC  # noqa: E402

NOW = 2_000_000_000.0
TOKEN = "sk-ant-oat01-" + "x" * 40
BODY = {"five_hour": {"utilization": 5.0, "resets_at": "2033-05-18T05:00:00+00:00"},
        "seven_day": {"utilization": 14.0, "resets_at": "2033-05-20T00:00:00Z"}, "extra_usage": {"is_enabled": False}}


def creds(d, token=TOKEN, exp_s=NOW + 3600):
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, ".credentials.json"), "w", encoding="utf-8") as f:
        json.dump({"claudeAiOauth": {"accessToken": token, "expiresAt": exp_s * 1000, "refreshToken": "r"}}, f)


_STATE = mock.patch.dict(os.environ, {"TOOLKIT_STATE_DIR": tempfile.mkdtemp(prefix="usage-test-")})


def setUpModule():
    _STATE.start()          # read_api_usage writes the usage cache (F.22): never into the real temp/


def tearDownModule():
    _STATE.stop()


class UsageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        e = mock.patch.dict(os.environ, {"CLAUDE_USAGE_URL": ""})
        e.start()
        self.addCleanup(e.stop)

    def test_config_dir_rules(self):
        self.assertEqual(CU.config_dir({"CLAUDE_CONFIG_DIR": "~/a"}), os.path.expanduser("~/a"))
        self.assertEqual(CU.config_dir({"HOME": "/h", "USERPROFILE": "/u"}), os.path.join("/u", ".claude"))
        self.assertEqual(CU.config_dir({}), os.path.join(os.path.expanduser("~"), ".claude"))

    def test_read_access(self):
        self.assertEqual(CU.read_access(self.tmp, NOW), {"error": "missing"})
        creds(self.tmp, exp_s=NOW - 60)
        self.assertEqual(CU.read_access(self.tmp, NOW)["error"], "expired")
        self.assertNotIn("token", CU.read_access(self.tmp, NOW))           # never hands out an expired token
        creds(self.tmp)
        self.assertEqual(CU.read_access(self.tmp, NOW)["token"], TOKEN)
        creds(self.tmp, token="")
        self.assertEqual(CU.read_access(self.tmp, NOW), {"error": "no_token"})
        with open(os.path.join(self.tmp, ".credentials.json"), "w") as f:
            f.write("{broken")
        self.assertEqual(CU.read_access(self.tmp, NOW), {"error": "unreadable"})

    def test_parse(self):
        u = CU.parse(BODY, NOW)
        self.assertEqual((u["session"]["left"], u["weekly"]["left"], u["age_min"]), (95.0, 86.0, 0))
        self.assertGreater(u["weekly"]["resets_at"], NOW)
        self.assertIsNone(CU.parse({"five_hour": {"utilization": 150}, "seven_day": BODY["seven_day"]})["session"])
        with self.assertRaises(ValueError):
            CU.parse({"five_hour": None, "seven_day": {"utilization": True}})

    def test_usage_for_never_leaks_the_token(self):
        creds(self.tmp)
        envd = {"CLAUDE_CONFIG_DIR": self.tmp}
        with mock.patch.object(CU, "fetch", return_value=BODY) as f:
            ok = CU.usage_for(envd, now=NOW)
        self.assertEqual(f.call_args[0][0], TOKEN)
        self.assertEqual(ok["usage"]["session"]["left"], 95.0)
        cases = [(urllib.error.HTTPError("u", 401, f"bad {TOKEN}", {}, io.BytesIO(TOKEN.encode())), "http_401"),
                 (urllib.error.URLError("[SSL: CERTIFICATE_VERIFY_FAILED] x"), "tls"),
                 (urllib.error.URLError("refused"), "network"), (TimeoutError("t"), "network"),
                 (ValueError("json"), "bad_response")]
        for exc, want in cases:
            with mock.patch.object(CU, "fetch", side_effect=exc):
                res = CU.usage_for(envd, now=NOW)
            self.assertEqual(res, {"error": want})
            self.assertNotIn(TOKEN, json.dumps(res))

    def test_only_plain_https_urls(self):
        creds(self.tmp)
        for bad in ("http://api.anthropic.com/x", "https://u:p@h/x", "file:///etc/passwd"):
            with mock.patch.dict(os.environ, {"CLAUDE_USAGE_URL": bad}), \
                    mock.patch.object(CU, "fetch", side_effect=AssertionError("must not call")):
                self.assertEqual(CU.usage_for({"CLAUDE_CONFIG_DIR": self.tmp}, now=NOW), {"error": "bad_url"})

    def test_redirects_are_refused(self):
        self.assertIsNone(CU._NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil/"))

    def test_fetch_uses_the_verifying_context_and_sends_the_bearer(self):
        seen = {}

        class Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def opener(*handlers):
            seen["handlers"] = handlers
            o = mock.Mock()
            o.open.side_effect = lambda req, timeout: (seen.update(req=req, timeout=timeout),
                                                       Resp(json.dumps(BODY).encode()))[1]
            return o
        ctx = mock.Mock()
        with mock.patch.object(CU.tls, "context", return_value=ctx), \
                mock.patch.object(CU.urllib.request, "build_opener", side_effect=opener), \
                mock.patch.object(CU.urllib.request, "HTTPSHandler") as https:
            self.assertEqual(CU.fetch(TOKEN, timeout=3), BODY)
        self.assertIs(https.call_args.kwargs["context"], ctx)
        self.assertTrue(any(isinstance(h, CU._NoRedirect) for h in seen["handlers"]))
        self.assertEqual(seen["req"].get_header("Authorization"), "Bearer " + TOKEN)
        self.assertEqual(seen["req"].full_url, CU.DEFAULT_URL)
        self.assertEqual(seen["timeout"], 3)


class TlsTest(unittest.TestCase):
    def setUp(self):
        tls._ctx = None
        self.addCleanup(setattr, tls, "_ctx", None)

    def test_empty_store_on_windows_loads_the_windows_roots(self):
        ctx = mock.Mock()
        ctx.cert_store_stats.return_value = {"x509_ca": 0}
        pem = "-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n"
        with mock.patch.object(tls.ssl, "create_default_context", return_value=ctx), \
                mock.patch.object(tls.sys, "platform", "win32"), \
                mock.patch.object(tls, "windows_roots_pem", return_value=pem):
            self.assertIs(tls.context(), ctx)
            self.assertIs(tls.context(), ctx)                              # built once
        ctx.load_verify_locations.assert_called_with(cadata=pem)
        self.assertNotIn("verify_mode", ctx.__dict__)                     # never turned off
        self.assertNotIn("check_hostname", ctx.__dict__)

    def test_a_store_with_roots_is_left_alone(self):
        ctx = mock.Mock()
        ctx.cert_store_stats.return_value = {"x509_ca": 120}
        with mock.patch.object(tls.ssl, "create_default_context", return_value=ctx), \
                mock.patch.object(tls, "windows_roots_pem", side_effect=AssertionError("not needed")):
            tls.context()
        ctx.load_verify_locations.assert_not_called()

    def test_one_bad_certificate_does_not_drop_the_others(self):
        ctx = mock.Mock()
        calls = []

        def load(cadata):
            calls.append(cadata)
            if "BAD" in cadata:
                raise tls.ssl.SSLError("bad")
        ctx.load_verify_locations.side_effect = load
        pem = ("-----BEGIN CERTIFICATE-----\nBAD\n-----END CERTIFICATE-----\n"
               "-----BEGIN CERTIFICATE-----\nGOOD\n-----END CERTIFICATE-----\n")
        tls._load_pem(ctx, pem)
        self.assertTrue(any("GOOD" in c and "BAD" not in c for c in calls))


class MergeTest(unittest.TestCase):
    CFG = {"accounts": {"claude": {"a": {"orca_id": "id-a"}, "b": {"orca_id": "id-b"}}}}
    ORCA = {"claude": {"activeAccountId": "id-a", "accounts": [{"id": "id-a"}, {"id": "id-b"}]},
            "rateLimits": {"inactiveClaudeAccounts": [{"accountId": "id-b", "rateLimits": {
                "status": "ok", "updatedAt": (NOW - 3000) * 1000,
                "session": {"usedPercent": 50, "resetsAt": (NOW + 600) * 1000},
                "weekly": {"usedPercent": 10, "resetsAt": (NOW + 9000) * 1000}}}]}}

    def test_api_wins_and_errors_keep_orca(self):
        api = {"claude@a": {"usage": CU.parse(BODY, NOW), "at": NOW - 120}, "claude@b": {"error": "expired"}}
        rows = {r["account"]: r for r in A.report(self.CFG, self.ORCA, [], [], NOW, api=api)["accounts"]}
        self.assertEqual((rows["claude@a"]["usage_source"], rows["claude@a"]["usage"]["age_min"]), ("api", 2))
        self.assertEqual(rows["claude@b"]["usage_source"], "cached")
        self.assertEqual(rows["claude@b"]["usage"]["session"]["left"], 50)
        self.assertIn("hết hạn", rows["claude@b"]["api_error"])

    def test_cached_api_reading_after_a_reset_is_unknown(self):
        u = CU.parse(BODY, NOW)
        u["session"]["resets_at"] = NOW - 1
        rows = A.report(self.CFG, None, [], [], NOW, api={"claude@a": {"usage": u, "at": NOW - 900}})["accounts"]
        a = next(r for r in rows if r["account"] == "claude@a")
        self.assertTrue(a["usage"]["session"]["reset_passed"])
        self.assertEqual(a["usage"]["weekly"]["left"], 86.0)


class FacadeTest(unittest.TestCase):
    def setUp(self):
        ACC._api_cache.clear()
        ACC._inflight.clear()
        self.addCleanup(ACC._api_cache.clear)
        self.addCleanup(ACC._inflight.clear)

    def test_parallel_fetch_is_cached_per_account(self):
        cfg = {"accounts": {"claude": {"a": {"env": {"CLAUDE_CONFIG_DIR": "/a"}}, "b": {}}, "codex": {"m": {}}}}
        calls = []

        def usage_for(envd, timeout):
            calls.append(envd.get("CLAUDE_CONFIG_DIR"))
            time.sleep(0.2)
            return {"error": "expired"}
        with mock.patch.object(ACC.CU, "usage_for", side_effect=usage_for):
            t0 = time.time()
            out = ACC.read_api_usage(cfg, now=NOW)
            self.assertLess(time.time() - t0, 0.39)                       # both at once, not 0.4 s
            ACC.read_api_usage(cfg, now=NOW + 60)                          # cached
            ACC.read_api_usage(cfg, now=NOW + ACC.API_CACHE_S + 1)         # expired -> asked again
        self.assertEqual(set(out), {"claude@a", "claude@b"})               # codex is not asked
        self.assertEqual(sorted(calls, key=str), sorted(["/a", None, "/a", None], key=str))

    def test_concurrent_callers_share_one_request(self):
        import threading
        cfg = {"accounts": {"claude": {"a": {"env": {"CLAUDE_CONFIG_DIR": "/a"}}}}}
        calls = []

        def usage_for(envd, timeout):
            calls.append(1)
            time.sleep(0.3)
            return {"error": "http_429"}
        results = []
        with mock.patch.object(ACC.CU, "usage_for", side_effect=usage_for):
            ts = [threading.Thread(target=lambda: results.append(ACC.read_api_usage(cfg, now=NOW))) for _ in range(4)]
            [t.start() for t in ts]
            [t.join(5) for t in ts]
        self.assertEqual(len(calls), 1)
        self.assertTrue(all(r["claude@a"] == {"error": "http_429"} for r in results))

    def test_repointed_account_is_a_new_identity(self):
        calls = []
        with mock.patch.object(ACC.CU, "usage_for", side_effect=lambda e, t: calls.append(e["CLAUDE_CONFIG_DIR"])
                               or {"usage": {"session": None, "weekly": None}, "at": NOW}):
            ACC.read_api_usage({"accounts": {"claude": {"a": {"env": {"CLAUDE_CONFIG_DIR": "/A"}}}}}, now=NOW)
            ACC.read_api_usage({"accounts": {"claude": {"a": {"env": {"CLAUDE_CONFIG_DIR": "/B"}}}}}, now=NOW + 1)
        self.assertEqual(calls, ["/A", "/B"])                             # not A's cached numbers for B

    def test_a_stuck_request_does_not_block_the_caller(self):
        import threading
        gate = threading.Event()
        cfg = {"accounts": {"claude": {"a": {"env": {"CLAUDE_CONFIG_DIR": "/a"}}}}}

        def usage_for(envd, timeout):
            gate.wait(5)
            return {"error": "expired"}
        with mock.patch.object(ACC.CU, "usage_for", side_effect=usage_for):
            t0 = time.time()
            self.assertEqual(ACC.read_api_usage(cfg, now=NOW, wait_s=0.2)["claude@a"], {"error": "network"})
            self.assertLess(time.time() - t0, 1.0)
            gate.set()
            for _ in range(50):
                if not ACC._inflight:
                    break
                time.sleep(0.05)
            self.assertEqual(ACC.read_api_usage(cfg, now=NOW + 1)["claude@a"], {"error": "expired"})   # landed

    def test_an_abandoned_request_lets_a_new_one_start(self):
        import threading
        gate = threading.Event()
        cfg = {"accounts": {"claude": {"a": {"env": {"CLAUDE_CONFIG_DIR": "/a"}}}}}
        calls = []

        def usage_for(envd, timeout):
            calls.append(1)
            if len(calls) == 1:
                gate.wait(5)                                       # the first request hangs
            return {"error": "expired"}
        with mock.patch.object(ACC.CU, "usage_for", side_effect=usage_for), mock.patch.object(ACC, "STUCK_S", 0.1):
            ACC.read_api_usage(cfg, now=NOW, wait_s=0.05)
            time.sleep(0.2)
            out = ACC.read_api_usage(cfg, now=NOW + 1, wait_s=2)     # stuck one abandoned -> a new request
            gate.set()
        self.assertEqual((len(calls), out["claude@a"]), (2, {"error": "expired"}))

    def test_a_late_abandoned_result_never_overwrites_the_newer_one(self):
        import threading
        gate = threading.Event()
        cfg = {"accounts": {"claude": {"a": {"env": {"CLAUDE_CONFIG_DIR": "/a"}}}}}
        calls = []

        def usage_for(envd, timeout):
            calls.append(1)
            if len(calls) == 1:                                    # A: hangs, finishes LAST, says "network"
                gate.wait(5)
                return {"error": "network"}
            return {"error": "http_429"}                           # B: replacement, finishes first
        with mock.patch.object(ACC.CU, "usage_for", side_effect=usage_for), mock.patch.object(ACC, "STUCK_S", 0.1):
            ACC.read_api_usage(cfg, now=NOW, wait_s=0.05)
            time.sleep(0.2)
            self.assertEqual(ACC.read_api_usage(cfg, now=NOW + 1, wait_s=2)["claude@a"], {"error": "http_429"})
            gate.set()
            time.sleep(0.2)                                        # A completes now
            self.assertEqual(ACC.read_api_usage(cfg, now=NOW + 2)["claude@a"], {"error": "http_429"})
        self.assertEqual(len(calls), 2)                            # B's cached answer was used, not overwritten

    def test_orca_only_skips_the_api(self):
        with mock.patch.object(ACC.orca_adapter, "account_list", return_value={}),                 mock.patch.object(ACC, "read_api_usage", side_effect=AssertionError("no api")):
            ACC.gather(api=False)


class ShapeAndDeadlineTest(unittest.TestCase):
    def test_bad_shapes_never_raise(self):
        d = tempfile.mkdtemp()
        for doc in ([], {"claudeAiOauth": []}, {"claudeAiOauth": "x"}, "str"):
            with open(os.path.join(d, ".credentials.json"), "w", encoding="utf-8") as f:
                json.dump(doc, f)
            self.assertEqual(CU.read_access(d, NOW), {"error": "unreadable"})
        creds(d)
        for body in ([1, 2], "x", None):
            with mock.patch.object(CU, "fetch", return_value=body):
                self.assertEqual(CU.usage_for({"CLAUDE_CONFIG_DIR": d}, now=NOW), {"error": "bad_response"})

    def _fetch_with(self, chunks, timeout=3):
        class Resp:
            def __init__(self):
                self.it = iter(chunks)

            def read(self, n):
                b = next(self.it, b"")
                if callable(b):
                    return b()
                return b

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        op = mock.Mock()
        op.open.return_value = Resp()
        with mock.patch.object(CU.tls, "context", return_value=mock.Mock()),                 mock.patch.object(CU.urllib.request, "build_opener", return_value=op),                 mock.patch.object(CU.urllib.request, "HTTPSHandler"):
            return CU.fetch(TOKEN, timeout=timeout)

    def test_body_is_bounded(self):
        with self.assertRaises(ValueError):
            self._fetch_with([b"x" * 8192] * 40)

    def test_real_http_response_trickling_hits_the_deadline(self):
        """A real buffered http.client.HTTPResponse fed one byte every 50 ms (Codex usage R2 #1)."""
        import http.client

        class Trickle(io.RawIOBase):
            def __init__(self, head, body):
                self.head, self.body = head, body

            def readable(self):
                return True

            def readinto(self, buf):
                if self.head:
                    n = min(len(buf), len(self.head))
                    buf[:n], self.head = self.head[:n], self.head[n:]
                    return n
                if not self.body:
                    return 0
                time.sleep(0.05)
                buf[:1], self.body = self.body[:1], self.body[1:]
                return 1

        class Sock:
            def makefile(self, mode):
                return io.BufferedReader(Trickle(b"HTTP/1.1 200 OK\r\nContent-Length: 400\r\n\r\n", b"x" * 400))
        r = http.client.HTTPResponse(Sock())
        r.begin()
        t0 = time.monotonic()
        with self.assertRaises(TimeoutError):
            CU.read_body(r, time.monotonic() + 0.2)
        self.assertLess(time.monotonic() - t0, 0.6)

    def test_trickling_server_hits_the_overall_deadline(self):
        def slow():
            time.sleep(0.15)
            return b" "
        t0 = time.time()
        with self.assertRaises(TimeoutError):
            self._fetch_with([slow] * 100, timeout=0.4)
        self.assertLess(time.time() - t0, 1.5)


if __name__ == "__main__":
    unittest.main()
