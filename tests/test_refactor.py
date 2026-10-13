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
        before = R.scan(self.repo, rules)["violations"]
        self.assertEqual(sum(before.values()), 2)
        self.write(J + "web/TaskController.java", "package com.acme.web;\n\n\n// moved\nimport com.acme.repository.TaskRepo;\n")
        self.assertEqual(R.compare(before, R.scan(self.repo, rules)["violations"])["new"], {})

    def test_init_never_reinitialises_an_empty_baseline(self):   # Codex F.10a #6
        self.write(R.BASELINE_FILE, json.dumps({"violations": {}}))
        with self.assertRaises(ValueError):
            RF.ratchet("init", self.repo)

    def test_failed_check_exits_nonzero(self):                   # Codex F.10a #7
        RF.ratchet("init", self.repo)
        self.write(J + "web/AdminController.java", "package com.acme.web;\nimport com.acme.repository.X;\n")
        git(self.repo, "add", "-A")
        self.assertEqual(RF.main(["ratchet", "check", "--repo", self.repo]), 1)

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

    def branch(self):
        RF.ratchet("init", self.repo)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "baseline")
        git(self.repo, "checkout", "-qb", "feat/x")

    def commit(self, msg="change"):
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", msg)

    def test_new_violation_is_red_and_fixable(self):
        self.branch()
        self.write(J + "web/AdminController.java", "package com.acme.web;\nimport com.acme.repository.X;\n")
        self.commit()
        res = self.run_rung()
        self.assertFalse(res["passed"])
        self.assertNotIn("fixable", res)                         # the fixer may remove it in production code
        self.assertIn("AdminController", res["summary"])
        self.assertEqual((res["baseline_count"], res["count"]), (2, 3))

    def test_branch_cannot_weaken_the_policy(self):              # Codex F.10a #1
        self.branch()
        self.write(".agent/architecture.json", json.dumps({"rules": [{"id": "nothing", "files": "nope", "deny": "nope"}]}))
        self.commit("loosen")
        res = self.run_rung()
        self.assertEqual((res["passed"], res["fixable"]), (False, False))
        self.assertIn("removed", res["summary"])
        git(self.repo, "checkout", "-q", "main")
        git(self.repo, "checkout", "-qb", "feat/y")
        bl = json.load(open(os.path.join(self.repo, R.BASELINE_FILE)))
        bl["violations"]["web-no-repo|x.java|import a.repository.B"] = 1
        self.write(R.BASELINE_FILE, json.dumps(bl))
        self.commit("grow baseline")
        self.assertIn("baseline grew", self.run_rung()["summary"])

    def test_reformatting_cannot_hide_a_violation(self):         # Codex F.10a #3
        self.branch()
        self.write(J + "web/AdminController.java", "package com.acme.web;\nimport\n  com.acme.repository . X;\n"
                   "/* import com.acme.repository.Y; */\n")
        self.commit()
        res = self.run_rung()
        self.assertFalse(res["passed"])
        self.assertEqual(res["count"], 3)                         # the commented-out one is not counted

    def test_oversized_covered_file_fails_closed(self):          # Codex F.10a #2
        self.branch()
        self.write(J + "web/TaskController.java", "package com.acme.web;\nimport com.acme.repository.TaskRepo;\n"
                   + "// pad\n" * (R.MAX_FILE // 7 + 10))
        self.commit()
        res = self.run_rung()
        self.assertEqual((res["passed"], res["fixable"]), (False, False))
        self.assertIn("could not be scanned", res["summary"])

    def test_moving_a_file_out_of_scope_is_not_a_fix(self):      # Codex F.10a #4
        self.branch()
        os.makedirs(os.path.join(self.repo, J + "other"), exist_ok=True)
        git(self.repo, "mv", J + "web/UserController.java", J + "other/UserController.java")
        self.commit("move")
        res = self.run_rung()
        self.assertEqual((res["passed"], res["fixable"]), (False, False))
        self.assertIn("moved out", res["summary"])

    def test_package_selector_follows_the_code(self):            # Codex F.10a #4
        rules = {"rules": [dict(RULES["rules"][0], package=r"\.web(\.|$)")]}
        self.write(".agent/architecture.json", json.dumps(rules))
        self.commit("package selector")
        self.branch()
        os.makedirs(os.path.join(self.repo, J + "other"), exist_ok=True)
        git(self.repo, "mv", J + "web/UserController.java", J + "other/UserController.java")
        self.commit("move")
        res = self.run_rung()
        self.assertTrue(res["passed"])
        self.assertEqual(res["count"], 2)                         # still counted where it moved

    def test_long_statements_do_not_collide(self):               # Codex F.10a #5
        a, b = "import com.acme.repository." + "A" * 400, "import com.acme.repository." + "A" * 399 + "B"
        self.assertNotEqual(R.identity("r", "f", a), R.identity("r", "f", b))

    def test_expensive_regex_is_cut_off(self):                   # Codex F.10a #8
        self.write(".agent/architecture.json", json.dumps({"rules": [{"id": "dos", "files": r"\.java$",
                                                                       "deny": r"^(a+)+$"}]}))
        self.write(J + "web/Evil.java", "a" * 40 + "b;\n")
        self.commit("evil")
        with self.assertRaises(TimeoutError):
            R.scan_isolated(self.repo, open(os.path.join(self.repo, ".agent/architecture.json")).read(), timeout=2)

    def test_missing_rules_on_base_blocks(self):
        git(self.repo, "rm", "-q", ".agent/architecture.json")
        git(self.repo, "commit", "-qm", "drop rules")
        res = self.run_rung()
        self.assertEqual((res["passed"], res["fixable"]), (False, False))

    def test_rung_is_registered_and_needs_no_protects(self):
        self.assertIn("ratchet", L.RUNGS)
        self.assertEqual(L.spec_launchers({"rungs": [{"name": "arch", "kind": "ratchet"}]}), [])


class Round2Test(RatchetRungTest):
    """Codex F.10a round 2."""

    def pkg_rule(self):
        self.write(".agent/architecture.json", json.dumps({"rules": [dict(RULES["rules"][0], package=r"\.web(\.|$)")]}))
        self.commit("package selector")

    def test_package_covered_but_unreadable_fails_closed(self):
        self.pkg_rule()
        self.branch()
        self.write(J + "other/Big.java", "package com.acme.web;\nimport com.acme.repository.Z;\n" + "// p\n" * (R.MAX_FILE // 5 + 10))
        self.commit()
        res = self.run_rung()
        self.assertEqual((res["passed"], res["fixable"]), (False, False))
        self.assertIn("could not be scanned", res["summary"])

    def test_move_plus_rewrite_is_still_caught(self):
        self.branch()
        os.remove(os.path.join(self.repo, J + "web/UserController.java"))
        self.write(J + "other/Totally.java", "package com.acme.other;\nimport com.acme.repository.UserRepo;\n"
                   + "".join(f"class C{i} {{}}\n" for i in range(40)))
        self.commit("move + rewrite")
        res = self.run_rung()
        self.assertEqual((res["passed"], res["fixable"]), (False, False))
        self.assertIn("moved out", res["summary"])

    def test_package_with_a_comment_is_read(self):
        self.pkg_rule()
        self.branch()
        self.write(J + "other/C.java", "package /* x */ com.acme.web;\nimport com.acme.repository.Q;\n")
        self.commit()
        self.assertFalse(self.run_rung()["passed"])

    def test_kotlin_without_semicolons(self):
        self.branch()
        self.write(J + "web/K.kt", "package com.acme.web\nimport com.acme.repository.X\nclass K {}\n")
        self.write(".agent/architecture.json", json.dumps(RULES))
        self.commit()
        rules = R.parse_rules(json.dumps({"rules": [dict(RULES["rules"][0], files=r"(^|/)web/.*\.(java|kt)$")]}))
        found = [k for k in R.scan(self.repo, rules)["violations"] if "K.kt" in k]
        self.assertEqual(found, ["web-no-repo|" + J + "web/K.kt|import com.acme.repository.X"])

    def test_js_ts_named_and_semicolonless_imports(self):                    # Codex F.10a R3
        rules = R.parse_rules(json.dumps({"rules": [{"id": "ui-no-repo", "files": r"\.tsx?$",
                                                     "deny": r"^import\b.*repository"}]}))
        self.write("web/app.ts", 'import { Repo, Other } from "./repository"\nimport type {X} from \'../repository/x\';\n'
                   "export function f() { return 1 }\n")
        git(self.repo, "add", "-A")
        found = sorted(k.split("|", 2)[2] for k in R.scan(self.repo, rules)["violations"])
        self.assertEqual(found, ['import type {X} from \'../repository/x\'', 'import { Repo, Other } from "./repository"'])

    def test_long_statement_moved_out_is_caught(self):                       # Codex F.10a R3
        long_import = "import com.acme.repository." + ".".join(["sub"] * 120) + ".Thing;"
        self.write(J + "web/LongController.java", "package com.acme.web;\n" + long_import + "\n")
        self.commit("long violation")
        RF.ratchet("init", self.repo)
        self.commit("baseline")
        git(self.repo, "checkout", "-qb", "feat/long")
        os.remove(os.path.join(self.repo, J + "web/LongController.java"))
        self.write(J + "other/Elsewhere.java", "package com.acme.other;\n" + long_import + "\nclass E {}\n" * 30)
        self.commit("move + rewrite")
        res = self.run_rung()
        self.assertEqual((res["passed"], res["fixable"]), (False, False))
        self.assertIn("moved out", res["summary"])

    def test_legacy_baseline_keys_still_match(self):
        RF.ratchet("init", self.repo)
        bl = json.load(open(os.path.join(self.repo, R.BASELINE_FILE)))
        bl["violations"] = {k + ";": n for k, n in bl["violations"].items()}       # older format
        self.write(R.BASELINE_FILE, json.dumps(bl))
        self.commit("old baseline")
        git(self.repo, "checkout", "-qb", "feat/old")
        self.write(J + "service/Other.java", "package com.acme.service;\nclass O {}\n")
        self.commit()
        self.assertTrue(self.run_rung()["passed"])

    def test_tighten_carries_renames(self):
        self.branch()
        git(self.repo, "checkout", "-q", "main")
        git(self.repo, "mv", J + "web/UserController.java", J + "web/AccountController.java")
        self.commit("rename in scope")
        out = RF.ratchet("tighten", self.repo)
        self.assertEqual(out["now"], 2)
        self.assertIn("AccountController", open(os.path.join(self.repo, R.BASELINE_FILE)).read())
        self.commit("tightened")
        git(self.repo, "checkout", "-qb", "feat/next")
        self.write(J + "service/X.java", "package com.acme.service;\nclass X {}\n")
        self.commit()
        self.assertTrue(self.run_rung()["passed"])

    def test_path_rule_regex_is_time_limited_everywhere(self):
        self.write(".agent/architecture.json", json.dumps({"rules": [{"id": "dos", "files": r"^(a+)+$", "deny": "x"}]}))
        self.write("a" * 30, "x\n")
        self.commit("dos rule")
        git(self.repo, "checkout", "-qb", "feat/dos")
        git(self.repo, "mv", "a" * 30, "a" * 29 + "b")
        self.commit("rename")
        job = {"repo": self.repo, "base": git(self.repo, "rev-parse", "main")}
        import time as _t
        t0 = _t.monotonic()
        res = L.rung_ratchet(None, {"name": "arch", "kind": "ratchet", "timeout": 2}, job)
        self.assertLess(_t.monotonic() - t0, 20)
        self.assertEqual(res["passed"], False)


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

    def test_python_src_layout_relative_imports_and_java_wildcards(self):   # Codex F.10a #9 / #10
        for rel, body in {"src/a/__init__.py": "", "src/a/x.py": "from ..b import y\nimport os, b.z\n",
                          "src/b/__init__.py": "", "src/b/y.py": "from a import x\n", "src/b/z.py": "",
                          J + "util/Helpers.java": "package com.acme.util;\nimport com.acme.service.*;\n"}.items():
            self.write(rel, body)
        git(self.repo, "add", "-A")
        s = survey.run(self.repo)
        edges = {(e["from"], e["to"]) for e in s["edges"]}
        self.assertIn(("a", "b"), edges)
        self.assertIn(("b", "a"), edges)
        self.assertIn(["a", "b"], s["cycles"])
        self.assertIn(("com.acme.util", "com.acme.service"), edges)

    def test_namespace_packages_in_src(self):                                # Codex F.10a R2 #9
        for rel, body in {"src/a/x.py": "import b.y\n", "src/b/y.py": "from a import x\n"}.items():
            self.write(rel, body)
        git(self.repo, "add", "-A")
        s = survey.run(self.repo)
        self.assertIn(["a", "b"], s["cycles"])

    def test_cli_writes_the_report(self):
        out = RF.main(["survey", "--repo", self.repo, "--out", os.path.join(self.tmp, "s.md")])
        self.assertEqual(out, 0)
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, "s.md")))


if __name__ == "__main__":
    unittest.main()
