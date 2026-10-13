#!/usr/bin/env python3
"""code_context (checklist F.12): CodeGraph when its index is fresh, grep otherwise; stack traces.
    python -m unittest discover -s tests
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(_ROOT, ".claude", "skills", "dev-automation", "tools"))
sys.path.insert(0, os.path.join(_ROOT, ".claude", "skills", "auto-dev", "tools"))

import code_context as cc  # noqa: E402

FILES = {
    "src/main/java/com/acme/billing/InvoiceService.java":
        "package com.acme.billing;\npublic class InvoiceService {\n"
        "  public Invoice createInvoice(Order o) {\n    return totals(o);\n  }\n}\n",
    "src/main/java/com/acme/legacy/InvoiceService.java":
        "package com.acme.legacy;\npublic class InvoiceService {}\n",
    "src/main/java/com/acme/api/InvoiceController.java":
        "package com.acme.api;\nclass InvoiceController {\n  void post() { service.createInvoice(order); }\n}\n",
    "src/test/java/com/acme/billing/InvoiceServiceTest.java":
        "class InvoiceServiceTest { void t() { new InvoiceService().createInvoice(null); } }\n",
    ".claude/tools/helper.py": "def create_invoice_py():\n    return 1\n\nx = create_invoice_py()\n",
    ".env": "createInvoice=SECRET_TOKEN_VALUE\n",
    "node_modules/lib/x.js": "function createInvoice() {}\n",
}


class Base(unittest.TestCase):
    def setUp(self):
        self.repo = tempfile.mkdtemp(prefix="cc-")
        for rel, body in FILES.items():
            p = os.path.join(self.repo, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                f.write(body)

    def tearDown(self):
        mock.patch.stopall()

    def no_codegraph(self):
        mock.patch.object(cc.cg, "available", return_value=False).start()


class GrepFallbackTest(Base):
    def test_locate_and_callers_by_text(self):
        self.no_codegraph()
        loc = cc.locate(self.repo, "createInvoice")
        self.assertEqual(loc["source"], "grep")
        self.assertIn("not installed", loc["fallback"])
        self.assertEqual([d["file"] for d in loc["definitions"]],
                         ["src/main/java/com/acme/billing/InvoiceService.java"])
        callers = {r["file"] for r in cc.callers(self.repo, "createInvoice")["callers"]}
        self.assertEqual(callers, {"src/main/java/com/acme/api/InvoiceController.java",
                                   "src/test/java/com/acme/billing/InvoiceServiceTest.java"})

    def test_hidden_code_dirs_are_searched_but_secrets_and_vendor_are_not(self):
        self.no_codegraph()
        self.assertEqual(cc.locate(self.repo, "create_invoice_py")["definitions"][0]["file"], ".claude/tools/helper.py")
        everywhere = json.dumps(cc.impact(self.repo, "createInvoice"))
        self.assertNotIn("SECRET_TOKEN_VALUE", everywhere)
        self.assertNotIn("node_modules", everywhere)

    def test_affected_tests_by_name(self):
        self.no_codegraph()
        out = cc.affected(self.repo, ["src/main/java/com/acme/billing/InvoiceService.java"])
        self.assertEqual(out["tests"], ["src/test/java/com/acme/billing/InvoiceServiceTest.java"])

    def test_bad_symbol_is_refused(self):
        self.no_codegraph()
        with self.assertRaises(ValueError):
            cc.locate(self.repo, "x; rm -rf /")


class CodegraphTest(Base):
    def setUp(self):
        super().setUp()
        os.makedirs(os.path.join(self.repo, ".codegraph"))
        open(os.path.join(self.repo, ".codegraph", "codegraph.db"), "w").close()
        mock.patch.object(cc.cg, "available", return_value=True).start()
        self.calls = []
        self.pending = {"added": 0, "modified": 0, "removed": 0}
        self.answers = {}
        mock.patch.object(cc.cg, "_run", side_effect=self.fake_run).start()

    def status(self):
        return {"initialized": True, "pendingChanges": self.pending, "worktreeMismatch": None,
                "index": {"state": "complete", "reindexRecommended": False}}

    def fake_run(self, args, timeout=120, **kw):
        self.calls.append(args[0])
        if args[0] == "status":
            out = json.dumps(self.status())
        elif args[0] == "sync":
            self.pending = {"added": 0, "modified": 0, "removed": 0}
            out = "synced"
        else:
            out = self.answers.get(args[0], "")
        return subprocess.CompletedProcess(args, 0, out, "")

    def test_fresh_index_answers_from_codegraph(self):
        self.answers["callers"] = json.dumps({"ambiguous": False, "definitions": [{"callers": [
            {"name": "post", "kind": "method", "filePath": "src\\main\\java\\com\\acme\\api\\InvoiceController.java",
             "startLine": 3}]}]})
        out = cc.callers(self.repo, "createInvoice")
        self.assertEqual(out["source"], "codegraph")
        self.assertEqual(out["callers"][0]["file"], "src/main/java/com/acme/api/InvoiceController.java")
        self.assertNotIn("sync", self.calls)

    def test_stale_index_is_synced_first(self):
        self.pending = {"added": 2, "modified": 1, "removed": 0}
        self.answers["callers"] = json.dumps({"definitions": []})
        out = cc.callers(self.repo, "createInvoice")
        self.assertEqual((out["source"], out.get("synced")), ("codegraph", True))
        self.assertEqual(self.calls[:3], ["status", "sync", "status"])

    def test_stale_without_sync_falls_back_to_grep(self):
        self.pending = {"added": 0, "modified": 4, "removed": 0}
        out = cc.locate(self.repo, "createInvoice", sync=False)
        self.assertEqual(out["source"], "grep")
        self.assertIn("4 pending", out["fallback"])
        self.assertNotIn("sync", self.calls)

    def test_not_found_text_falls_back_with_the_reason(self):
        self.answers["callers"] = 'ℹ Symbol "createInvoice" not found — did you mean: create?'
        out = cc.callers(self.repo, "createInvoice")
        self.assertEqual(out["source"], "grep")
        self.assertIn("not found", out["fallback"])
        self.assertTrue(out["callers"])

    def test_definitions_many_is_codegraph_only(self):
        self.answers["query"] = json.dumps([
            {"node": {"name": "createInvoice", "kind": "method", "qualifiedName": "InvoiceService.createInvoice",
                      "filePath": "src/main/java/com/acme/billing/InvoiceService.java", "startLine": 3}},
            {"node": {"name": "createInvoice", "kind": "import", "filePath": "x.java"}}])
        got = cc.definitions_many(self.repo, ["createInvoice", "bad name"])
        self.assertEqual(list(got), ["createInvoice"])
        self.assertEqual(len(got["createInvoice"]), 1)
        self.pending = {"added": 1, "modified": 0, "removed": 0}
        self.assertEqual(cc.definitions_many(self.repo, ["createInvoice"]), {})


class TraceTest(Base):
    def test_java_trace_keeps_repo_frames_and_resolves_packages(self):
        text = ("java.lang.NullPointerException\n"
                "\tat com.acme.billing.InvoiceService.createInvoice(InvoiceService.java:4)\n"
                "\tat com.acme.api.InvoiceController.post(InvoiceController.java:3)\n"
                "\tat org.springframework.web.Servlet.service(Servlet.java:99)\n")
        out = cc.trace(self.repo, text)
        self.assertEqual(out["frames"], [
            {"file": "src/main/java/com/acme/billing/InvoiceService.java", "line": 4,
             "symbol": "com.acme.billing.InvoiceService.createInvoice"},
            {"file": "src/main/java/com/acme/api/InvoiceController.java", "line": 3,
             "symbol": "com.acme.api.InvoiceController.post"}])

    def test_python_trace_with_absolute_paths(self):
        text = (f'Traceback (most recent call last):\n  File "{self.repo}\\.claude\\tools\\helper.py", line 2, in '
                'create_invoice_py\n  File "C:\\Python\\lib\\json\\decoder.py", line 9, in decode\n')
        out = cc.trace(self.repo, text)
        self.assertEqual(out["files"], [".claude/tools/helper.py"])

    def test_fix_loop_hands_the_fixer_code_locations(self):
        import fix_loop
        locs = fix_loop._code_locations(self.repo, "\tat com.acme.billing.InvoiceService.createInvoice(InvoiceService.java:4)")
        self.assertIn("src/main/java/com/acme/billing/InvoiceService.java:4", locs)
        self.assertEqual(fix_loop._code_locations(self.repo, "no trace here"), "")
        seen = {}
        with mock.patch.object(fix_loop.agent_runner, "run_turn", side_effect=lambda p, **k: seen.update(p=p) or "ok"), \
                mock.patch.object(fix_loop.run_log, "_runs_dir", return_value=self.repo):
            fix_loop._spawn_fixer({"run_id": "r", "clone_dir": self.repo, "backend": "claude"},
                                  {"kind": "unit", "context": "at com.acme.api.InvoiceController.post(InvoiceController.java:3)"})
        self.assertIn("<code_locations>\nsrc/main/java/com/acme/api/InvoiceController.java:3", seen["p"])


class Round1FixesTest(Base):
    """Codex F.12 round 1."""

    def test_symlinked_secret_is_never_read(self):                           # #1
        self.no_codegraph()
        outside = tempfile.mkdtemp(prefix="cc-out-")
        secret = os.path.join(outside, "credentials")
        with open(secret, "w") as f:
            f.write("createInvoice = 'AKIA_SECRET'\n")
        try:
            os.symlink(secret, os.path.join(self.repo, "src", "config.py"))
        except (OSError, NotImplementedError):
            self.skipTest("symlinks need privileges on this machine")
        self.assertNotIn("AKIA_SECRET", json.dumps(cc.callers(self.repo, "createInvoice")))

    def test_links_and_escaping_real_paths_are_skipped(self):                # #1 without symlink rights
        self.no_codegraph()
        ctrl = os.path.join(self.repo, "src", "main", "java", "com", "acme", "api", "InvoiceController.java")
        real_islink, real_realpath = os.path.islink, os.path.realpath
        with mock.patch.object(cc.os.path, "islink", side_effect=lambda p: os.path.normcase(p) == os.path.normcase(ctrl)
                               or real_islink(p)):
            files = {r for r, _ in cc._iter_code(self.repo)}
        self.assertNotIn("src/main/java/com/acme/api/InvoiceController.java", files)
        outside = os.path.join(tempfile.gettempdir(), "elsewhere", "secret.py")
        with mock.patch.object(cc.os.path, "realpath",
                               side_effect=lambda p: outside if os.path.normcase(p) == os.path.normcase(ctrl)
                               else real_realpath(p)):
            files = {r for r, _ in cc._iter_code(self.repo)}
        self.assertNotIn("src/main/java/com/acme/api/InvoiceController.java", files)
        self.assertIn("src/main/java/com/acme/billing/InvoiceService.java", files)

    def test_library_frames_are_not_attributed_to_repo_files(self):          # #2
        for rel, body in {"src/main/java/com/acme/Servlet.java": "package com.acme;\nclass Servlet {}\n",
                          "json/decoder.py": "def decode():\n    pass\n"}.items():
            p = os.path.join(self.repo, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w") as f:
                f.write(body)
        text = ("\tat org.springframework.web.Servlet.service(Servlet.java:99)\n"
                '  File "C:\\Python\\lib\\json\\decoder.py", line 9, in decode\n'
                "\tat com.acme.Servlet.run(Servlet.java:2)\n")
        self.assertEqual(cc.trace(self.repo, text)["frames"],
                         [{"file": "src/main/java/com/acme/Servlet.java", "line": 2, "symbol": "com.acme.Servlet.run"}])

    def test_windows_node_paths_with_spaces(self):                           # #5
        p = os.path.join(self.repo, "web app", "src", "app.js")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write("function boom() {}\n")
        win = p.replace("/", "\\")
        text = f"TypeError: x\n    at boom ({win}:10:5)\n    at Object.<anonymous> (node:internal/x.js:1:1)\n"
        self.assertEqual(cc.trace(self.repo, text)["files"], ["web app/src/app.js"])

    def test_round2_trace_edges(self):                                       # Codex F.12 R2
        p = os.path.join(self.repo, "decoder.py")
        with open(p, "w") as f:
            f.write("def d():\n    pass\n")
        self.assertEqual(cc.trace(self.repo, 'File "../decoder.py", line 3, in d\n')["frames"], [])
        inner = "\tat com.acme.billing.InvoiceService$Lines.sum(InvoiceService.java:4)\n"
        self.assertEqual(cc.trace(self.repo, inner)["files"], ["src/main/java/com/acme/billing/InvoiceService.java"])

    def test_grep_never_claims_a_qualified_identity(self):                   # Codex F.12 R2
        self.no_codegraph()
        out = cc.locate(self.repo, "Other.createInvoice")
        self.assertEqual(out["definitions"], [])
        self.assertFalse(out["resolved"])
        self.assertTrue(out["candidates"])

    def test_no_trace_means_no_repo_walk(self):                              # #6
        with mock.patch.object(cc, "_iter_code", side_effect=AssertionError("walked")):
            self.assertEqual(cc.trace(self.repo, "plain failure text, no frames")["frames"], [])


class Round1CodegraphTest(CodegraphTest):
    def test_query_failure_falls_back_to_grep(self):                         # #3
        orig = self.fake_run

        def boom(args, timeout=120, **kw):
            if args[0] == "callers":
                raise subprocess.TimeoutExpired(args, timeout)
            return orig(args, timeout, **kw)
        cc.cg._run.side_effect = boom
        out = cc.callers(self.repo, "createInvoice")
        self.assertEqual(out["source"], "grep")
        self.assertIn("TimeoutExpired", out["fallback"])

    def test_ambiguity_and_qualified_names(self):                            # #4
        self.answers["impact"] = json.dumps({"ambiguous": True, "definitions": [{"affected": []}, {"affected": []}]})
        out = cc.impact(self.repo, "createInvoice")
        self.assertEqual((out["ambiguous"], out["definitions"]), (True, 2))
        self.answers["query"] = json.dumps([
            {"node": {"name": "createInvoice", "kind": "method", "qualifiedName": "LegacyService.createInvoice",
                      "filePath": "a.java", "startLine": 1}},
            {"node": {"name": "createInvoice", "kind": "method", "qualifiedName": "InvoiceService.createInvoice",
                      "filePath": "b.java", "startLine": 2}}])
        out = cc.locate(self.repo, "InvoiceService.createInvoice")
        self.assertEqual(([d["file"] for d in out["definitions"]], out["ambiguous"]), (["b.java"], False))
        self.assertTrue(cc.locate(self.repo, "createInvoice")["ambiguous"])
        out = cc.locate(self.repo, "Other.createInvoice")
        self.assertEqual(out["source"], "grep")
        self.assertIn("exactly", out["fallback"])

    def test_malformed_nested_answers_fall_back(self):                       # Codex F.12 R2
        self.answers["query"] = json.dumps([None, {"node": None}, {"node": {"name": 3}}])
        self.assertEqual(cc.locate(self.repo, "createInvoice")["source"], "grep")
        self.answers["callers"] = json.dumps({"definitions": [None, {"callers": [None, {"filePath": 5}]}]})
        self.assertEqual(cc.callers(self.repo, "createInvoice")["callers"], [])


class GroundingTest(Base):
    def test_scout_ranks_the_defining_file_first(self):
        import grounding
        defs = {"InvoiceService": [{"file": "src/main/java/com/acme/legacy/InvoiceService.java", "line": 2}]}
        art = os.path.join(self.repo, "scout.md")
        with mock.patch.object(cc, "definitions_many", return_value=defs), \
                mock.patch.object(grounding, "_scout_artifact_path", return_value=art):
            out = grounding.cmd_scout(type("A", (), {"root": self.repo, "desc": "", "desc_file": "",
                                                     "keywords": "InvoiceService", "run": "t-scout"})())
        self.assertEqual(out["code_context"], "codegraph")
        top = out["candidates"][0]
        self.assertEqual(top["path"].replace("\\", "/"), "src/main/java/com/acme/legacy/InvoiceService.java")
        self.assertEqual(top["defines"], ["InvoiceService"])


if __name__ == "__main__":
    unittest.main()
