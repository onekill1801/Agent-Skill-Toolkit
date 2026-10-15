"""Editable settings (F.17): core.env.set_values, workflows.settings catalog/apply, the dashboard's
POST /api/settings (edit key, same-origin JSON only) and the guard rules."""

import json
import os
import runpy
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
from agent_skill_toolkit.core import env as E  # noqa: E402
from agent_skill_toolkit.core import store as St  # noqa: E402
from agent_skill_toolkit.workflows import settings as S  # noqa: E402

sys.path.insert(0, str(ROOT / ".claude" / "skills" / "dev-automation" / "tools"))
sys.path.insert(0, str(ROOT / ".claude" / "hooks"))
import dashboard as D  # noqa: E402

ENV_TEXT = ("# comment line\r\n"
            "GITLAB_PRIVATE_TOKEN=glpat-SECRETSECRETSECRET12\r\n"
            "REVIEW_AUTO_POST=shadow   # off|shadow|auto\r\n"
            "# REVIEW_AUTO_POST_PROJECTS=old\r\n"
            "FCHAT_WATCH_TIMEOUT=1800\r\n"
            "FCHAT_WATCH_TIMEOUT=999\r\n"
            "ETASK_PAT_TOKEN=x\r\n")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.env = os.path.join(self.tmp, ".env")
        with open(self.env, "w", encoding="utf-8", newline="") as f:
            f.write(ENV_TEXT)
        p = mock.patch.dict(os.environ, {"TOOLKIT_ENV_FILE": self.env, "TOOLKIT_STATE_DIR": self.tmp,
                                         "TOOLKIT_STORE": os.path.join(self.tmp, "t.db")})
        p.start()
        self.addCleanup(p.stop)
        for k in [k for k in os.environ if k.startswith(("REVIEW_", "FCHAT_"))]:
            os.environ.pop(k)                 # restored by p.stop(): patch.dict snapshots the whole env

    def text(self):
        with open(self.env, encoding="utf-8", newline="") as f:
            return f.read()


