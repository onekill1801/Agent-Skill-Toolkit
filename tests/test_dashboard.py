"""Web dashboard (dev-automation/tools/dashboard.py): read-only, localhost-only, one failing section
never breaks the page; Store.reader never writes."""

import json
import os
import runpy
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
runpy.run_path(str(ROOT / ".claude" / "skills" / "_runtime" / "bootstrap.py"))["activate"]()
from agent_skill_toolkit.core import store as S  # noqa: E402

sys.path.insert(0, str(ROOT / ".claude" / "skills" / "dev-automation" / "tools"))
import dashboard as D  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "t.db")
        env = mock.patch.dict(os.environ, {"TOOLKIT_STORE": self.db, "TOOLKIT_STATE_DIR": self.tmp,
                                           "REVIEW_STATE_DIR": os.path.join(self.tmp, "rq")})
        env.start()
        self.addCleanup(env.stop)
        D._cache.update(accounts_at=0.0, accounts=None)


class ReaderTest(Base):
    def test_missing_db_is_not_created(self):
        with self.assertRaises(FileNotFoundError):
            S.Store.reader()
        self.assertFalse(os.path.exists(self.db))
        self.assertEqual(D.read_store()["exists"], False)
        self.assertFalse(os.path.exists(self.db))

    def test_reader_reads_but_cannot_write(self):
        with S.Store() as st:
            t = st.add("verify", "repo@feat", title="Kiểm nhánh feat")
            st.db.execute("update tasks set status='blocked', last_error='cần bạn xem' where id=?", (t["id"],))
        before = os.path.getmtime(self.db), os.path.getsize(self.db)
        r = S.Store.reader()
        try:
            self.assertEqual(r.get(t["id"])["title"], "Kiểm nhánh feat")
            with self.assertRaises(sqlite3.OperationalError):
                r.set_capacity(5)
        finally:
            r.close()
        out = D.read_store()
        self.assertTrue(out["exists"])
        row = next(x for x in out["rows"] if x["id"] == t["id"])
        self.assertEqual((row["status"], row["note"]), ("blocked", "cần bạn xem"))
        self.assertIn("capacity", out["settings"])
        self.assertEqual((os.path.getmtime(self.db), os.path.getsize(self.db)), before)


