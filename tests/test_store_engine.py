#!/usr/bin/env python3
"""Durable store + checkpointed engine + quota classification (design §7, §B.4-M5).

Acceptance (agreed with Codex): quota pause then restart, expired-lease recovery,
concurrent claims, resume from the last checkpoint by a different worker/backend.
    python -m unittest discover -s tests
"""

import json
import os
import runpy
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_SKILLS = os.path.join(_ROOT, ".claude", "skills")
runpy.run_path(os.path.join(_SKILLS, "_runtime", "bootstrap.py"))["activate"]()

from agent_skill_toolkit.core import store as S  # noqa: E402
from agent_skill_toolkit.workflows import engine as E  # noqa: E402
from agent_skill_toolkit.workflows.execution import handoff, quota  # noqa: E402


class _Tmp(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="store-test-")
        self.env = mock.patch.dict(os.environ, {"TOOLKIT_STATE_DIR": self.dir,
                                                "TOOLKIT_STORE": os.path.join(self.dir, "t.db")})
        self.env.start()
        self.st = S.Store()

    def tearDown(self):
        self.st.close()
        self.env.stop()


class StoreTest(_Tmp):
    def test_add_dedupes_open_task_by_kind_and_ref(self):
        a = self.st.add("review_mr", "77!5", title="MR 5")
        b = self.st.add("review_mr", "77!5")
        self.assertFalse(a["duplicate"])
        self.assertTrue(b["duplicate"])
        self.assertEqual(a["id"], b["id"])
        self.assertNotEqual(self.st.add("code", "77!5")["id"], a["id"])

    def test_claim_priority_then_age(self):
        self.st.add("k", "low", priority=5)
        self.st.add("k", "urgent", priority=0)
        self.assertEqual(self.st.claim("w1")["ref"], "urgent")
        self.assertEqual(self.st.claim("w1")["ref"], "low")
        self.assertEqual(self.st.claim("w1"), {})

    def test_expired_lease_is_reclaimed_and_old_owner_loses_it(self):
        t = self.st.add("k", "r")
        self.st.claim("dead-worker", lease_s=1)
        with mock.patch("time.time", return_value=time.time() + 5):
            again = self.st.claim("w2")
        self.assertEqual(again["id"], t["id"])
        self.assertEqual(again["lease_owner"], "w2")
        with self.assertRaises(S.LeaseLost):
            self.st.checkpoint(t["id"], "dead-worker", "x")

    def test_quota_pause_keeps_attempts_and_waits(self):
        t = self.st.add("k", "r")
        self.st.claim("w")
        self.st.pause_quota(t["id"], "w", resume_at=time.time() + 3600, backend="claude", reason="usage limit")
        got = self.st.get(t["id"])
        self.assertEqual((got["status"], got["attempts"], got["backend"]), ("paused_quota", 0, "claude"))
        self.assertEqual(self.st.claim("w"), {})                       # not yet
        with mock.patch("time.time", return_value=time.time() + 3700):
            self.assertEqual(self.st.claim("w")["id"], t["id"])        # resumed after reset

    def test_fail_retries_then_fails(self):
        t = self.st.add("k", "r", max_attempts=2)
        self.st.claim("w")
        self.assertEqual(self.st.fail(t["id"], "w", "boom"), "queued")
        self.st.claim("w")
        self.assertEqual(self.st.fail(t["id"], "w", "boom"), "failed")
        self.assertEqual(self.st.get(t["id"])["attempts"], 2)

    def test_block_unblock_cancel_and_events(self):
        t = self.st.add("k", "r")
        self.st.claim("w")
        self.st.block(t["id"], "w", "needs a decision")
        self.assertEqual(self.st.claim("w"), {})
        self.assertTrue(self.st.unblock(t["id"], by="me"))
        self.assertTrue(self.st.cancel(t["id"], by="me"))
        self.assertFalse(self.st.cancel(t["id"]))
        kinds = [e["kind"] for e in self.st.events(t["id"])]
        self.assertEqual(kinds, ["added", "claimed", "blocked", "unblocked", "cancelled"])

    def test_concurrent_claims_across_processes_never_double_assign(self):
        for i in range(20):
            self.st.add("k", f"r{i}")
        code = ("import os,runpy,json,sys;"
                f"runpy.run_path(r'{os.path.join(_SKILLS, '_runtime', 'bootstrap.py')}')['activate']();"
                "from agent_skill_toolkit.core import store as S;"
                "st=S.Store();got=[]\n"
                "while True:\n"
                "  t=st.claim(sys.argv[1])\n"
                "  if not t: break\n"
                "  got.append(t['id'])\n"
                "print(json.dumps(got))")
        procs = [subprocess.Popen([sys.executable, "-c", code, f"w{i}"], stdout=subprocess.PIPE, text=True,
                                  env=dict(os.environ)) for i in range(4)]
        claimed = [tid for p in procs for tid in json.loads(p.communicate(timeout=60)[0])]
        self.assertEqual(len(claimed), 20)
        self.assertEqual(len(set(claimed)), 20)