class EnvWriteTest(Base):
    def test_in_place_keeps_comment_crlf_and_everything_else(self):
        old = E.set_values(self.env, {"REVIEW_AUTO_POST": "auto", "REVIEW_AUTO_POST_PROJECTS": "etask"})
        self.assertEqual(old, {"REVIEW_AUTO_POST": "shadow", "REVIEW_AUTO_POST_PROJECTS": None})
        t = self.text()
        self.assertIn("REVIEW_AUTO_POST=auto   # off|shadow|auto\r\n", t)
        self.assertIn("# REVIEW_AUTO_POST_PROJECTS=old\r\n", t)              # commented line untouched
        self.assertTrue(t.endswith("REVIEW_AUTO_POST_PROJECTS=etask\r\n"))    # appended, same line ending
        self.assertIn("GITLAB_PRIVATE_TOKEN=glpat-SECRETSECRETSECRET12\r\n", t)
        self.assertEqual(E.parse(t)["REVIEW_AUTO_POST"], "auto")

    def test_first_occurrence_is_the_one_changed(self):
        E.set_values(self.env, {"FCHAT_WATCH_TIMEOUT": "60"})
        t = self.text()
        self.assertIn("FCHAT_WATCH_TIMEOUT=60\r\n", t)
        self.assertIn("FCHAT_WATCH_TIMEOUT=999\r\n", t)
        self.assertEqual(E.parse(t)["FCHAT_WATCH_TIMEOUT"], "60")

    def test_values_a_reader_would_misread_are_refused_and_nothing_written(self):
        for bad in ("a#b", "'x", 'x"', "a\nB=1", "a\rb", "a b", " x", "x ", "a\x0bb", "x" * 501):
            with self.assertRaises(ValueError, msg=repr(bad)):
                E.set_values(self.env, {"REVIEW_AUTO_POST": bad})
        with self.assertRaises(ValueError):
            E.set_values(self.env, {"BAD KEY": "x"})
        self.assertEqual(self.text(), ENV_TEXT)

    def test_empty_value_with_comment_reads_back_empty(self):
        E.set_values(self.env, {"REVIEW_AUTO_POST": ""})
        self.assertEqual(E.parse(self.text())["REVIEW_AUTO_POST"], "")

    def test_expected_is_checked_under_the_lock(self):                         # Codex settings R1 #1
        with self.assertRaises(E.StaleValue):
            E.set_values(self.env, {"REVIEW_AUTO_POST": "auto"}, expected={"REVIEW_AUTO_POST": "off"})
        E.set_values(self.env, {"REVIEW_AUTO_POST_PROJECTS": "x"}, expected={"REVIEW_AUTO_POST_PROJECTS": ""})
        self.assertEqual(E.parse(self.text())["REVIEW_AUTO_POST_PROJECTS"], "x")

    def test_replace_retries_while_a_reader_holds_the_file(self):              # Codex settings R1 #2
        real, calls = os.replace, []

        def flaky(a, b):
            calls.append(1)
            if len(calls) < 3:
                raise PermissionError("in use")
            return real(a, b)
        with mock.patch.object(E.os, "replace", side_effect=flaky), mock.patch.object(E.time, "sleep"):
            E.set_values(self.env, {"REVIEW_AUTO_POST": "off"})
        self.assertEqual(len(calls), 3)
        self.assertEqual(E.parse(self.text())["REVIEW_AUTO_POST"], "off")
        self.assertEqual([f for f in os.listdir(self.tmp) if ".tmp-" in f], [])

    def test_file_with_odd_line_separators_is_not_edited(self):              # Codex settings R2 #1
        for sep in (" ", "\r", "\x0c", "\x85"):
            raw = f"A=one{sep}REVIEW_AUTO_POST=shadow\n"
            with open(self.env, "w", encoding="utf-8", newline="") as f:
                f.write(raw)
            with self.assertRaises(ValueError, msg=repr(sep)):
                E.set_values(self.env, {"REVIEW_AUTO_POST": "auto"})
            self.assertEqual(self.text(), raw)

    def test_keyword_phrases_stay_phrases(self):                              # Codex settings R2 #2
        sys.path.insert(0, str(ROOT / ".claude" / "skills" / "tchat-automation" / "tools"))
        import group_watch
        with mock.patch.object(group_watch.rccfg, "get", return_value="lên dev, merge request,review"):
            self.assertEqual(group_watch._keywords(), ["lên dev", "merge request", "review"])

    def test_file_without_trailing_newline(self):
        with open(self.env, "w", encoding="utf-8", newline="") as f:
            f.write("A=1")
        E.set_values(self.env, {"B": "2"})
        self.assertEqual(E.parse(self.text()), {"A": "1", "B": "2"})


