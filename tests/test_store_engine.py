#!/usr/bin/env python3
"""Durable store + checkpointed engine + quota classification (design §7, §B.4-M5).

Acceptance (agreed with Codex): quota pause then restart, expired-lease recovery, concurrent
claims, resume from the last valid checkpoint by another worker/backend; plus Codex review round 1
(2026-10-13): claim tokens, heartbeat during long steps, runner quota -> pause, LeaseLost never
escapes, checkpoint input keys + invalidation, no quota false positives, long resets kept.
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

FUTURE = time.time() + 10 ** 6


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

    def test_reclaim_with_same_owner_name_fences_the_stale_worker(self):
        """Codex R1 #1: owner names are not identity — every claim mints a new token."""
        t = self.st.add("k", "r")
        old = self.st.claim("worker-1", lease_s=1)["lease_token"]
        with mock.patch("time.time", return_value=time.time() + 5):
            new = self.st.claim("worker-1")["lease_token"]       # same name, after expiry
        self.assertNotEqual(old, new)
        for call in (lambda: self.st.checkpoint(t["id"], old, "x"),
                     lambda: self.st.complete(t["id"], old),
                     lambda: self.st.fail(t["id"], old, "boom"),
                     lambda: self.st.pause_quota(t["id"], old, FUTURE),
                     lambda: self.st.heartbeat(t["id"], old)):
            with self.assertRaises(S.LeaseLost):
                call()
        self.st.checkpoint(t["id"], new, "x")                     # the new holder is unaffected
        self.assertEqual(self.st.get(t["id"])["status"], "running")

    def test_expired_lease_rejects_checkpoint_until_renewed(self):
        t = self.st.add("k", "r")
        tok = self.st.claim("w", lease_s=1)["lease_token"]
        with mock.patch("time.time", return_value=time.time() + 5):
            with self.assertRaises(S.LeaseLost):
                self.st.checkpoint(t["id"], tok, "x")
            self.st.heartbeat(t["id"], tok)                       # nobody re-claimed: may renew
            self.st.checkpoint(t["id"], tok, "x")

    def test_quota_pause_keeps_attempts_and_waits(self):
        t = self.st.add("k", "r")
        tok = self.st.claim("w")["lease_token"]
        self.st.pause_quota(t["id"], tok, resume_at=time.time() + 3600, backend="claude", reason="usage limit")
        got = self.st.get(t["id"])
        self.assertEqual((got["status"], got["attempts"], got["backend"]), ("paused_quota", 0, "claude"))
        self.assertEqual(self.st.claim("w"), {})
        with mock.patch("time.time", return_value=time.time() + 3700):
            self.assertEqual(self.st.claim("w")["id"], t["id"])

    def test_fail_retries_then_fails(self):
        t = self.st.add("k", "r", max_attempts=2)
        self.assertEqual(self.st.fail(t["id"], self.st.claim("w")["lease_token"], "boom"), "queued")
        self.assertEqual(self.st.fail(t["id"], self.st.claim("w")["lease_token"], "boom"), "failed")
        self.assertEqual(self.st.get(t["id"])["attempts"], 2)

    def test_block_unblock_cancel_and_events(self):
        t = self.st.add("k", "r")
        self.st.block(t["id"], self.st.claim("w")["lease_token"], "needs a decision")
        self.assertEqual(self.st.claim("w"), {})
        self.assertTrue(self.st.unblock(t["id"], by="me"))
        self.assertTrue(self.st.cancel(t["id"], by="me"))
        self.assertFalse(self.st.cancel(t["id"]))
        kinds = [e["kind"] for e in self.st.events(t["id"])]
        self.assertEqual(kinds, ["added", "claimed", "blocked", "unblocked", "cancelled"])

    def test_old_schema_is_migrated(self):
        path = os.path.join(self.dir, "old.db")
        import sqlite3
        con = sqlite3.connect(path)
        con.executescript("create table tasks (id text primary key, kind text not null, ref text not null, "
                          "title text default '', status text not null, step text default '', attempts integer "
                          "default 0, max_attempts integer default 3, priority integer default 5, lease_owner text,"
                          " lease_until real, resume_at real, last_error text, backend text, data text default "
                          "'{}', created real not null, updated real not null);"
                          "create table checkpoints (id integer primary key autoincrement, task_id text not null,"
                          " step text not null, data text default '{}', sha text, ts real not null);")
        con.close()
        with S.Store(path) as st:
            t = st.add("k", "r")
            st.checkpoint(t["id"], st.claim("w")["lease_token"], "s", input_key="abc")
            self.assertEqual(st.last_checkpoint(t["id"])["input_key"], "abc")

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
        calls, limited = [], {"on": True}

        def plan(ctx):
            calls.append("plan")
            return {"plan": "A then B"}

        def implement(ctx):
            calls.append("implement")
            if limited["on"]:
                ctx.raise_if_quota("...\nClaude AI usage limit reached. Try again in 2 hours", backend="claude")
            return {"sha": "abc1234", "plan_seen": ctx.outputs["plan"]["plan"]}

        steps = [("plan", plan), ("implement", implement), ("verify", lambda c: calls.append("verify") or {})]
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

    def test_agent_runner_quota_error_pauses_without_using_an_attempt(self):
        """Codex R1 #3: the runner's quota error must reach the engine as a pause."""
        sys.path.insert(0, os.path.join(_SKILLS, "auto-dev", "tools"))
        import agent_runner as ar

        def call_agent(ctx):
            raise ar._cli_failure("CLI 'claude' exit 1", "Claude AI usage limit reached. resets in 3 hours")
        t = self.st.add("code", "R-q", max_attempts=1)
        out = E.work_once(self.st, "w", {"code": [("agent", call_agent)]})
        self.assertEqual(out["status"], "paused_quota")
        self.assertEqual(self.st.get(t["id"])["attempts"], 0)

    def test_crash_mid_step_is_recovered_after_lease_expiry(self):
        t = self.st.add("code", "R-2")
        tok = self.st.claim("crashed", lease_s=1)["lease_token"]
        self.st.checkpoint(t["id"], tok, "plan", {"plan": "x"})
        ran = []
        steps = [("plan", lambda c: ran.append("plan")), ("implement", lambda c: ran.append("impl") or {})]
        with mock.patch("time.time", return_value=time.time() + 10):
            out = E.work_once(self.st, "w2", {"code": steps})
        self.assertEqual(out["status"], "done")
        self.assertEqual(ran, ["impl"])

    def test_long_step_keeps_its_lease_via_heartbeat(self):
        """Codex R1 #2: a step longer than the lease must not become claimable meanwhile."""
        self.st.add("code", "R-long")
        seen = {}

        def slow(ctx):
            time.sleep(2.0)                                   # 4x the 0.5 s lease
            with S.Store(self.st.path) as other:
                seen["claim"] = other.claim("intruder", lease_s=0.5)
            return {"ok": True}
        out = E.work_once(self.st, "w", {"code": [("slow", slow)]}, lease_s=0.5)
        self.assertEqual(out["status"], "done")
        self.assertEqual(seen["claim"], {})

    def test_lease_lost_never_escapes_and_result_is_not_checkpointed(self):
        """Codex R1 #4: reclaimed during a step -> 'lease_lost', no exception, no checkpoint."""
        t = self.st.add("code", "R-lost")

        def stolen(ctx):
            with S.Store(self.st.path) as other:              # simulate a reclaim (new token)
                other.db.execute("update tasks set lease_token='someone-else' where id=?", (t["id"],))
            time.sleep(0.4)
            return {"would": "be lost"}
        out = E.work_once(self.st, "w", {"code": [("s", stolen)]}, lease_s=0.3)
        self.assertEqual(out["status"], "lease_lost")
        self.assertEqual(self.st.checkpoints(t["id"]), [])

    def test_changed_input_key_reruns_step_and_everything_after(self):
        """Codex R1 #5: new MR revision / repaired code -> later checks run again."""
        head = {"sha": "aaa"}
        runs = []

        def step(name):
            def fn(ctx):
                runs.append(name)
                if name == "verify" and len(runs) == 3:
                    raise E.NeedsHuman("stop after first pass")
                return {"sha": head["sha"]}
            return fn
        steps = [("fetch", step("fetch"), lambda c: head["sha"]), ("build", step("build")), ("verify", step("verify"))]
        t = self.st.add("review", "77!5")
        self.assertEqual(E.work_once(self.st, "w", {"review": steps})["status"], "blocked")
        self.st.unblock(t["id"])
        head["sha"] = "bbb"                                    # MR got a new commit while blocked
        self.assertEqual(E.work_once(self.st, "w", {"review": steps})["status"], "done")
        self.assertEqual(runs, ["fetch", "build", "verify", "fetch", "build", "verify"])
        self.assertTrue(self.st.invalidate(t["id"]) >= 0)

    def test_explicit_invalidation_forces_rerun(self):
        t = self.st.add("k", "1")
        runs = []
        steps = [("a", lambda c: runs.append("a") or {}), ("b", lambda c: runs.append("b") or {})]
        E.work_once(self.st, "w", {"k": steps})
        self.st.add("k", "1")                                  # done task: a new one is created
        self.assertEqual(self.st.invalidate(t["id"], ["b"], reason="test"), 1)
        self.assertEqual(self.st.last_checkpoint(t["id"])["step"], "a")

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
        out = E.work_once(self.st, "w", {"mystery": None})
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
                            ('{"type":"error","error":{"type":"overloaded_error"}}', "overloaded"),
                            ("You exceeded your current quota, please check your plan", "usage_limit"),
                            ("stream error: rate_limit_error", "rate_limit")]:
            r = self.c(text)
            self.assertEqual((r["kind"], r["limit"]), ("quota", limit), text)
            self.assertGreater(r["resume_at"], self.NOW)

    def test_no_false_positives_on_ordinary_failures(self):
        """Codex R1 #6."""
        for text in ("tests failed: expected HTTP 429 but got HTTP 500",
                     "Error: rate limit config is invalid",
                     "AssertionError: status 529 != 200",
                     "docs: explain how we handle 429 Too Many Requests from upstream\n" + "ok\n" * 20 + "exit 1",
                     "def login(user): ...", "SyntaxError: invalid syntax", "update the rate limiter config"):
            self.assertEqual(self.c(text)["kind"], "error", text)

    def test_reset_parsing_and_long_resets_are_kept(self):
        """Codex R1 #7: weekly limits / long Retry-After are not cut to 6 h."""
        self.assertEqual(self.c("usage limit reached|1800003600")["resume_at"], 1800003600)
        self.assertAlmostEqual(self.c("rate limited. Try again in 5 minutes")["resume_at"], self.NOW + 300)
        self.assertAlmostEqual(self.c("Error: 429 Too Many Requests retry-after: 120")["resume_at"], self.NOW + 120)
        self.assertAlmostEqual(self.c("weekly limit reached, resets in 2 days")["resume_at"], self.NOW + 2 * 86400)
        far = self.c(f"usage limit reached|{int(self.NOW + 60 * 86400)}")      # implausible -> fallback
        self.assertFalse(far["reset_parsed"])
        self.assertLessEqual(far["resume_at"], self.NOW + 6 * 3600)

    def test_auth_and_plain_errors(self):
        self.assertEqual(self.c("Error: Not logged in. Please run /login")["kind"], "auth")

    def test_user_pattern(self):
        with mock.patch.dict(os.environ, {"TOOLKIT_QUOTA_PATTERNS": r"het han muc"}):
            self.assertEqual(self.c("Lỗi: het han muc hom nay")["kind"], "quota")


class HandoffTest(_Tmp):
    def test_roundtrip_outside_repo_and_preamble(self):
        p = handoff.write("t1", {"Mục tiêu": "Export Excel", "Đã xong": ["API @abc1234"], "Bước tiếp": "UI"})
        self.assertTrue(p.startswith(self.dir))
        self.assertEqual(handoff.read("t1")["Đã xong"], "- API @abc1234")
        pre = handoff.resume_preamble("t1", last_step="implement")
        self.assertIn("TIẾP TỤC", pre)
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