class EngineTest(_Tmp):
    def test_resume_from_last_checkpoint_after_quota_by_another_worker(self):
        calls = []
        limited = {"on": True}

        def plan(ctx):
            calls.append("plan")
            return {"plan": "A then B"}

        def implement(ctx):
            calls.append("implement")
            if limited["on"]:
                ctx.raise_if_quota("...\nClaude AI usage limit reached. Try again in 2 hours", backend="claude")
            return {"sha": "abc1234", "plan_seen": ctx.outputs["plan"]["plan"]}

        def verify(ctx):
            calls.append("verify")
            return {"passed": True}

        steps = [("plan", plan), ("implement", implement), ("verify", verify)]
        t = self.st.add("code", "R-1")
        first = E.work_once(self.st, "claude-worker", {"code": steps})
        self.assertEqual(first["status"], "paused_quota")
        self.assertGreater(first["resume_at"], time.time() + 3600)
        self.assertEqual(self.st.get(t["id"])["attempts"], 0)

        limited["on"] = False
        with mock.patch("time.time", return_value=time.time() + 3 * 3600):
            second = E.work_once(self.st, "codex-worker", {"code": steps})
        self.assertEqual(second["status"], "done")
        self.assertEqual(calls, ["plan", "implement", "implement", "verify"])   # plan NOT re-run
        self.assertEqual(second["outputs"]["implement"]["plan_seen"], "A then B")
        self.assertEqual([c["sha"] for c in self.st.checkpoints(t["id"]) if c["step"] == "implement"], ["abc1234"])

    def test_crash_mid_step_is_recovered_after_lease_expiry(self):
        t = self.st.add("code", "R-2")
        self.st.claim("crashed", lease_s=1)
        self.st.checkpoint(t["id"], "crashed", "plan", {"plan": "x"})
        ran = []
        steps = [("plan", lambda c: ran.append("plan")), ("implement", lambda c: ran.append("impl") or {})]
        with mock.patch("time.time", return_value=time.time() + 10):
            out = E.work_once(self.st, "w2", {"code": steps})
        self.assertEqual(out["status"], "done")
        self.assertEqual(ran, ["impl"])

    def test_needs_human_blocks_and_errors_count_attempts(self):
        self.st.add("a", "1")
        self.st.add("b", "1", max_attempts=1)

        def ask(ctx):
            raise E.NeedsHuman("which DB?")

        def boom(ctx):
            raise RuntimeError("compile error")
        reg = {"a": [("s", ask)], "b": [("s", boom)]}
        results = {E.work_once(self.st, "w", reg)["status"], E.work_once(self.st, "w", reg)["status"]}
        self.assertEqual(results, {"blocked", "failed"})

    def test_unknown_kind_is_blocked_not_dropped(self):
        t = self.st.add("mystery", "1")
        out = E.work_once(self.st, "w", {"mystery": None} | {})
        self.assertEqual(out["status"], "blocked")
        self.assertIn("no workflow", self.st.get(t["id"])["last_error"])


