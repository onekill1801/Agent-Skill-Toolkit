#!/usr/bin/env python3
"""gitlab_api.py update-mr: PUT chỉ field được truyền, đọc mô tả từ file/stdin, báo verified; guard hook hỏi trước.
    python -m unittest tests.test_gitlab_api_update_mr
"""

import http.server
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from unittest import mock

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_TOOLS = os.path.join(_ROOT, ".claude", "skills", "dev-automation", "tools")
_HOOKS = os.path.join(_ROOT, ".claude", "hooks")
sys.path.insert(0, _TOOLS)
import gitlab_api  # noqa: E402


class FakeGitLab(http.server.BaseHTTPRequestHandler):
    seen = []
    trim = False   # giả GitLab bỏ khoảng trắng cuối mô tả

    def do_PUT(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])) or b"{}")
        FakeGitLab.seen.append(("PUT", self.path, body))
        mr = {"iid": 7, "web_url": "http://x/mr/7", "title": "old", "description": "old", "target_branch": "main"}
        mr.update(body)
        if FakeGitLab.trim and "description" in body:
            mr["description"] = body["description"].rstrip()
        out = json.dumps(mr).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


class UpdateMrTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.HTTPServer(("127.0.0.1", 0), FakeGitLab)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        FakeGitLab.seen = []
        FakeGitLab.trim = False
        env = {"GITLAB_URL": f"http://127.0.0.1:{self.srv.server_port}", "GITLAB_PRIVATE_TOKEN": "t",
               "GITLAB_PROJECT_ID": "5401", "SSL_VERIFY": "true"}
        p = mock.patch.dict(os.environ, env)
        p.start()
        self.addCleanup(p.stop)

    def test_sends_only_given_fields(self):
        r = gitlab_api.update_mr_cli(["7", "--title", "Feat: x"])
        self.assertEqual(FakeGitLab.seen, [("PUT", "/api/v4/projects/5401/merge_requests/7", {"title": "Feat: x"})])
        self.assertEqual(r["updated"], ["title"])
        self.assertTrue(r["verified"])

    def test_description_from_file_and_trailing_whitespace_still_verified(self):
        FakeGitLab.trim = True
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as fh:
            fh.write("## Vì sao\nmô tả tiếng Việt\n\n")
        self.addCleanup(os.unlink, fh.name)
        r = gitlab_api.update_mr_cli(["7", "--description-file", fh.name, "--target-branch", "merge/rfc"])
        sent = FakeGitLab.seen[0][2]
        self.assertEqual(sent, {"description": "## Vì sao\nmô tả tiếng Việt\n\n", "target_branch": "merge/rfc"})
        self.assertEqual(r["updated"], ["description", "target_branch"])
        self.assertTrue(r["verified"])

    def test_description_from_stdin(self):
        with mock.patch.object(sys, "stdin", io.StringIO("từ stdin")):
            gitlab_api.update_mr_cli(["7", "--description-file", "-"])
        self.assertEqual(FakeGitLab.seen[0][2], {"description": "từ stdin"})

    def test_nothing_to_update_is_error_without_request(self):
        r = gitlab_api.update_mr_cli(["7"])
        self.assertTrue(r["error"])
        self.assertEqual(FakeGitLab.seen, [])

    def test_missing_file_is_error_without_request(self):
        r = gitlab_api.update_mr_cli(["7", "--description-file", "/khong/ton/tai.md"])
        self.assertTrue(r["error"])
        self.assertEqual(FakeGitLab.seen, [])

    def test_description_and_file_are_exclusive(self):
        with self.assertRaises(SystemExit), mock.patch.object(sys, "stderr", io.StringIO()):
            gitlab_api.update_mr_cli(["7", "--description", "a", "--description-file", "b"])


class GuardTest(unittest.TestCase):
    def test_update_mr_is_a_guarded_write(self):
        sys.path.insert(0, _HOOKS)
        import guard_write
        for cmd in ("python gitlab_api.py update-mr 7 --title x",
                    "python agentkit.py gitlab_api update-mr 7 --description-file d.md"):
            self.assertEqual(guard_write.decide({"tool_name": "Bash", "tool_input": {"command": cmd}})[0], "ask", cmd)


if __name__ == "__main__":
    unittest.main()
