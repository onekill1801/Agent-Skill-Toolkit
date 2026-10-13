#!/usr/bin/env python3
"""Intake gate (checklist F.5): which source may create which task, allowlist by user id, switches,
and the watchers only enqueue.
    python -m unittest discover -s tests
"""

import json
import os
import runpy
import sys
import tempfile
import unittest
from unittest import mock

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
runpy.run_path(os.path.join(_ROOT, ".claude", "skills", "_runtime", "bootstrap.py"))["activate"]()
from agent_skill_toolkit.core import store as S  # noqa: E402
from agent_skill_toolkit.workflows.intake import gate  # noqa: E402


class GateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gate-")
        mock.patch.dict(os.environ, {"WORK_DIR": self.tmp, "TOOLKIT_STATE_DIR": self.tmp,
                                     "TOOLKIT_STORE": os.path.join(self.tmp, "t.db")}).start()

    def tearDown(self):
        mock.patch.stopall()

    def policy(self, data):
        with open(os.path.join(self.tmp, "policy.json"), "w") as f:
            f.write(data if isinstance(data, str) else json.dumps(data))

    def test_kinds_per_source(self):
        self.assertEqual(gate.admit("fchat", "review_mr")["trust"], "low")
        self.assertEqual(gate.admit("self", "implement")["trust"], "high")
        for source, kind in (("fchat", "implement"), ("mr_watch", "implement"), ("schedule", "implement"),
                             ("atask_watch", "review_mr"), ("nobody", "review_mr")):
            with self.assertRaises(PermissionError, msg=(source, kind)):
                gate.admit(source, kind)

    def test_switches(self):
        self.policy({"intake": {"mr_watch": {"on": False}, "fchat": {"on": True}}})
        with self.assertRaises(PermissionError):
            gate.admit("mr_watch", "review_mr")
        self.assertTrue(gate.admit("fchat", "review_mr"))

    def test_unreadable_policy_fails_closed(self):
        self.policy("{not json")
        with self.assertRaises(PermissionError):
            gate.admit("fchat", "review_mr")

    def test_allowlist_is_by_user_id_only(self):
        self.assertTrue(gate.sender_allowed("U1", "u1, U2"))
        self.assertFalse(gate.sender_allowed("U3", "U1,Nguyen Van A"))
        self.assertFalse(gate.sender_allowed("", "*"))                     # no id = nobody
        self.assertFalse(gate.sender_allowed("Nguyen Van A", "U1"))        # a name is not an id
        self.assertTrue(gate.sender_allowed("U9", "*"))
        self.assertFalse(gate.sender_allowed("U9", ""))
        self.assertTrue(gate.sender_allowed("ME", "", me="me"))

    def test_submit_records_where_the_task_came_from(self):
        with S.Store() as st:
            t = gate.submit(st, "atask_watch", "atask_intake", "T1@open", title="x", data={"a": 1},
                            requester_id="u")
            again = gate.submit(st, "atask_watch", "atask_intake", "T1@open")
        self.assertEqual(t["data"]["intake"]["source"], "atask_watch")
        self.assertEqual(t["data"]["a"], 1)
        self.assertTrue(again["duplicate"])


class MrWatchEnqueueTest(unittest.TestCase):
    """mr_watch default mode: enqueue a durable task; an MR that could not be queued is retried."""

    def setUp(self):
        sys.path.insert(0, os.path.join(_ROOT, ".claude", "skills", "dev-automation", "tools"))
        import mr_watch
        self.mw = mr_watch
        self.tmp = tempfile.mkdtemp(prefix="mrw-")
        mock.patch.object(mr_watch, "STATE", os.path.join(self.tmp, "state.json")).start()
        mock.patch.object(mr_watch, "MODE", "store").start()
        self.mrs = [{"project_id": 7, "iid": 1, "sha": "a", "title": "t", "author": "x", "web_url": "u"},
                    {"project_id": 7, "iid": 2, "sha": "b", "title": "t", "author": "x", "web_url": "u"}]
        mock.patch.object(mr_watch.gitlab_api, "list_review_merge_requests", return_value=self.mrs).start()
        self.spawn = mock.patch.object(mr_watch, "_spawn_review").start()

    def tearDown(self):
        mock.patch.stopall()

    def test_store_mode_enqueues_and_never_spawns(self):
        with mock.patch.object(self.mw, "_enqueue_review", side_effect=lambda mr: mr["iid"] == 1) as enq:
            state = {}
            self.mw.poll_once("me", "reviewer", False, True, state)
        self.assertEqual(enq.call_count, 2)
        self.assertEqual(state, {"7:1": "a"})              # !2 failed -> not marked, retried next poll
        self.spawn.assert_not_called()

    def test_enqueue_calls_review_flow_with_source_mr_watch(self):
        fake = mock.Mock(stdout=json.dumps({"task_id": "t1", "job_id": "7-1"}), returncode=0)
        with mock.patch.object(self.mw.subprocess, "run", return_value=fake) as run:
            self.assertTrue(self.mw._enqueue_review(self.mrs[0]))
        argv = run.call_args[0][0]
        self.assertEqual(argv[argv.index("--source") + 1], "mr_watch")
        self.assertTrue(argv[1].endswith("review_flow.py"))
        fake.stdout = json.dumps({"error": True, "message": "switched off"})
        with mock.patch.object(self.mw.subprocess, "run", return_value=fake):
            self.assertFalse(self.mw._enqueue_review(self.mrs[0]))


if __name__ == "__main__":
    unittest.main()
