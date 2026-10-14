"""Web dashboard (dev-automation/tools/dashboard.py): read-only, localhost-only, one failing section
never breaks the page; Store.reader never writes."""

import json
import os
import runpy
import sqlite3
import sys
import tempfile
import threading
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
        D._cache.update(at=0.0, data=None, accounts_at=0.0, accounts=None)


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
            data = D.collect(fresh=True)
        self.assertFalse(data["sections"]["daemons"]["ok"])
        self.assertIn("boom", data["sections"]["daemons"]["error"])
        self.assertTrue(data["sections"]["store"]["ok"])
        self.assertTrue(data["sections"]["accounts"]["ok"])
        json.dumps(data, default=str)                                   # always serialisable

    def test_snapshot_is_cached_briefly(self):
        with mock.patch.object(D, "read_accounts", return_value={}) as acc:
            D.collect(fresh=True)
            D.collect()
            self.assertEqual(acc.call_count, 1)
            D.collect(fresh=True)
            self.assertEqual(acc.call_count, 2)

    def test_review_jobs_are_listed_without_creating_dirs(self):
        self.assertEqual(D.read_reviews(), {"jobs": []})
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "rq")))
        d = os.path.join(self.tmp, "rq", "jobs")
        os.makedirs(d)
        with open(os.path.join(d, "etask-12.json"), "w", encoding="utf-8") as f:
            json.dump({"id": "etask-12", "status": "awaiting_approval", "project_path": "g/etask", "iid": 12,
                       "title": "Fix NPE", "requests": [{"by": "dungpd15"}]}, f)
        [j] = D.read_reviews()["jobs"]
        self.assertEqual((j["mr"], j["status"], j["by"]), ("g/etask!12", "awaiting_approval", "dungpd15"))


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