class CollectTest(Base):
    def test_one_failing_section_does_not_break_the_rest(self):
        with mock.patch.object(D, "read_accounts", return_value={"accounts": []}), \
                mock.patch.object(D.status_mod, "sect_daemons", side_effect=RuntimeError("boom")):
            data = D.collect()
        self.assertFalse(data["sections"]["daemons"]["ok"])
        self.assertIn("boom", data["sections"]["daemons"]["error"])
        self.assertNotIn("trace", data["sections"]["daemons"])                 # no traceback leaves
        self.assertTrue(data["sections"]["store"]["ok"])
        self.assertTrue(data["sections"]["accounts"]["ok"])
        json.dumps(data, default=str)                                   # always serialisable

    def test_secrets_in_free_text_are_redacted(self):
        tok = "glpat-" + "A" * 24
        with S.Store() as st:
            t = st.add("verify", "repo@feat", title=f"title {tok}")
            st.db.execute("update tasks set status='blocked', last_error=? where id=?",
                          (f"curl -H 'PRIVATE-TOKEN: {tok}' failed", t["id"]))
        with mock.patch.object(D, "read_accounts", side_effect=RuntimeError(f"orca said token={tok}")):
            text = json.dumps(D.collect(), default=str)
        self.assertNotIn(tok, text)
        self.assertIn("[REDACTED]", text)

    def test_every_string_is_redacted_except_generated_ids(self):
        tok = "glpat-" + "B" * 24
        out = D._clean({"resource": f"repo@{tok}", "branches": tok, "nested": [{"spec": tok}],
                        "id": "t-1", "status": "blocked"})
        self.assertNotIn(tok, json.dumps(out))
        self.assertEqual((out["id"], out["status"]), ("t-1", "blocked"))

    def test_done_24h_and_failures_are_not_capped_by_the_board_limit(self):
        import time as _t
        with S.Store() as st:
            bad = st.add("verify", "r@bad", title="hỏng")
            st.db.execute("update tasks set status='failed', last_error='exit 1', updated=? where id=?",
                          (_t.time() - 3600, bad["id"]))
            old = st.add("verify", "r@old", title="cũ")
            st.db.execute("update tasks set status='failed', updated=? where id=?", (_t.time() - 30 * 86400, old["id"]))
            for i in range(205):                                       # pushes the failure off the board
                t = st.add("verify", f"r@{i}", title=str(i))
                st.db.execute("update tasks set status='done' where id=?", (t["id"],))
        out = D.read_store()
        self.assertEqual(out["done_24h"], 205)
        self.assertNotIn(bad["id"], [r["id"] for r in out["rows"]])
        self.assertEqual([f["id"] for f in out["failed_recent"]], [bad["id"]])   # still "needs you"
        self.assertEqual(out["failed_window_count"], 1)

    def test_secret_in_a_dict_key_is_redacted(self):
        tok = "glpat-" + "D" * 24
        with S.Store() as st:
            t = st.add("verify", "r@x", title="x")
            st.db.execute("update tasks set status=? where id=?", (tok, t["id"]))   # status -> summary key
        sec = D._section(D.read_store)
        self.assertTrue(sec["ok"])
        self.assertNotIn(tok, json.dumps(sec))

    def test_secret_in_a_review_status_is_redacted(self):
        tok = "glpat-" + "C" * 24
        d = os.path.join(self.tmp, "rq", "jobs")
        os.makedirs(d)
        with open(os.path.join(d, "x.json"), "w", encoding="utf-8") as f:
            json.dump({"id": tok, "status": tok, "iid": 1}, f)
        self.assertNotIn(tok, json.dumps(D._section(D.read_reviews)))

    def test_approvals_follow_toolkit_state_dir(self):
        d = os.path.join(self.tmp, "tg_approvals")
        os.makedirs(d)
        with open(os.path.join(d, "abc.json"), "w", encoding="utf-8") as f:
            json.dump({"id": "abc", "tool": "review_post", "status": "pending", "summary": "post review x-1",
                       "created": 1}, f)
        with open(os.path.join(d, "broken.json"), "w", encoding="utf-8") as f:
            f.write("{not json")
        out = D.read_approvals()
        self.assertEqual(([a["id"] for a in out["items"]], out["unreadable"]), (["abc"], 1))

    def test_forwards_read_without_creating_dirs_or_running_tools(self):
        with mock.patch("os.makedirs", side_effect=AssertionError("no mkdir")),                 mock.patch("subprocess.run", side_effect=AssertionError("no subprocess")):
            self.assertIsInstance(D.read_forwards()["forwards"], list)

    def test_accounts_get_a_bounded_orca_call(self):
        sys.path.insert(0, str(ROOT / ".claude" / "skills" / "auto-dev" / "tools"))
        import accounts as ACC
        with mock.patch.object(ACC.orca_adapter, "account_list", return_value={}) as al:
            D.read_accounts()
        self.assertEqual(al.call_args.kwargs["timeout"], D.ORCA_TIMEOUT_S)


    def test_review_jobs_are_listed_without_creating_dirs(self):
        self.assertEqual(D.read_reviews(), {"jobs": [], "unreadable": 0})
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "rq")))
        d = os.path.join(self.tmp, "rq", "jobs")
        os.makedirs(d)
        with open(os.path.join(d, "etask-12.json"), "w", encoding="utf-8") as f:
            json.dump({"id": "etask-12", "status": "awaiting_approval", "project_path": "g/etask", "iid": 12,
                       "title": "Fix NPE", "requests": [{"by": "dungpd15"}]}, f)
        with open(os.path.join(d, "bad.json"), "w", encoding="utf-8") as f:
            f.write("[1, 2")
        out = D.read_reviews()
        [j] = out["jobs"]
        self.assertEqual((j["mr"], j["status"], j["by"]), ("g/etask!12", "awaiting_approval", "dungpd15"))
        self.assertEqual(out["unreadable"], 1)                           # surfaced, not silently skipped


