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
