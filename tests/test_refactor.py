#!/usr/bin/env python3
"""Large-refactor lane, part a (checklist F.10a): architecture ratchet + survey.
    python -m unittest discover -s tests
"""

import json
import os
import runpy
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
runpy.run_path(os.path.join(_ROOT, ".claude", "skills", "_runtime", "bootstrap.py"))["activate"]()
from agent_skill_toolkit.workflows.refactor import ratchet as R  # noqa: E402
from agent_skill_toolkit.workflows.refactor import survey  # noqa: E402
from agent_skill_toolkit.workflows.verify import ladder as L  # noqa: E402

sys.path.insert(0, os.path.join(_ROOT, ".claude", "skills", "auto-dev", "tools"))
import refactor as RF  # noqa: E402

RULES = {"rules": [{"id": "web-no-repo", "files": r"(^|/)web/.*\.java$", "deny": r"^\s*import\s+.*\.repository\.",
                    "why": "controllers go through services"}]}
J = "src/main/java/com/acme/"


def git(repo, *a):
    return subprocess.run(["git", "-C", repo, *a], check=True, capture_output=True, text=True).stdout.strip()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="refac-")
        mock.patch.dict(os.environ, {"TOOLKIT_STATE_DIR": self.tmp, "WORK_DIR": self.tmp}).start()
        self.repo = os.path.join(self.tmp, "repo")
        self.files = {
            J + "web/TaskController.java": "package com.acme.web;\nimport com.acme.repository.TaskRepo;\n"
                                           "import com.acme.service.TaskService;\nclass TaskController {}\n",
            J + "web/UserController.java": "package com.acme.web;\nimport com.acme.repository.UserRepo;\nclass U {}\n",
            J + "service/TaskService.java": "package com.acme.service;\nimport com.acme.repository.TaskRepo;\n"
                                            "import com.acme.web.TaskController;\nclass TaskService {}\n",
            J + "repository/TaskRepo.java": "package com.acme.repository;\nclass TaskRepo {}\n",
            J + "repository/UserRepo.java": "package com.acme.repository;\nclass UserRepo {}\n",
            "src/test/java/com/acme/service/TaskServiceTest.java": "class TaskServiceTest {}\n",
            ".agent/architecture.json": json.dumps(RULES),
        }
        for rel, body in self.files.items():
            self.write(rel, body)
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "t@t")
        git(self.repo, "config", "user.name", "t")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "init")

    def tearDown(self):
        mock.patch.stopall()

    def write(self, rel, body):
        p = os.path.join(self.repo, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(body)


class RatchetTest(Base):
    def test_rules_are_validated(self):
        for bad in ("{}", '{"rules": []}', '{"rules": [{"id": "a", "files": "x"}]}',
                    '{"rules": [{"id": "a|b", "files": "x", "deny": "y"}]}', '{"rules": [{"id": "a", "files": "(", "deny": "y"}]}'):
            with self.assertRaises(ValueError, msg=bad):
                R.parse_rules(bad)

    def test_identity_ignores_line_numbers(self):
        rules = R.parse_rules(json.dumps(RULES))
        before = R.violations(self.repo, rules)
        self.assertEqual(sum(before.values()), 2)
        self.write(J + "web/TaskController.java", "package com.acme.web;\n\n\n// moved\nimport com.acme.repository.TaskRepo;\n")
        self.assertEqual(R.compare(before, R.violations(self.repo, rules))["new"], {})

    def test_cli_init_check_tighten(self):
        out = RF.ratchet("init", self.repo)
        self.assertEqual(out["count"], 2)
        with self.assertRaises(ValueError):
            RF.ratchet("init", self.repo)                         # never overwritten
        self.write(J + "web/AdminController.java", "package com.acme.web;\nimport com.acme.repository.X;\n")
        git(self.repo, "add", "-A")
        chk = RF.ratchet("check", self.repo)
        self.assertFalse(chk["passed"])
        with self.assertRaises(ValueError):
            RF.ratchet("tighten", self.repo)                      # a new violation blocks tightening
        os.remove(os.path.join(self.repo, J + "web/AdminController.java"))
        self.write(J + "web/UserController.java", "package com.acme.web;\nclass U {}\n")   # one fixed
        git(self.repo, "add", "-A")
        out = RF.ratchet("tighten", self.repo)
        self.assertEqual((out["was"], out["now"]), (2, 1))
        self.write(J + "web/UserController.java", "package com.acme.web;\nimport com.acme.repository.UserRepo;\n")
        self.assertFalse(RF.ratchet("check", self.repo)["passed"])   # the fixed one cannot come back


class RatchetRungTest(Base):
    def run_rung(self):
        job = {"repo": self.repo, "base": git(self.repo, "rev-parse", "main")}
        return L.rung_ratchet(None, {"name": "arch", "kind": "ratchet"}, job)

    def test_branch_cannot_loosen_its_own_rules(self):
        RF.ratchet("init", self.repo)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "baseline")
        git(self.repo, "checkout", "-qb", "feat/x")
        self.write(J + "web/AdminController.java", "package com.acme.web;\nimport com.acme.repository.X;\n")
        self.write(".agent/architecture.json", json.dumps({"rules": [{"id": "nothing", "files": "nope", "deny": "nope"}]}))
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "violate + loosen")
        res = self.run_rung()
        self.assertFalse(res["passed"])
        self.assertNotIn("fixable", res)                         # the fixer may remove it in production code
        self.assertIn("AdminController", res["summary"])
        self.assertEqual((res["baseline_count"], res["count"]), (2, 3))

    def test_missing_rules_on_base_blocks(self):
        git(self.repo, "rm", "-q", ".agent/architecture.json")
        git(self.repo, "commit", "-qm", "drop rules")
        res = self.run_rung()
        self.assertEqual((res["passed"], res["fixable"]), (False, False))

    def test_rung_is_registered_and_needs_no_protects(self):
        self.assertIn("ratchet", L.RUNGS)
        self.assertEqual(L.spec_launchers({"rungs": [{"name": "arch", "kind": "ratchet"}]}), [])


class SurveyTest(Base):
    def test_modules_edges_cycles_tests(self):
        s = survey.run(self.repo)
        mods = {m["module"]: m for m in s["modules"]}
        self.assertEqual(sorted(mods), ["com.acme.repository", "com.acme.service", "com.acme.web"])
        edges = {(e["from"], e["to"]) for e in s["edges"]}
        self.assertIn(("com.acme.web", "com.acme.repository"), edges)
        self.assertEqual(s["cycles"], [["com.acme.service", "com.acme.web"]])
        self.assertEqual(mods["com.acme.service"]["tests"], 1)
        md = survey.markdown(s)
        self.assertIn("com.acme.service ↔ com.acme.web", md)
        self.assertIn("Module chưa có file test", md)

    def test_cli_writes_the_report(self):
        out = RF.main(["survey", "--repo", self.repo, "--out", os.path.join(self.tmp, "s.md")])
        self.assertEqual(out, 0)
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, "s.md")))


if __name__ == "__main__":
    unittest.main()
