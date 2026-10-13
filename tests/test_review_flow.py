#!/usr/bin/env python3
"""review_flow (checklist F.4b): MR review as a durable workflow.

GitLab and the workspace are faked; the AGENT is a real subprocess (a fake agent_runner script)
so the ctx.run path — stdin prompt, exit codes, quota JSON — is exercised for real.
    python -m unittest discover -s tests
"""

import json
import os
import sys
import tempfile
import textwrap
import time
import unittest
from unittest import mock

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(_ROOT, ".claude", "skills", "mr-review", "tools"))

import review_common as rc  # noqa: E402
import review_flow as rf  # noqa: E402
import review_post as rp  # noqa: E402
from agent_skill_toolkit.core import store as S  # noqa: E402  (bootstrapped by review_flow)

H1, H2 = "a" * 40, "b" * 40

FAKE_AGENT = textwrap.dedent('''
    import json, os, re, sys
    prompt = sys.stdin.read()
    mode = os.environ.get("FAKE_AGENT_MODE", "ok")
    if mode == "quota":
        print(json.dumps({"error": True, "kind": "quota", "resume_at": %(future)s,
                          "message": "Claude AI usage limit reached"}))
        sys.exit(1)
    if mode == "crash":
        print(json.dumps({"error": True, "kind": "error", "message": "boom"}))
        sys.exit(1)
    wt = re.search(r"worktree: (.+)", prompt).group(1).strip()
    with open(os.environ["FAKE_AGENT_LOG"], "a") as f:
        f.write("ran\\n")
    review = {"head_sha": os.environ["FAKE_HEAD"], "verdict": "COMMENT", "summary": "Looks fine",
              "findings": [{"severity": "minor", "title": "nit", "file": "a.py", "line": 3}]}
    with open(wt + ".review.json", "w", encoding="utf-8") as f:
        json.dump(review, f)
    if mode == "tamper":   # prompt-injected agent forges a VALID human approval (matching payload hash)
        sys.path.insert(0, os.environ["FAKE_TOOLS"])
        import review_common as rc, review_post as rp
        jid = re.search(r"Process review job (\\S+)", prompt).group(1)
        job = rc.load_job(jid)
        job["approval"] = {"via": "terminal", "decision": "approved", "by": "attacker",
                           "payload_hash": rp.payload_hash(job, rp.load_review(job))}
        rc.save_job(job, sync=False)
    print("done")
''')


class ReviewFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="flow-test-")
        self.head = {"sha": H1, "state": "opened"}
        self.agent = os.path.join(self.tmp, "fake_agent.py")
        with open(self.agent, "w") as f:
            f.write(FAKE_AGENT % {"future": time.time() + 7200})
        self.log = os.path.join(self.tmp, "agent.log")
        env = {"REVIEW_STATE_DIR": os.path.join(self.tmp, "rq"), "TOOLKIT_STATE_DIR": self.tmp,
               "TOOLKIT_STORE": os.path.join(self.tmp, "t.db"), "WORK_DIR": self.tmp, "REVIEW_ORCA": "0", "REVIEW_CODEGRAPH": "0",
               "REVIEW_AUTO_POST": "auto", "REVIEW_AUTO_POST_PROJECTS": "grp/proj",
               "GITLAB_URL": "https://gitlab.example.vn", "FAKE_AGENT_LOG": self.log, "FAKE_AGENT_MODE": "ok",
               "FAKE_TOOLS": os.path.join(_ROOT, ".claude", "skills", "mr-review", "tools")}
        mock.patch.dict(os.environ, env).start()
        mock.patch.object(rf, "AGENT_RUNNER", self.agent).start()
        mock.patch.object(rc, "resolve_project_id", return_value=("77", "grp/proj")).start()
        mock.patch.object(rc, "gitlab_mr", side_effect=self.fake_mr).start()
        mock.patch.object(rc, "registry_by_gitlab_id", return_value=("proj", {})).start()
        self.posted = []
        mock.patch.object(rp, "post", side_effect=self.fake_post).start()
        import review_ws
        mock.patch.object(review_ws, "prepare", side_effect=self.fake_prepare).start()

    def tearDown(self):
        mock.patch.stopall()

    # ------------------------------------------------------------ fakes
    def fake_mr(self, pid, iid):
        h = self.head["sha"]
        return {"iid": iid, "state": self.head["state"], "title": f"MR {iid}", "sha": h,
                "web_url": f"https://gitlab.example.vn/grp/proj/-/merge_requests/{iid}",
                "source_branch": "feat/a", "target_branch": "develop", "author": {"username": "dev"},
                "diff_refs": {"base_sha": "c" * 40, "start_sha": "c" * 40, "head_sha": h}}

    def fake_prepare(self, jid, with_merge=False):
        wt = os.path.join(self.tmp, "wt", jid.replace("-", "_") + "_" + self.head["sha"][:6])
        os.makedirs(wt, exist_ok=True)
        job = rc.load_job(jid)
        job.update(worktree=wt, diff_refs=self.fake_mr("77", job["iid"])["diff_refs"], head_sha=self.head["sha"])
        rc.save_job(job)
        os.environ["FAKE_HEAD"] = self.head["sha"]
        return {"worktree": wt, "ws_path": wt + ".ws.json", "codegraph": {"status": "disabled"}}

    def fake_post(self, jid, dry_run=False, notify=True):
        job = rc.load_job(jid)
        if job["diff_refs"]["head_sha"] != self.head["sha"]:
            return {"job_id": jid, "stale": True, "posted": False}
        job["status"] = "posted"
        rc.save_job(job)
        self.posted.append(jid)
        return {"job_id": jid, "posted": True, "verdict": "COMMENT", "inline": 1}

    def runs(self):
        return open(self.log).read().count("ran") if os.path.exists(self.log) else 0

    def work(self):
        return rf.work("w-test", once=True, interval=0)["ran"]

    def task(self):
        with S.Store() as st:
            return st.list(kind=rf.KIND)[0]

    # ------------------------------------------------------------ tests
    def test_happy_path_auto_posts(self):
        out = rf.enqueue("proj", 5, source="fchat")
        self.assertFalse(out["task_duplicate"])
        self.assertEqual(rf.enqueue("proj", 5)["task_id"], out["task_id"])        # dedup
        ran = self.work()
        self.assertEqual(ran[-1]["status"], "done")
        self.assertEqual(self.posted, ["77-5"])
        self.assertEqual(self.runs(), 1)

    # ------------------------------------------------------------ F.5 intake gate
    def test_enqueue_records_the_intake_source_and_trust(self):
        out = rf.enqueue("proj", 30, source="fchat", requester_id="U7")
        with S.Store() as st:
            intake = st.get(out["task_id"])["data"]["intake"]
        self.assertEqual((intake["source"], intake["trust"], intake["requester_id"]), ("fchat", "low", "U7"))

    def test_switched_off_source_cannot_enqueue(self):
        with open(os.path.join(self.tmp, "policy.json"), "w") as f:
            json.dump({"intake": {"fchat": {"on": False}}}, f)
        with self.assertRaises(PermissionError):
            rf.enqueue("proj", 31, source="fchat")
        self.assertFalse(rc.load_job("77-31"))                  # refused before the job file
        self.assertTrue(rf.enqueue("proj", 31, source="mr_watch")["task_id"])

    def test_unknown_source_is_refused(self):
        with self.assertRaises(PermissionError):
            rf.enqueue("proj", 32, source="random-bot")

    def test_enqueue_text_refuses_links_to_another_host(self):
        text = ("review https://evil.example.com/grp/proj/-/merge_requests/40 and "
                "https://gitlab.example.vn/grp/proj/-/merge_requests/41")
        out = rf.enqueue_text(text, source="fchat", requester_id="U1")
        self.assertEqual(out["added"], ["77-41"])
        self.assertEqual(out["refused"][0]["reason"], "not our GitLab host")
        with self.assertRaises(ValueError):
            rf.enqueue_text("https://evil.example.com/grp/proj/-/merge_requests/42", source="fchat")
        with self.assertRaises(ValueError):
            rf.enqueue_text("chào cả nhà", source="fchat")
        with self.assertRaises(ValueError):           # userinfo: the real host is evil.example.com
            rf.enqueue_text("https://gitlab.example.vn:pw@evil.example.com/grp/proj/-/merge_requests/43",
                            source="fchat")

    def test_legacy_queue_cli_goes_through_the_gate(self):
        """Codex F.5 #2: review_queue.py add / add-text = gated durable enqueue."""
        import contextlib
        import io
        import review_queue as rq

        def cli(*argv):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rq.main(list(argv))
            return json.loads(buf.getvalue())
        out = cli("add-text", "https://evil.example.com/grp/proj/-/merge_requests/44", "--source", "fchat")
        self.assertTrue(out.get("error"))
        self.assertFalse(rc.load_job("77-44"))
        self.assertTrue(cli("add", "--project", "proj", "--iid", "45", "--source", "nobody").get("error"))
        ok = cli("add", "--project", "proj", "--iid", "45", "--source", "fchat")
        self.assertEqual(ok["job"]["id"], "77-45")
        with S.Store() as st:
            self.assertEqual(st.get(ok["task_id"])["data"]["intake"]["source"], "fchat")

    def test_quota_pauses_then_resumes_without_rerunning_finished_steps(self):
        rf.enqueue("proj", 6)
        os.environ["FAKE_AGENT_MODE"] = "quota"
        self.assertEqual(self.work()[-1]["status"], "paused_quota")
        t = self.task()
        self.assertEqual((t["status"], t["attempts"], t["step"]), ("paused_quota", 0, "prepare"))
        os.environ["FAKE_AGENT_MODE"] = "ok"
        with mock.patch("time.time", return_value=time.time() + 3 * 3600):
            ran = self.work()
        self.assertEqual(ran[-1]["status"], "done")
        self.assertEqual(self.posted, ["77-6"])
        prepares = [c for c in S.Store().checkpoints(t["id"]) if c["step"] == "prepare"]
        self.assertEqual(len(prepares), 1)                                          # prepare NOT redone

    def test_shadow_mode_blocks_until_human_then_reconcile_continues(self):
        os.environ["REVIEW_AUTO_POST"] = "shadow"
        rf.enqueue("proj", 7)
        self.assertEqual(self.work()[-1]["status"], "blocked")
        self.assertEqual(self.posted, [])
        job = rc.load_job("77-7")
        review = rp.load_review(job)
        job["approval"].update(via="terminal", decision="approved", payload_hash=rp.payload_hash(job, review))
        rc.save_job(job)                                           # what `review_post.py approve` records
        self.assertEqual(rf.reconcile()["reconciled"][0]["unblocked"], "decided")
        self.assertEqual(self.work()[-1]["status"], "done")
        self.assertEqual(self.posted, ["77-7"])
        self.assertEqual(self.runs(), 1)                           # agent not run again

    def test_new_mr_revision_reruns_review_and_posts_for_new_head(self):
        os.environ["REVIEW_AUTO_POST"] = "shadow"
        rf.enqueue("proj", 8)
        self.assertEqual(self.work()[-1]["status"], "blocked")
        self.head["sha"] = H2                                      # author pushed while we waited
        os.environ["REVIEW_AUTO_POST"] = "auto"
        with S.Store() as st:
            st.unblock(self.task()["id"])
        ran = self.work()
        self.assertEqual(ran[-1]["status"], "done")
        self.assertEqual(self.runs(), 2)                           # reviewed again for the new head
        self.assertEqual(rc.load_job("77-8")["diff_refs"]["head_sha"], H2)

    def test_stale_at_post_time_requeues_without_attempt(self):
        rf.enqueue("proj", 9)
        real_post = self.fake_post

        def moved_then_post(jid, **kw):
            if not getattr(self, "_moved", False):
                self._moved = True
                self.head["sha"] = H2
            return real_post(jid, **kw)
        with mock.patch.object(rp, "post", side_effect=moved_then_post):
            ran = self.work()
        self.assertEqual([r["status"] for r in ran], ["queued", "done"])
        self.assertEqual(self.task()["attempts"], 0)
        self.assertEqual(self.runs(), 2)

    def test_closed_mr_cancels_job_and_finishes(self):
        rf.enqueue("proj", 10)
        self.head["state"] = "merged"
        self.assertEqual(self.work()[-1]["status"], "done")
        self.assertEqual(rc.load_job("77-10")["status"], "cancelled")
        self.assertEqual(self.runs(), 0)

    def test_agent_failure_counts_attempts_then_fails(self):
        os.environ["FAKE_AGENT_MODE"] = "crash"
        rf.enqueue("proj", 11)
        statuses = [r["status"] for r in self.work()]
        self.assertEqual(statuses[-1], "failed")
        self.assertEqual(self.task()["attempts"], 3)

    def test_job_claimed_by_orca_path_is_not_taken(self):
        import review_queue as rq
        rf.enqueue("proj", 12)
        rq.claim_next("orca-worker")                               # the old path took it first
        self.assertEqual(self.work()[-1]["status"], "blocked")
        self.assertIn("orca-worker", self.task()["last_error"])
        self.assertEqual(self.runs(), 0)

    # ------------------------------------------------------------ Codex review F.4b round 1
    def test_tampering_agent_is_quarantined_until_a_human_releases_it(self):
        """#1 + round 2 HIGH: a forged approval WITH a matching hash must never be used —
        not by reconcile, not by a later work cycle, not by approval_status/post."""
        os.environ["REVIEW_AUTO_POST"] = "shadow"            # a human approval would be required
        os.environ["FAKE_AGENT_MODE"] = "tamper"
        rf.enqueue("proj", 20)
        self.assertEqual(self.work()[-1]["status"], "blocked")
        self.assertIn("protected state", self.task()["last_error"])
        for _ in range(3):                                    # repeated cycles change nothing
            self.assertEqual(rf.reconcile()["reconciled"], [])
            self.assertEqual(self.work(), [])
        job = rc.load_job("77-20")
        self.assertTrue(job["quarantine"])
        self.assertIsNone(job["approval"])                   # the forged approval was dropped
        self.assertEqual(rp.approval_status(job), "pending")
        self.assertFalse(os.path.exists(rp.review_path(job)))   # agent output set aside
        self.assertEqual(self.posted, [])
        with self.assertRaises(PermissionError):
            rp.request_approval(job)
        # a human looks, then releases: the review is done again by a fresh agent run
        os.environ["FAKE_AGENT_MODE"] = "ok"
        os.environ["REVIEW_AUTO_POST"] = "auto"
        with mock.patch("sys.stdin.isatty", return_value=True), mock.patch("builtins.input", return_value="y"):
            self.assertTrue(rf.release_quarantine(self.task()["id"])["released"])
        self.assertEqual(self.work()[-1]["status"], "done")
        self.assertEqual(self.posted, ["77-20"])
        self.assertEqual(self.runs(), 2)

    def test_direct_request_approval_is_once_per_payload(self):
        """Round 2 MED: the CLI / Orca path calling request_approval directly gets the same guarantee."""
        os.environ["REVIEW_AUTO_POST"] = "shadow"
        rf.enqueue("proj", 25)
        self.assertEqual(self.work()[-1]["status"], "blocked")      # flow requested approval once
        sends = []
        with mock.patch.object(rp, "_telegram_request", side_effect=lambda j, q: sends.append(1) or {}):
            again = rp.request_approval(rc.load_job("77-25"))
        self.assertTrue(again["reused"])
        self.assertEqual(sends, [])

    def test_direct_request_crash_after_send_is_uncertain_not_resent(self):
        os.environ["REVIEW_AUTO_POST"] = "shadow"
        rf.enqueue("proj", 26)
        os.environ["FAKE_AGENT_MODE"] = "ok"
        sends = []

        def send_then_die(job, question):
            sends.append(job["id"])
            raise RuntimeError("process died right after sending")
        with mock.patch.object(rp, "_telegram_request", side_effect=send_then_die):
            ran = self.work()
            self.assertEqual(ran[-1]["status"], "blocked")
            with self.assertRaises(rp.ApprovalUncertain):           # a direct caller is refused too
                rp.request_approval(rc.load_job("77-26"))
        self.assertEqual(sends, ["77-26"])

    def test_agent_env_has_no_toolkit_secrets(self):
        sys.path.insert(0, os.path.join(_ROOT, ".claude", "skills", "auto-dev", "tools"))
        import agent_runner as ar
        with mock.patch.dict(os.environ, {"GITLAB_PRIVATE_TOKEN": "glpat-x", "ATASK_PAT_TOKEN": "p",
                                          "MY_SERVICE_PASSWORD": "pw", "ANTHROPIC_API_KEY": "keep"}):
            env = ar.agent_env()
        for k in ("GITLAB_PRIVATE_TOKEN", "ATASK_PAT_TOKEN", "MY_SERVICE_PASSWORD"):
            self.assertNotIn(k, env)
        self.assertEqual(env["ANTHROPIC_API_KEY"], "keep")
        self.assertIn("PATH", {k.upper() for k in env})
        self.assertEqual(env["TOOLKIT_HEADLESS"], "1")

    def test_quota_pause_is_not_reaped_by_the_orca_path(self):
        """#2: a paused flow job keeps its owner; the Orca reaper/claimer must not take it."""
        import review_queue as rq
        rf.enqueue("proj", 21)
        os.environ["FAKE_AGENT_MODE"] = "quota"
        self.assertEqual(self.work()[-1]["status"], "paused_quota")
        job = rc.load_job("77-21")
        job["claimed_at"] = 0                                   # long past REVIEW_JOB_TIMEOUT_MIN
        rc.save_job(job)
        rq.pending()                                            # runs _reap
        self.assertEqual(rq.claim_next("orca-worker"), {})
        self.assertTrue(rc.load_job("77-21")["worker"].startswith("flow:"))
        os.environ["FAKE_AGENT_MODE"] = "ok"
        with mock.patch("time.time", return_value=time.time() + 3 * 3600):
            self.assertEqual(self.work()[-1]["status"], "done")
        self.assertEqual(self.posted, ["77-21"])

    def test_sweeper_stale_reset_restarts_a_blocked_task(self):
        """#3: the sweeper marked the job queued (MR moved) while we waited for a human."""
        os.environ["REVIEW_AUTO_POST"] = "shadow"
        rf.enqueue("proj", 22)
        self.assertEqual(self.work()[-1]["status"], "blocked")
        self.head["sha"] = H2
        job = rc.load_job("77-22")
        job.update(status="queued", approval=None)              # what review_post.post does when stale
        rc.save_job(job)
        os.environ["REVIEW_AUTO_POST"] = "auto"
        self.assertEqual(rf.reconcile()["reconciled"][0]["restarted"], "queued")
        self.assertEqual(self.work()[-1]["status"], "done")
        self.assertEqual(self.runs(), 2)

    def test_head_moved_while_blocked_wakes_the_task(self):
        """#3: no sweeper involved — reconcile notices the new head itself."""
        os.environ["REVIEW_AUTO_POST"] = "shadow"
        os.environ["REVIEW_FLOW_HEAD_CHECK_SEC"] = "0"
        rf.enqueue("proj", 23)
        self.assertEqual(self.work()[-1]["status"], "blocked")
        self.head["sha"] = H2
        os.environ["REVIEW_AUTO_POST"] = "auto"
        self.assertEqual(rf.reconcile()["reconciled"][0]["unblocked"], "mr moved")
        self.assertEqual(self.work()[-1]["status"], "done")
        self.assertEqual(self.runs(), 2)

    def test_interrupted_approval_request_is_not_sent_twice(self):
        """#4: crash after the intent was persisted -> the next run does NOT send a second card."""
        os.environ["REVIEW_AUTO_POST"] = "shadow"
        rf.enqueue("proj", 24)
        sent = []

        def crash_after_send(job, question):           # the Telegram send itself (inside the guard)
            sent.append(job["id"])
            raise RuntimeError("process died right after sending")
        with mock.patch.object(rp, "_telegram_request", side_effect=crash_after_send):
            ran = self.work()
        self.assertEqual(sent, ["77-24"])                        # sent once, then never again
        self.assertEqual(ran[-1]["status"], "blocked")
        self.assertIn("may already have been sent", self.task()["last_error"])

    def test_agent_runner_timeout_kills_the_cli_tree(self):
        """#5: the inner CLI timeout must not leave an agent child running."""
        sys.path.insert(0, os.path.join(_ROOT, ".claude", "skills", "auto-dev", "tools"))
        import agent_runner as ar
        import importlib.util
        spec = importlib.util.spec_from_file_location("tse", os.path.join(os.path.dirname(__file__),
                                                                          "test_store_engine.py"))
        tse = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tse)
        pidfile = os.path.join(self.tmp, "child.pid").replace("\\", "/")
        launcher = os.path.join(self.tmp, "launcher.py")
        with open(launcher, "w") as f:
            f.write("import subprocess,sys,time\n"
                    "subprocess.Popen([sys.executable,'-c','import os,time;"
                    f"open(\"{pidfile}\",\"w\").write(str(os.getpid()));time.sleep(60)'])\n"
                    "time.sleep(60)\n")
        tmpl = f'"{sys.executable.replace(chr(92), "/")}" "{launcher.replace(chr(92), "/")}"'
        with self.assertRaises(ar.AgentRunError):
            ar.run_turn("hi", backend="custom", cmd_template=tmpl, timeout=3)
        time.sleep(0.5)
        self.assertFalse(tse._alive(int(open(pidfile).read())), "agent child survived the CLI timeout")


if __name__ == "__main__":
    unittest.main()
