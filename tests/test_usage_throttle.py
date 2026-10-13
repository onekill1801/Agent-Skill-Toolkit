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
        self.assertEqual([U.tier_of(p) for p in (None, 0, 79.9, 80, 94, 95, 99.9, 100)],
                         ["normal", "normal", "normal", "save", "save", "critical", "critical", "exhausted"])
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

    def test_malformed_policy_denies_all(self):                           # Codex F.11 #6
        self.add_mix()
        for bad in ("[]", '{"only_kinds": [], "expires_at": NaN}', '{"only_kinds": [], "expires_at": "NaN"}',
                    '{"only_kinds": "review_mr", "expires_at": 9e12}', '{"deny_kinds": [1], "expires_at": 9e12}',
                    '{"deny_kinds": [], "expires_at": "soon"}', '{"deny_kinds": []}'):
            self.st.set_setting("claim_policy", bad)
            self.assertEqual(self.st.claim_policy().get("unreadable"), True, bad)
            self.assertEqual(self.st.claim("w"), {}, bad)

    def test_exhausted_claims_nothing(self):                              # Codex F.11 #5
        self.add_mix()
        U.set_level(self.st, 100, resets_at=time.time() + 3600)
        self.assertEqual(self.refs_claimable(), [])


FAKE_CLI = "{py} -c \"import sys; sys.stdin.read(); print('ok')\""