class QuotaTest(unittest.TestCase):
    NOW = 1_800_000_000.0

    def c(self, text):
        return quota.classify(text, now=self.NOW)

    def test_limits(self):
        for text, limit in [("Claude AI usage limit reached|1800003600", "usage_limit"),
                            ("You've hit your limit · resets 3pm", "usage_limit"),
                            ("Error: 429 Too Many Requests", "rate_limit"),
                            ('{"type":"overloaded_error"}', "overloaded"),
                            ("You exceeded your current quota, please check your plan", "usage_limit")]:
            r = self.c(text)
            self.assertEqual((r["kind"], r["limit"]), ("quota", limit), text)
            self.assertGreater(r["resume_at"], self.NOW)

    def test_reset_parsing(self):
        self.assertEqual(self.c("usage limit reached|1800003600")["resume_at"], 1800003600)
        self.assertAlmostEqual(self.c("rate limited. Try again in 5 minutes")["resume_at"], self.NOW + 300)
        self.assertAlmostEqual(self.c("429 retry-after: 120")["resume_at"], self.NOW + 120)

    def test_auth_and_plain_errors(self):
        self.assertEqual(self.c("Error: Not logged in. Please run /login")["kind"], "auth")
        for text in ("def login(user): ...", "SyntaxError: invalid syntax", "update the rate limiter config"):
            self.assertEqual(self.c(text)["kind"], "error", text)

    def test_only_the_tail_counts(self):
        early = "agent wrote docs about '429 Too Many Requests' handling\n" + "x" * 5000 + "\nexit 1: compile failed"
        self.assertEqual(self.c(early)["kind"], "error")

    def test_user_pattern(self):
        with mock.patch.dict(os.environ, {"TOOLKIT_QUOTA_PATTERNS": r"het han muc"}):
            self.assertEqual(self.c("Lỗi: het han muc hom nay")["kind"], "quota")


class HandoffTest(_Tmp):
    def test_roundtrip_outside_repo_and_preamble(self):
        p = handoff.write("t1", {"Mục tiêu": "Export Excel", "Đã xong": ["API @abc1234"], "Bước tiếp": "UI"})
        self.assertTrue(p.startswith(self.dir))
        h = handoff.read("t1")
        self.assertEqual(h["Đã xong"], "- API @abc1234")
        pre = handoff.resume_preamble("t1", last_step="implement")
        self.assertIn("TIẾP TỤC", pre)
        self.assertIn("implement", pre)
        with self.assertRaises(ValueError):
            handoff.path("../escape")


class FacadeTest(_Tmp):
    def test_cli_runs_from_any_cwd(self):
        tool = os.path.join(_SKILLS, "dev-automation", "tools", "taskstore.py")
        env = dict(os.environ)
        r = subprocess.run([sys.executable, tool, "add", "review_mr", "77!9", "--title", "MR 9"],
                           cwd=tempfile.gettempdir(), capture_output=True, text=True, encoding="utf-8", env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        tid = json.loads(r.stdout)["id"]
        r = subprocess.run([sys.executable, tool, "summary"], cwd=self.dir, capture_output=True, text=True,
                           encoding="utf-8", env=env)
        self.assertEqual(json.loads(r.stdout)["counts"], {"queued": 1})
        r = subprocess.run([sys.executable, tool, "show", tid], capture_output=True, text=True, encoding="utf-8",
                           env=env)
        self.assertEqual(json.loads(r.stdout)["task"]["title"], "MR 9")

    def test_bootstrap_refuses_foreign_package(self):
        fake = mock.Mock(__file__=os.path.join(self.dir, "elsewhere", "agent_skill_toolkit", "__init__.py"))
        boot = runpy.run_path(os.path.join(_SKILLS, "_runtime", "bootstrap.py"))
        with mock.patch.dict(sys.modules, {"agent_skill_toolkit": fake}):
            with self.assertRaises(ImportError):
                boot["activate"]()


if __name__ == "__main__":
    unittest.main()