class CatalogTest(Base):
    def test_validate(self):
        ok = {"REVIEW_AUTO_POST": "auto", "REVIEW_CODEGRAPH": "0", "FCHAT_WATCH_TIMEOUT": "600",
              "FCHAT_WATCH_AUTO_APPROVE": "read,file", "REVIEW_AUTO_POST_PROJECTS": "etask,idaas/etask,5401",
              "FCHAT_REVIEW_ALLOWED": "*", "FCHAT_WATCH_GROUP": "6883495a299a0f54343608e3",
              "FCHAT_WATCH_KEYWORDS": "review,lên dev", "capacity": "10", "FCHAT_REVIEW_ALLOWED ": None}
        for k, v in ok.items():
            if v is not None:
                self.assertEqual(S.validate(k, v), "", (k, v))
        bad = {"REVIEW_AUTO_POST": "yes", "REVIEW_CODEGRAPH": "true", "FCHAT_WATCH_TIMEOUT": "5",
               "FCHAT_WATCH_AUTO_APPROVE": "read,danger", "capacity": "-1", "FCHAT_WATCH_GROUP": "abc",
               "FCHAT_REVIEW_ALLOWED": "a,,b", "GITLAB_PRIVATE_TOKEN": "x", "SSL_VERIFY": "false"}
        for k, v in bad.items():
            self.assertNotEqual(S.validate(k, v), "", (k, v))

    def test_snapshot_never_contains_a_secret_value(self):
        snap = S.snapshot()
        blob = json.dumps(snap)
        self.assertNotIn("SECRETSECRET", blob)
        self.assertIn({"key": "GITLAB_PRIVATE_TOKEN", "set": True}, snap["secrets"])
        self.assertTrue(any("ETASK_" in w for w in snap["warnings"]))
        it = next(i for i in snap["items"] if i["key"] == "REVIEW_AUTO_POST")
        self.assertEqual((it["value"], it["set"]), ("shadow", True))
        self.assertNotIn("GITLAB_PRIVATE_TOKEN", [i["key"] for i in snap["items"]])

    def test_effective_group_auto_approve_is_the_union(self):                 # Codex settings R3 #1
        with open(self.env, "a", encoding="utf-8") as f:
            f.write("FCHAT_WATCH_AUTO_APPROVE=read\nTELEGRAM_AUTO_APPROVE=read,bash\n")
        w = [x for x in S.snapshot()["warnings"] if "group TChat" in x]
        self.assertEqual(len(w), 1)
        self.assertIn("bash, read", w[0])
        tg = next(i for i in S.snapshot()["items"] if i["key"] == "FCHAT_WATCH_AUTO_APPROVE")
        self.assertIn("CỘNG THÊM", tg["help"])

    def test_apply_env_and_store_with_audit_and_restart_hint(self):
        out = S.apply({"REVIEW_AUTO_POST": "auto", "capacity": "7"}, by="test",
                      expected={"REVIEW_AUTO_POST": "shadow"})
        self.assertEqual(out["changed"]["REVIEW_AUTO_POST"], {"old": "shadow", "new": "auto"})
        self.assertIn("Coordinator", out["restart"])
        with St.Store() as st:
            self.assertEqual(st.capacity(), 7)
        with open(os.path.join(self.tmp, "settings_audit.jsonl"), encoding="utf-8") as f:
            rec = json.loads(f.readlines()[-1])
        self.assertEqual(rec["by"], "test")

    def test_first_store_edit_without_a_store_is_not_stale(self):              # Codex settings R1 #3
        self.assertFalse(os.path.exists(os.environ["TOOLKIT_STORE"]))
        it = next(i for i in S.snapshot()["items"] if i["key"] == "capacity")
        self.assertEqual((it["value"], it["set"]), (str(St.max_running_default()), True))
        S.apply({"capacity": "4"}, by="t", expected={"capacity": it["value"]})
        with St.Store() as st:
            self.assertEqual(st.capacity(), 4)

    def test_stale_or_invalid_change_writes_nothing(self):
        with self.assertRaises(RuntimeError):
            S.apply({"REVIEW_AUTO_POST": "auto"}, by="t", expected={"REVIEW_AUTO_POST": "off"})
        with self.assertRaises(ValueError):
            S.apply({"REVIEW_AUTO_POST": "auto", "FCHAT_WATCH_TIMEOUT": "1"}, by="t")
        with self.assertRaises(ValueError):
            S.apply({"GITLAB_PRIVATE_TOKEN": "x"}, by="t")
        self.assertEqual(self.text(), ENV_TEXT)


