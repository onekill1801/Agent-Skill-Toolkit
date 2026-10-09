#!/usr/bin/env python3
"""Stdlib unittest for the mr-review pipeline (queue / post gate), the PreToolUse guard
and agent_runner role routing. GitLab and Orca are faked — no network.
    python -m unittest discover -s tests
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, ".."))
sys.path.insert(0, os.path.join(_ROOT, ".claude", "skills", "mr-review", "tools"))
sys.path.insert(0, os.path.join(_ROOT, ".claude", "skills", "auto-dev", "tools"))

import review_common as rc  # noqa: E402
import review_post as rp  # noqa: E402
import review_queue as rq  # noqa: E402

HEAD = "a" * 40


def fake_mr(iid, state="opened", head=HEAD):
    return {"iid": iid, "state": state, "title": f"MR {iid}", "web_url": f"https://g/x/-/merge_requests/{iid}",
            "source_branch": "feat/a", "target_branch": "develop", "author": {"username": "dev"},
            "diff_refs": {"base_sha": "b" * 40, "start_sha": "c" * 40, "head_sha": head}, "sha": head,
            "references": {"full": "grp/proj!%d" % iid}}


class QueueTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rq-test-")
        self.env = mock.patch.dict(os.environ, {"REVIEW_STATE_DIR": self.tmp, "REVIEW_ORCA": "0",
                                                "REVIEW_MAX_PARALLEL": "2"})
        self.env.start()
        self.p1 = mock.patch.object(rc, "resolve_project_id", return_value=("77", "grp/proj"))
        self.p2 = mock.patch.object(rc, "gitlab_mr", side_effect=lambda pid, iid: fake_mr(iid))
        self.p1.start()
        self.p2.start()

    def tearDown(self):
        mock.patch.stopall()

    def test_duplicate_requests_merge_into_one_job(self):
        a = rq.add_job("proj", 5, requester="A")
        b = rq.add_job("proj", 5, requester="B", priority=1)
        self.assertFalse(a["duplicate"])
        self.assertTrue(b["duplicate"])
        self.assertEqual(b["job"]["priority"], 1)
        self.assertEqual([r["by"] for r in b["job"]["requests"]], ["A", "B"])

    def test_closed_mr_rejected(self):
        with mock.patch.object(rc, "gitlab_mr", return_value=fake_mr(9, state="merged")):
            with self.assertRaises(ValueError):
                rq.add_job("proj", 9)

    def test_claim_respects_priority_and_capacity(self):
        rq.add_job("proj", 1, priority=5)
        rq.add_job("proj", 2, priority=0)
        rq.add_job("proj", 3, priority=5)
        self.assertEqual(rq.claim_next("w")["iid"], 2)
        self.assertEqual(rq.claim_next("w")["iid"], 1)
        self.assertEqual(rq.claim_next("w"), {})          # capacity 2 reached
        self.assertFalse(rq.pending()["can_start"])

    def test_dead_worker_is_requeued_then_failed(self):
        rq.add_job("proj", 4)
        job = rq.claim_next("w")
        for expected in ("queued", "failed"):
            job["status"], job["claimed_at"] = "running", 0
            rc.save_job(job)
            rq.pending()
            job = rc.load_job(job["id"])
            self.assertEqual(job["status"], expected)


class ParseTest(unittest.TestCase):
    def test_url_and_bang(self):
        with mock.patch.object(rc, "registry", return_value={"etask": {}}):
            self.assertEqual(rc.parse_mr_refs("xem https://git.x/a/b/c/-/merge_requests/12 nhé"),
                             [{"project": "a/b/c", "iid": 12, "host": "git.x"}])
            self.assertEqual(rc.parse_mr_refs("review etask !34"), [{"project": "etask", "iid": 34}])
            self.assertEqual(rc.parse_mr_refs("review MR 34", default_project="etask"),
                             [{"project": "etask", "iid": 34}])
            self.assertEqual(rc.parse_mr_refs("review !34"), [])   # no project known → nothing


class PostGateTest(unittest.TestCase):
    def job(self, **kw):
        j = {"id": "77-5", "project_id": "77", "iid": 5, "head_sha": HEAD,
             "diff_refs": {"base_sha": "b" * 40, "start_sha": "c" * 40, "head_sha": HEAD}}
        j.update(kw)
        return j

    def review(self, **kw):
        r = {"head_sha": HEAD, "verdict": "COMMENT", "summary": "ok",
             "findings": [{"severity": "minor", "title": "t", "file": "a.py", "line": 3}]}
        r.update(kw)
        return r

    def test_validate(self):
        self.assertEqual(rp.validate(self.job(), self.review()), [])
        errs = rp.validate(self.job(), self.review(head_sha="x", verdict="APPROVE",
                                                   findings=[{"severity": "blocker", "title": "b"}]))
        self.assertTrue(any("head_sha" in e for e in errs))
        self.assertTrue(any("contradictory" in e for e in errs))

    def test_job_file_decision_not_trusted_for_orca(self):
        forged = self.job(approval={"via": "orca", "run": "r", "id": "g", "decision": "approved"})
        fake = mock.Mock()
        fake.gate_get.return_value = {"status": "pending"}
        with mock.patch.object(rc, "orca", return_value=fake):
            self.assertEqual(rp.approval_status(forged), "pending")
        fake.gate_get.return_value = {"status": "resolved", "resolution": "approve"}
        with mock.patch.object(rc, "orca", return_value=fake):
            self.assertEqual(rp.approval_status(forged), "approved")

    def test_agents_cannot_approve_without_tty(self):
        with mock.patch.object(sys.stdin, "isatty", return_value=False):
            with self.assertRaises(PermissionError):
                rp.human_decide(self.job(), True)

    def test_post_refuses_without_approval(self):
        with mock.patch.object(rc, "load_job", return_value=self.job(status="awaiting_approval",
                                                                     approval={"via": "terminal"})):
            with self.assertRaises(PermissionError):
                rp.post("77-5")

    def test_render_note_folds_non_inline_findings(self):
        with mock.patch.object(rp, "_ws", return_value={}):
            note = rp.render_note(self.job(), self.review(), set())
        self.assertIn("🟢 **t**", note)
        self.assertIn("`a.py:3`", note)


class GuardHookTest(unittest.TestCase):
    HOOK = os.path.join(_ROOT, ".claude", "hooks", "guard_write.py")

    def decide(self, event, **env):
        e = {k: v for k, v in os.environ.items() if k not in ("TOOLKIT_HEADLESS", "CLAUDE_TG_BRIDGE")}
        e.update(env)
        p = subprocess.run([sys.executable, self.HOOK], input=json.dumps(event), capture_output=True,
                           text=True, env=e)
        out = p.stdout.strip()
        return json.loads(out)["hookSpecificOutput"]["permissionDecision"] if out else None

    def bash(self, cmd, **env):
        return self.decide({"tool_name": "Bash", "tool_input": {"command": cmd}}, **env)

    def test_decisions(self):
        self.assertEqual(self.bash("python3 gitlab_api.py merge-mr 3"), "ask")
        self.assertEqual(self.bash("python3 gitlab_api.py merge-mr 3", TOOLKIT_HEADLESS="1"), "deny")
        self.assertEqual(self.bash("python3 review_post.py approve 1-2"), "deny")
        self.assertIsNone(self.bash("python3 gitlab_api.py mr-detail 3"))
        self.assertIsNone(self.bash("python3 review_post.py request-approval 1-2"))
        self.assertEqual(self.bash("cat .env"), "ask")
        self.assertIsNone(self.bash("python3 gitlab_api.py merge-mr 3", CLAUDE_TG_BRIDGE="1"))
        self.assertEqual(self.decide({"tool_name": "Write",
                                      "tool_input": {"file_path": "/r/temp/review_queue/jobs/1-2.json"}}), "deny")


class RoleRoutingTest(unittest.TestCase):
    def test_fallback_on_rate_limit_and_avoid(self):
        import agent_runner as ar
        cfg = {"roles": {"review": ["claude:sonnet", "codex"]}, "limits": {}}
        calls = []

        def fake(prompt, **k):
            calls.append(k["backend"])
            if k["backend"] == "claude":
                raise ar.AgentRunError("usage limit reached")
            return "ok"

        with mock.patch.object(ar, "_agents_config", return_value=cfg), \
             mock.patch.object(ar, "run_turn", side_effect=fake):
            self.assertEqual(ar.run_role("p", "review")["used"], "codex")
            self.assertEqual(calls, ["claude", "codex"])
            calls.clear()
            with self.assertRaises(ar.AgentRunError):      # codex avoided, claude rate-limited
                ar.run_role("p", "review", avoid=("codex",))
            self.assertEqual(calls, ["claude"])

    def test_non_retryable_error_raises(self):
        import agent_runner as ar
        cfg = {"roles": {"review": ["claude", "codex"]}}
        with mock.patch.object(ar, "_agents_config", return_value=cfg), \
             mock.patch.object(ar, "run_turn", side_effect=ar.AgentRunError("syntax error in prompt")):
            with self.assertRaises(ar.AgentRunError):
                ar.run_role("p", "review")


if __name__ == "__main__":
    unittest.main()