class SnapshotsTest(unittest.TestCase):
    def test_requests_never_build_and_fresh_is_coalesced(self):
        built = []
        gate = threading.Event()

        def build():
            built.append(threading.current_thread().name)
            gate.wait(2)
            return {"n": len(built)}
        snaps = D.Snapshots(build=build).start()
        gate.set()
        first = snaps.get()                                     # first request waits for the first build
        self.assertEqual(first["n"], 1)
        gate.clear()
        results = []
        threads = [threading.Thread(target=lambda: results.append(snaps.get(fresh=True, wait_s=3))) for _ in range(5)]
        [t.start() for t in threads]
        time.sleep(0.2)
        gate.set()
        [t.join(5) for t in threads]
        self.assertTrue(all(n == "dashboard-snapshots" for n in built))   # only the worker builds
        self.assertLessEqual(len(built), 3)                              # 5 fresh requests -> 1-2 rebuilds
        self.assertTrue(all(r["n"] >= 2 for r in results))

    def test_slow_build_does_not_block_a_normal_request(self):
        gate = threading.Event()
        calls = []

        def build():
            calls.append(1)
            if len(calls) > 1:
                gate.wait(5)                                    # the 2nd build hangs (slow orca)
            return {"n": len(calls)}
        snaps = D.Snapshots(build=build).start()
        snaps.get()
        snaps.wanted.set()                                      # a rebuild starts and hangs
        time.sleep(0.2)
        t0 = time.time()
        self.assertEqual(snaps.get()["n"], 1)                   # served the old snapshot at once
        self.assertLess(time.time() - t0, 0.5)
        t0 = time.time()
        snaps.get(fresh=True, wait_s=0.3)                       # fresh waits only its bound
        self.assertLess(time.time() - t0, 1.5)
        gate.set()

class HttpTest(Base):
    def setUp(self):
        super().setUp()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), D.Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        p = mock.patch.object(D, "read_accounts", return_value={"accounts": []})
        p.start()
        self.addCleanup(p.stop)
        snaps = mock.patch.object(D.Handler, "snapshots", D.Snapshots(build=D.collect).start())
        snaps.start()
        self.addCleanup(snaps.stop)

    def get(self, path, method="GET", host=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method)
        if host:
            req.add_header("Host", host)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()

    def test_page_and_state(self):
        code, h, body = self.get("/")
        self.assertEqual(code, 200)
        self.assertIn("text/html", h["Content-Type"])
        self.assertIn(b"Toolkit Dashboard", body)
        code, h, body = self.get("/api/state?fresh=1")
        self.assertEqual(code, 200)
        self.assertIn("store", json.loads(body)["sections"])
        self.assertEqual(h["Cache-Control"], "no-store")

    def test_foreign_host_write_methods_and_unknown_paths_refused(self):
        self.assertEqual(self.get("/api/state", host="evil.example:8765")[0], 403)
        self.assertEqual(self.get("/", host="localhost:1")[0], 200)
        for m in ("POST", "PUT", "DELETE"):
            self.assertEqual(self.get("/api/state", method=m)[0], 405)
        self.assertEqual(self.get("/../.env")[0], 404)

    def test_binds_loopback_only(self):
        with mock.patch.object(D, "ThreadingHTTPServer") as srv:
            srv.return_value.serve_forever.side_effect = KeyboardInterrupt
            D.serve(9999)
        self.assertEqual(srv.call_args[0][0], ("127.0.0.1", 9999))


class ApprovalsDirTest(Base):
    def test_rc_config_follows_toolkit_state_dir(self):
        sys.path.insert(0, str(ROOT / ".claude" / "skills" / "remote-control" / "tools"))
        import rc_config
        self.assertEqual(os.path.normpath(rc_config.temp_dir()), os.path.join(self.tmp, "tg_approvals"))


if __name__ == "__main__":
    unittest.main()