class HttpTest(Base):
    def setUp(self):
        super().setUp()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), D.Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        k = mock.patch.object(D.Handler, "edit_key", "k" * 32)
        k.start()
        self.addCleanup(k.stop)
        snaps = mock.patch.object(D.Handler, "snapshots", mock.Mock())
        snaps.start()
        self.addCleanup(snaps.stop)

    def req(self, path, body=None, headers=None, method=None):
        data = json.dumps(body).encode() if isinstance(body, dict) else body
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, method=method)
        for k, v in (headers or {}).items():
            r.add_header(k, v)
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, json.loads(resp.read() or b"null")
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw)
            except ValueError:
                return e.code, raw

    GOOD = {"Content-Type": "application/json", "X-Dashboard-Key": "k" * 32}

    def test_get_settings_has_no_secret_and_no_key(self):
        code, out = self.req("/api/settings")
        self.assertEqual(code, 200)
        blob = json.dumps(out)
        self.assertNotIn("SECRETSECRET", blob)
        self.assertNotIn("k" * 32, blob)
        self.assertTrue(out["editable"])

    def test_post_needs_the_key_same_origin_and_json(self):
        body = {"changes": {"REVIEW_AUTO_POST": "auto"}}
        cases = [{"Content-Type": "application/json"},                                   # no key
                 {**self.GOOD, "X-Dashboard-Key": "x" * 32},                              # wrong key
                 {**self.GOOD, "Content-Type": "text/plain"},                             # form-able type
                 {**self.GOOD, "Origin": "http://evil.example"},
                 {**self.GOOD, "Sec-Fetch-Site": "cross-site"},
                 {**self.GOOD, "Host": "evil.example:1"}]
        for h in cases:
            code, _ = self.req("/api/settings", body, h)
            self.assertEqual(code, 403, h)
        self.assertEqual(self.text(), ENV_TEXT)
        self.assertEqual(self.req("/api/state", body, self.GOOD)[0], 405)

    def test_post_applies_valid_changes_and_refuses_invalid(self):
        code, out = self.req("/api/settings", {"changes": {"REVIEW_AUTO_POST": "auto"},
                                               "expected": {"REVIEW_AUTO_POST": "shadow"}},
                             {**self.GOOD, "Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual((code, out["ok"]), (200, True))
        self.assertEqual(E.parse(self.text())["REVIEW_AUTO_POST"], "auto")
        code, out = self.req("/api/settings", {"changes": {"GITLAB_PRIVATE_TOKEN": "x"}}, self.GOOD)
        self.assertEqual((code, out["ok"]), (400, False))
        code, _ = self.req("/api/settings", b"{" * 20000, self.GOOD)
        self.assertEqual(code, 413)
        for root in (b"null", b"[]", b'"x"', b"{bad"):                       # Codex settings R1 #5
            code, out = self.req("/api/settings", root, self.GOOD)
            self.assertEqual((code, out["ok"]), (400, False), root)

    def test_read_only_server_has_no_key(self):
        with mock.patch.object(D.Handler, "edit_key", ""):
            self.assertEqual(self.req("/api/settings", {"changes": {"REVIEW_AUTO_POST": "auto"}}, self.GOOD)[0], 403)
            self.assertFalse(self.req("/api/settings")[1]["editable"])


class GuardTest(unittest.TestCase):
    def test_settings_writes_ask_and_headless_is_denied(self):
        import guard_write
        for cmd in ("python toolkit_settings.py set REVIEW_AUTO_POST auto",
                    "python agentkit.py toolkit_settings set capacity 3",
                    "curl -X POST http://127.0.0.1:8765/api/settings -d {}"):
            self.assertEqual(guard_write.decide({"tool_name": "Bash", "tool_input": {"command": cmd}})[0], "ask", cmd)
            with mock.patch.dict(os.environ, {"TOOLKIT_HEADLESS": "1"}):
                self.assertEqual(guard_write.decide({"tool_name": "Bash", "tool_input": {"command": cmd}})[0],
                                 "deny", cmd)
        self.assertIsNone(guard_write.decide({"tool_name": "Bash",
                                              "tool_input": {"command": "python toolkit_settings.py list"}})[0])


if __name__ == "__main__":
    unittest.main()