class TurnBudgetTest(Base):
    """Budgets are enforced per AGENT TURN in agent_runner (Codex F.11 #1-#4, #7)."""

    def setUp(self):
        super().setUp()
        import agent_runner
        self.ar = agent_runner
        self.cli = FAKE_CLI.format(py=sys.executable.replace("\\", "/"))

    def turn(self, **env):
        with mock.patch.dict(os.environ, env):
            return self.ar.run_turn("p", backend="custom", cmd_template=self.cli, timeout=60)

    def test_every_turn_is_charged_to_the_task(self):
        t = self.st.add("k", "x", data={"budget": {"agent_runs": 2}})
        self.assertEqual(self.turn(TOOLKIT_TASK_ID=t["id"]), "ok")
        self.assertEqual(self.turn(TOOLKIT_TASK_ID=t["id"]), "ok")
        with self.assertRaises(self.ar.AgentBudgetError) as e:
            self.turn(TOOLKIT_TASK_ID=t["id"])
        self.assertEqual(e.exception.classification["kind"], "budget")
        used = U.agent_usage(self.st, t["id"])
        self.assertEqual(used["runs"], 2)
        self.assertLess(used["seconds"], 60)                  # settled to the real duration

    def test_seconds_budget_shortens_and_then_refuses(self):
        t = self.st.add("k", "x", data={"budget": {"agent_seconds": 30}})
        res = U.reserve(self.st, t["id"], "s", "claude", "sonnet", 600)
        self.assertEqual(res["timeout"], 30)                   # only what is left
        with self.assertRaises(U.BudgetSpent):                 # unsettled = worst case still counts
            U.reserve(self.st, t["id"], "s", "claude", "sonnet", 600)

    def test_too_little_time_left_is_refused_not_squeezed(self):            # Codex F.11 R2
        t = self.st.add("k", "x", data={"budget": {"agent_seconds": 25}})
        res = U.reserve(self.st, t["id"], "s", "claude", "", 600)
        U.settle(self.st, res["row"], 20)                      # 5 s left < MIN_TURN_S
        with self.assertRaises(U.BudgetSpent):
            U.reserve(self.st, t["id"], "s", "claude", "", 600)
        U.settle(self.st, res["row"], 40)                      # an overrun is recorded as it was
        self.assertEqual(U.agent_usage(self.st, t["id"])["seconds"], 40)

    def test_fractional_timeout_is_kept(self):
        seen = []
        t = self.st.add("k", "x", data={"budget": {"agent_seconds": 12.5}})
        with mock.patch.dict(os.environ, {"TOOLKIT_TASK_ID": t["id"]}):
            with mock.patch.object(self.ar, "_run_cli", side_effect=lambda *a, **k: seen.append(a[6]) or "ok"):
                self.ar.run_turn("p", backend="claude", model="sonnet", timeout=600)
        self.assertEqual(seen, [12.5])

    def test_api_turn_is_cancelled_at_its_deadline(self):                  # Codex F.11 R3
        """A server that drips its answer slowly: the request is aborted (socket closed) at the deadline,
        the call returns then, and the server sees the connection drop."""
        import http.server
        import threading
        dropped = threading.Event()

        class Drip(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                Drip.posts = getattr(Drip, "posts", 0) + 1
                self.rfile.read(int(self.headers["content-length"]))
                self.send_response(200)
                self.send_header("content-length", "100")
                self.end_headers()
                try:
                    for _ in range(100):
                        self.wfile.write(b" ")
                        self.wfile.flush()
                        time.sleep(0.1)          # each byte well within a socket timeout
                except OSError:
                    dropped.set()

            def log_message(self, *a):
                pass
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Drip)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            t0 = time.monotonic()
            with self.assertRaises(self.ar.AgentRunError) as e:
                self.ar._post_with_deadline(f"http://127.0.0.1:{srv.server_address[1]}/v1/messages", b"{}",
                                            {"content-type": "application/json"}, 2.0)
            took = time.monotonic() - t0
            self.assertIn("deadline", str(e.exception))
            self.assertLess(took, 4)
            self.assertTrue(dropped.wait(5))
            # Codex F.11 R4: a deadline that expires before the request goes out -> nothing is sent
            got_before = Drip.posts if hasattr(Drip, "posts") else 0
            with self.assertRaises(self.ar.AgentRunError):
                self.ar._post_with_deadline(f"http://127.0.0.1:{srv.server_address[1]}/v1/messages", b"{}",
                                            {"content-type": "application/json"}, 0.02)
            time.sleep(1.0)
            self.assertEqual(getattr(Drip, "posts", 0), got_before)
        finally:
            srv.shutdown()

    def test_api_worker_round_trip(self):
        import http.server
        import threading

        class Ok(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers["content-length"])
                body = self.rfile.read(n)
                out = json.dumps({"echo": body.decode(), "key": self.headers.get("x-api-key")}).encode()
                self.send_response(200)
                self.send_header("content-length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def log_message(self, *a):
                pass
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Ok)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            status, headers, body = self.ar._post_with_deadline(
                f"http://127.0.0.1:{srv.server_address[1]}/v1/messages", b'{"q": 1}', {"x-api-key": "k1"}, 20)
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body), {"echo": '{"q": 1}', "key": "k1"})
        finally:
            srv.shutdown()

    def test_subprocess_cannot_outlive_its_timeout(self):                   # Codex F.11 R3
        import subprocess
        from agent_skill_toolkit.core import proc
        t0 = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            proc.run([sys.executable, "-c", "import time; time.sleep(0.35); print('done')"], timeout=0.15, poll=0.5)
        self.assertLess(time.monotonic() - t0, 0.35)

    def test_role_routing_keeps_the_quota_type(self):                       # Codex F.11 R2
        from agent_skill_toolkit.workflows.execution import quota
        self.st.set_setting("daily_agent_runs", "1")
        U.reserve(self.st, "", "", "claude", "", 60)
        with open(os.path.join(self.tmp, "agents.json"), "w") as f:
            json.dump({"roles": {"r": ["claude:sonnet", "codex"]}}, f)
        with self.assertRaises(quota.QuotaExceeded):
            self.ar.run_role("p", "r")

    def test_daily_cap_covers_every_caller(self):
        self.st.set_setting("daily_agent_runs", "1")
        self.assertEqual(self.turn(), "ok")                    # a direct call, no task
        with self.assertRaises(self.ar.AgentQuotaError) as e:
            self.turn(TOOLKIT_TASK_ID="whatever")
        self.assertEqual(e.exception.classification["resume_at"], U.next_local_midnight() + 60)

    def test_exhausted_refuses_turns(self):
        U.set_level(self.st, 100)
        with self.assertRaises(self.ar.AgentQuotaError):
            self.turn()

    def test_concurrent_turns_never_exceed_the_cap(self):
        import threading
        self.st.set_setting("daily_agent_runs", "3")
        ok, refused = [], []

        def go():
            with S.Store() as st:
                try:
                    U.reserve(st, "", "", "claude", "", 60)
                    ok.append(1)
                except U.BudgetSpent:
                    refused.append(1)
        ts = [threading.Thread(target=go) for _ in range(8)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual((len(ok), len(refused)), (3, 5))

    def test_engine_steps_without_agents_are_never_blocked(self):          # Codex F.11 #4
        t = self.st.add("k", "x", data={"budget": {"agent_runs": 0}})
        res = E.run(self.st, self.st.claim("w"), [("build", lambda ctx: {}), ("post", lambda ctx: {})])
        self.assertEqual(res["status"], "done")
        self.assertEqual(self.st.get(t["id"])["status"], "done")

    def test_engine_passes_the_task_to_subprocesses(self):
        t = self.st.add("k", "x")
        seen = {}

        def step(ctx):
            p = ctx.run([sys.executable, "-c", "import os; print(os.environ['TOOLKIT_TASK_ID'], os.environ['TOOLKIT_STEP'])"])
            seen["out"] = p.stdout.split()
            return {}
        E.run(self.st, self.st.claim("w"), [("probe", step)])
        self.assertEqual(seen["out"], [t["id"], "probe"])

    def test_model_follows_the_tier_on_every_path(self):                   # Codex F.11 #7
        seen = []
        with mock.patch.object(self.ar, "_run_cli", side_effect=lambda pr, sy, be, m, *a, **k: seen.append(m) or "ok"):
            with mock.patch.dict(os.environ, {"TOOLKIT_USAGE_TIER": "save"}):
                self.ar.run_turn("p", backend="claude", model="opus")              # direct call
                self.ar.run_turn("p", backend="claude", model="opus", role="log_read")
                with open(os.path.join(self.tmp, "agents.json"), "w") as f:
                    json.dump({"roles": {"implement": ["claude:opus@work"], "x": ["claude"]}}, f)
                out = self.ar.run_role("p", "implement")
                self.ar.run_role("p", "x")                                       # bare candidate: default model
            self.ar.run_turn("p", backend="claude", model="opus")                  # normal tier
        self.assertEqual(seen, ["sonnet", "haiku", "sonnet", "sonnet", "opus"])
        self.assertIn("opus -> sonnet", json.dumps(out["tried"]))

    def test_dst_safe_midnight(self):
        m = U.next_local_midnight()
        lt = time.localtime(m)
        self.assertEqual((lt.tm_hour, lt.tm_min), (0, 0))
        self.assertGreater(m, time.time())


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
