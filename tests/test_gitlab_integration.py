#!/usr/bin/env python3
"""integrations.gitlab (checklist R.M4): per-project client, same surface as gitlab_api, timeouts and
network errors as error dicts; review_common uses it. A local HTTP server plays GitLab.
    python -m unittest discover -s tests
"""

import http.server
import json
import os
import runpy
import sys
import threading
import unittest
from unittest import mock

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
runpy.run_path(os.path.join(_ROOT, ".claude", "skills", "_runtime", "bootstrap.py"))["activate"]()
from agent_skill_toolkit.integrations import gitlab  # noqa: E402


class FakeGitLab(http.server.BaseHTTPRequestHandler):
    seen = []

    def _reply(self, code, obj):
        out = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def do_GET(self):
        FakeGitLab.seen.append(("GET", self.path, self.headers.get("PRIVATE-TOKEN")))
        if self.path == "/api/v4/projects/77/merge_requests/5":
            return self._reply(200, {"iid": 5, "state": "opened", "sha": "abc"})
        if self.path == "/api/v4/projects/77/empty":
            self.send_response(200)
            self.send_header("content-length", "0")
            self.end_headers()
            return None
        if self.path == "/api/v4/projects/grp%2Fproj":
            return self._reply(200, {"id": 77, "path_with_namespace": "grp/proj"})
        return self._reply(404, {"message": "404 Not Found"})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        FakeGitLab.seen.append(("POST", self.path, body))
        if "nope" in self.path:
            return self._reply(400, {"message": "line is not part of the diff"})
        return self._reply(201, {"id": 9, "body": body.get("body")})

    def log_message(self, *a):
        pass


class GitLabClientTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeGitLab)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.srv.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        FakeGitLab.seen.clear()
        mock.patch.dict(os.environ, {"GITLAB_URL": self.url, "GITLAB_PRIVATE_TOKEN": "tok-1"}).start()

    def tearDown(self):
        mock.patch.stopall()

    def test_reads_and_posts_for_one_project(self):
        gl = gitlab.Client(77)
        self.assertEqual(gl.get_merge_request(5)["sha"], "abc")
        self.assertEqual(gl.add_mr_comment(5, "hi")["id"], 9)
        self.assertEqual(FakeGitLab.seen[0], ("GET", "/api/v4/projects/77/merge_requests/5", "tok-1"))
        self.assertEqual(FakeGitLab.seen[1], ("POST", "/api/v4/projects/77/merge_requests/5/notes", {"body": "hi"}))
        self.assertEqual(gl._api("/x"), f"{self.url}/api/v4/projects/77/x")

    def test_errors_are_dicts_never_exceptions(self):
        res = gitlab.Client(77).get_merge_request(404)
        self.assertEqual((res["error"], res["status"]), (True, 404))
        with mock.patch.dict(os.environ, {"GITLAB_URL": "http://127.0.0.1:1"}):     # nothing listens
            res = gitlab.Client(77).get_merge_request(1)
        self.assertEqual((res["error"], res["status"]), (True, None))
        with self.assertRaises(ValueError):
            gitlab.Client("")

    def test_empty_body_and_uncertain_writes(self):                    # Codex R.M4 R1
        gl = gitlab.Client(77)
        res = gl._request(gl._api("/empty"))
        self.assertEqual((res["error"], res["status"], res["uncertain"]), (True, "bad-body", False))
        with mock.patch.dict(os.environ, {"GITLAB_URL": "http://127.0.0.1:1"}):
            w = gitlab.Client(77).add_mr_comment(1, "x")
            r = gitlab.Client(77).get_merge_request(1)
        self.assertTrue(w["uncertain"])                                # a write we cannot know about
        self.assertFalse(r["uncertain"])                               # a read is just an error
        rej = gl._request(gl._api("/nope"), method="POST", data={"a": 1})
        self.assertEqual(rej.get("uncertain"), None)                   # HTTP rejection: definite

    def test_project_info_quotes_paths(self):
        self.assertEqual(gitlab.project_info("grp/proj")["id"], 77)

    def test_ssl_verify_switch(self):
        import ssl
        with mock.patch.dict(os.environ, {"SSL_VERIFY": "false"}):
            self.assertEqual(gitlab._ssl_context().verify_mode, ssl.CERT_NONE)
        with mock.patch.dict(os.environ, {"SSL_VERIFY": "true"}):
            self.assertEqual(gitlab._ssl_context().verify_mode, ssl.CERT_REQUIRED)

    def test_review_common_goes_through_the_client(self):
        sys.path.insert(0, os.path.join(_ROOT, ".claude", "skills", "mr-review", "tools"))
        import review_common as rc
        self.assertEqual(rc.gitlab_mr(77, 5)["iid"], 5)
        self.assertEqual(rc.gitlab_project_info("grp/proj")["path_with_namespace"], "grp/proj")
        with rc.gitlab_project(77) as gl:
            self.assertIsInstance(gl, gitlab.Client)
        with self.assertRaises(RuntimeError):
            rc.gitlab_mr(77, 404)

    def test_parallel_clients_do_not_share_a_project(self):
        out = {}

        def go(pid):
            out[pid] = gitlab.Client(pid)._api("")
        ts = [threading.Thread(target=go, args=(p,)) for p in ("11", "22")]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertTrue(out["11"].endswith("/projects/11") and out["22"].endswith("/projects/22"))


if __name__ == "__main__":
    unittest.main()
