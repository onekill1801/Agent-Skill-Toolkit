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
                                     "WORK_DIR": self.tmp, "TOOLKIT_MAX_PARALLEL": "0"}).start()

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
            st.set_capacity(2)
            self.assertTrue(st.claim("w"))
            self.assertTrue(st.claim("w"))
            self.assertEqual(st.claim("w"), {})
            st.set_capacity(0)                                        # no cap
            self.assertTrue(st.claim("w"))

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
            st.set_capacity(1)
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


class CoordinatorRound1FixesTest(Base):
    """Codex F.7 round 1."""

    def test_gitlab_project_scope_is_per_thread(self):                       # #1
        sys.path.insert(0, os.path.join(_ROOT, ".claude", "skills", "dev-automation", "tools"))
        import gitlab_api
        seen, gate = {}, threading.Barrier(2)
        before = os.environ.get("GITLAB_PROJECT_ID")

        def worker(pid):
            with gitlab_api.project_scope(pid):
                gate.wait()
                time.sleep(0.05)
                seen[pid] = gitlab_api._project_id()
        ts = [threading.Thread(target=worker, args=(p,)) for p in ("11", "22")]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(seen, {"11": "11", "22": "22"})
        self.assertEqual(os.environ.get("GITLAB_PROJECT_ID"), before)

    def test_default_capacity_applies_to_every_caller(self):                 # #4
        """Codex F.7 R2 #4: the cap lives in the database, not in each process's environment."""
        with S.Store() as st:
            st.set_capacity(2)
            for r in "abc":
                st.add("k", r)
            self.assertTrue(st.claim("w"))
        with mock.patch.dict(os.environ, {"TOOLKIT_MAX_PARALLEL": "50"}):   # another process, other env
            with S.Store() as st2:
                self.assertEqual(st2.capacity(), 2)
                self.assertTrue(st2.claim("w2"))
                self.assertEqual(st2.claim("w2"), {})
        fresh = os.path.join(self.tmp, "fresh.db")
        with mock.patch.dict(os.environ, {"TOOLKIT_MAX_PARALLEL": "3"}):
            with S.Store(fresh) as st3:
                self.assertEqual(st3.capacity(), 3)                         # initial value only

    def test_accept_resolves_only_the_inspected_revision(self):             # R2 #5
        with S.Store() as st:
            t = st.add("k", "a")
            st.block(t["id"], st.claim("w")["lease_token"], "proposal: x")
            seen = st.get(t["id"])
            time.sleep(0.01)
            st.unblock(t["id"])
            st.block(t["id"], st.claim("w")["lease_token"], "proposal: y")     # re-run, blocked again
            self.assertFalse(st.resolve_blocked(t["id"], "done", if_updated=seen["updated"]))
            self.assertFalse(st.resolve_blocked(t["id"], "done", if_error_prefix="dependency "))
            self.assertTrue(st.resolve_blocked(t["id"], "done", if_updated=st.get(t["id"])["updated"],
                                               if_error_prefix="proposal: "))

    def test_running_legacy_task_keeps_keyed_tasks_of_its_kind_off(self):    # R2 #3
        with S.Store() as st:
            st.add("verify", "legacy")
            st.claim("old", kinds=("verify",))                                # running, no resource
            st.add("verify", "new", resource="repo:/x")
            st.add("review_mr", "other", resource="mr:1")
            self.assertEqual(st.claim("w", kinds=("verify",)), {})
            self.assertEqual(st.claim("w")["ref"], "other")                   # other kinds unaffected

    def test_failure_propagates_down_a_chain(self):                          # #6
        with S.Store() as st:
            a = st.add("k", "a", max_attempts=1)
            b = st.add("k", "b", depends_on=[a["id"]])
            c = st.add("k", "c", depends_on=[b["id"]])
            st.fail(a["id"], st.claim("w")["lease_token"], "boom")
            self.assertEqual(sorted(st.block_orphans()), sorted([b["id"], c["id"]]))
            self.assertTrue(st.get(c["id"])["last_error"].startswith(S.DEP_BLOCK))

    def test_accept_only_a_real_proposal(self):                              # #5
        import coordinator as cf
        with S.Store() as st:
            a = st.add("k", "a", max_attempts=1)
            t = st.add(atask_flow.KIND, "T9@open", depends_on=[a["id"]], data={"atask": {"name": "x"}})
            st.fail(a["id"], st.claim("w", kinds=("k",))["lease_token"], "boom")
            st.block_orphans()
        with self.assertRaises(ValueError):
            cf.accept(t["id"])
        with S.Store() as st:
            self.assertEqual(st.get(t["id"])["status"], "blocked")

    def test_board_keeps_old_open_tasks_and_grandchildren(self):            # #7
        with S.Store() as st:
            st.add("k", "old-open", title="việc cũ còn mở", priority=9)
            for i in range(30):                                  # 30 newer tasks finished after it
                st.add("k", f"done{i}", priority=0)
                got = st.claim("w", kinds=("k",))
                self.assertEqual(got["ref"], f"done{i}")
                st.complete(got["id"], got["lease_token"])
            root = st.add("request", "R", title="chương trình")
            mid = st.add("k", "M", title="đợt 1", parent=root["id"])
            st.add("k", "L", title="lát cắt 1.1", parent=mid["id"])
            b = C.board(st, limit=5)
        self.assertIn("việc cũ còn mở", b["text"])
        self.assertIn("    ⏳", b["text"])                                    # grandchild, depth 2
        self.assertIn("lát cắt 1.1", b["text"])

    def test_legacy_verify_task_without_resource(self):                     # #3
        import verify
        repo = tempfile.mkdtemp(prefix="repo-")
        with S.Store() as st:
            idle = st.add(verify.L.KIND, f"{repo}@a", data={"repo": repo})
            running = st.add(verify.L.KIND, f"{repo}@b", data={"repo": repo})
            st.claim("old-worker", kinds=(verify.L.KIND,))           # whichever comes first is now running
            with self.assertRaises(ValueError):
                verify._upgrade_legacy(st, repo)
            out = verify._upgrade_legacy(st)                       # no repo: backfill only, never raises
            self.assertEqual(len(out["running_without"]), 1)       # the idle one was keyed by the call above
            self.assertEqual(sum(1 for t in (idle, running) if st.get(t["id"])["resource"]), 1)
            with self.assertRaises(ValueError):
                verify._steps(st.get(idle["id"]))
        with S.Store() as st:
            for t in (idle, running):
                if st.get(t["id"])["status"] == "running":
                    st.cancel(t["id"])
            verify._upgrade_legacy(st, repo)
            left = [st.get(t["id"]) for t in (idle, running) if st.get(t["id"])["status"] != "cancelled"]
            self.assertEqual([x["resource"] for x in left], [verify.resource_of(repo)])
