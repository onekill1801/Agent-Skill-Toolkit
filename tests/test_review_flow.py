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
               "TOOLKIT_STORE": os.path.join(self.tmp, "t.db"), "REVIEW_ORCA": "0", "REVIEW_CODEGRAPH": "0",
               "REVIEW_AUTO_POST": "auto", "REVIEW_AUTO_POST_PROJECTS": "grp/proj",
               "GITLAB_URL": "https://gitlab.example.vn", "FAKE_AGENT_LOG": self.log, "FAKE_AGENT_MODE": "ok"}
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
        self.assertEqual(rf.reconcile()["reconciled"][0]["unblocked"], True)
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


if __name__ == "__main__":
    unittest.main()
