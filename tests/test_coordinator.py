#!/usr/bin/env python3
"""Coordinator (checklist F.7): dependencies, exclusive resources, global parallel cap, parallel
workers, aTask proposals, progress board.
    python -m unittest discover -s tests
"""

import contextlib
import io
import json
import os
import runpy
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
runpy.run_path(os.path.join(_ROOT, ".claude", "skills", "_runtime", "bootstrap.py"))["activate"]()
from agent_skill_toolkit.core import store as S  # noqa: E402
from agent_skill_toolkit.workflows import coordinator as C  # noqa: E402
from agent_skill_toolkit.workflows import engine as E  # noqa: E402
from agent_skill_toolkit.workflows.intake import atask_flow  # noqa: E402

sys.path.insert(0, os.path.join(_ROOT, ".claude", "skills", "auto-dev", "tools"))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="coord-")
        self.db = os.path.join(self.tmp, "t.db")
        mock.patch.dict(os.environ, {"TOOLKIT_STORE": self.db, "TOOLKIT_STATE_DIR": self.tmp,
                                     "WORK_DIR": self.tmp}).start()

    def tearDown(self):
        mock.patch.stopall()


class StoreSchedulingTest(Base):
    def test_dependency_must_be_done_first(self):
        with S.Store() as st:
            a = st.add("k", "a")
            b = st.add("k", "b", depends_on=[a["id"]], priority=0)       # higher priority, but waits
            first = st.claim("w")
            self.assertEqual(first["id"], a["id"])
            self.assertEqual(st.claim("w"), {})
            st.complete(a["id"], first["lease_token"])
            self.assertEqual(st.claim("w")["id"], b["id"])

    def test_failed_dependency_blocks_the_dependent(self):
        with S.Store() as st:
            a = st.add("k", "a", max_attempts=1)
            b = st.add("k", "b", depends_on=[a["id"]])
            c = st.claim("w")
            self.assertEqual(st.fail(a["id"], c["lease_token"], "boom"), "failed")
            self.assertEqual(st.block_orphans(), [b["id"]])
            t = st.get(b["id"])
            self.assertEqual(t["status"], "blocked")
            self.assertIn(a["id"], t["last_error"])

    def test_unknown_dependency_or_parent_is_refused(self):
        with S.Store() as st:
            with self.assertRaises(ValueError):
                st.add("k", "x", depends_on=["nope"])
            with self.assertRaises(ValueError):
                st.add("k", "y", parent="nope")

    def test_same_resource_never_runs_twice(self):
        with S.Store() as st:
            a = st.add("k", "a", resource="repo:/r")
            st.add("k", "b", resource="repo:/r")
            other = st.add("k", "c", resource="repo:/other")
            first = st.claim("w")
            self.assertEqual(first["id"], a["id"])
            self.assertEqual(st.claim("w")["id"], other["id"])      # b skipped: /r is held
            self.assertEqual(st.claim("w"), {})
            st.complete(a["id"], first["lease_token"])
            self.assertEqual(st.claim("w")["ref"], "b")

    def test_expired_holder_does_not_hold_the_resource(self):
        with S.Store() as st:
            st.add("k", "a", resource="r")
            st.add("k", "b", resource="r")
            st.claim("w", lease_s=-1)                                 # crashed: lease already expired
            self.assertIn(st.claim("w")["ref"], ("a", "b"))

    def test_global_running_cap(self):
        with S.Store() as st:
            for r in "abc":
                st.add("k", r)
            self.assertTrue(st.claim("w", max_running=2))
            self.assertTrue(st.claim("w", max_running=2))
            self.assertEqual(st.claim("w", max_running=2), {})
            self.assertTrue(st.claim("w"))                            # no cap

    def test_migration_adds_columns_to_an_old_database(self):
        import sqlite3
        old = os.path.join(self.tmp, "old.db")
        con = sqlite3.connect(old)
        con.execute("create table tasks (id text primary key, kind text not null, ref text not null, "
                    "title text default '', status text not null, step text default '', attempts integer "
                    "default 0, max_attempts integer default 3, priority integer default 5, lease_owner text, "
                    "lease_until real, resume_at real, last_error text, backend text, data text default '{}', "
                    "created real not null, updated real not null)")
        con.commit()
        con.close()
        with S.Store(old) as st:
            t = st.add("k", "a", resource="r")
            self.assertEqual(t["resource"], "r")


class CoordinatorRunTest(Base):
    def registry(self, record, sleep=0.3):
        def work(ctx):
            with self.lock:
                self.live += 1
                self.peak = max(self.peak, self.live)
            time.sleep(sleep)
            with self.lock:
                self.live -= 1
                record.append(ctx.task["ref"])
            return {"ok": True}
        return {"k": [("work", work)]}

    def setUp(self):
        super().setUp()
        self.lock, self.live, self.peak = threading.Lock(), 0, 0

    def test_independent_tasks_run_in_parallel(self):
        order = []
        with S.Store() as st:
            for r in "ab":
                st.add("k", r)
        out = C.Coordinator(self.registry(order), parallel=2).run(once=True)
        self.assertEqual(sorted(order), ["a", "b"])
        self.assertEqual(self.peak, 2)
        self.assertEqual([r["status"] for r in out["ran"]], ["done", "done"])

    def test_chain_and_resource_are_respected(self):
        order = []
        with S.Store() as st:
            a = st.add("k", "a")
            st.add("k", "b", depends_on=[a["id"]])
            st.add("k", "c", resource="repo:/x")
            st.add("k", "d", resource="repo:/x")
        C.Coordinator(self.registry(order, sleep=0.2), parallel=3).run(once=True)
        self.assertEqual(sorted(order), ["a", "b", "c", "d"])
        self.assertLess(order.index("a"), order.index("b"))
        with S.Store() as st:
            self.assertEqual(st.summary()["counts"], {"done": 4})

    def test_cap_counts_other_processes(self):
        order = []
        with S.Store() as st:
            st.add("other", "busy")
            st.claim("someone-else")                 # running elsewhere, counts against the cap
            st.add("k", "a")
        out = C.Coordinator(self.registry(order), parallel=1).run(once=True)
        self.assertEqual(order, [])
        self.assertEqual(out["ran"], [])

    def test_failing_hook_does_not_stop_the_loop(self):
        def bad():
            raise RuntimeError("gitlab down")
        with S.Store() as st:
            st.add("k", "a")
        ticks = []
        C.Coordinator(self.registry([]), parallel=1, hooks=[bad]).run(once=True, on_tick=ticks.append)
        self.assertIn("gitlab down", ticks[0]["hooks"][0]["error"])


class AtaskProposalTest(Base):
    def cli(self, *argv):
        import coordinator as cf
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cf.main(list(argv))
        text = buf.getvalue()
        return rc, (json.loads(text) if text.strip().startswith("{") else text)

    def test_proposal_blocks_then_accept_records_the_decision(self):
        with S.Store() as st:
            t = st.add(atask_flow.KIND, "T1@open", title="Fix NPE", data={
                "atask": {"id": "T1", "name": "Fix NullPointerException khi xuất báo cáo", "type": "bug"}})
        C.Coordinator(atask_flow.REGISTRY, parallel=1).run(once=True)
        with S.Store() as st:
            got = st.get(t["id"])
        self.assertEqual(got["status"], "blocked")
        self.assertIn("proposal: code / lane vua", got["last_error"])
        _, out = self.cli("accept", t["id"])
        self.assertTrue(out["accepted"])
        self.assertEqual(out["lane"], "vua")
        with S.Store() as st:
            self.assertEqual(st.get(t["id"])["status"], "done")
        _, again = self.cli("accept", t["id"])
        self.assertTrue(again["error"])
        _, bad = self.cli("accept", t["id"], "--lane", "huge")
        self.assertTrue(bad["error"])

    def test_board_groups_children_under_their_parent(self):
        with S.Store() as st:
            parent = st.add("request", "R1", title="etask: tối ưu projects/home")
            a = st.add("k", "B1", title="gộp query", parent=parent["id"])
            st.add("k", "B2", title="cache tên", parent=parent["id"], depends_on=[a["id"]])
            c = st.claim("w", kinds=("k",))
            st.complete(a["id"], c["lease_token"])
            b = C.board(st)
        self.assertIn("etask: tối ưu projects/home", b["text"])
        self.assertIn("50%", b["text"])
        self.assertIn("  ✅", b["text"])
        rc, text = self.cli("board")
        self.assertEqual(rc, 0)
        self.assertIn("gộp query", text)

    def test_registry_loads_every_workflow(self):
        import coordinator as cf
        reg, hooks, warn = cf.load_registry()
        self.assertEqual(warn, [])
        self.assertTrue({"review_mr", "verify", "atask_intake"} <= set(reg), sorted(reg))
        with self.assertRaises(ValueError):
            cf.load_registry(["nope"])


if __name__ == "__main__":
    unittest.main()
