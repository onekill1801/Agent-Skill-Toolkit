#!/usr/bin/env python3
"""orca_tabs: a tab is reopened only when its supervisor is not running, never twice.
    python -m unittest discover -s tests
"""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, ".."))
sys.path.insert(0, os.path.join(_ROOT, ".claude", "skills", "dev-automation", "tools"))

import orca_tabs as ot  # noqa: E402

SUP = ["python3", "-u", ".claude/skills/dev-automation/tools/supervise.py"]
CFG = {"worktree": "id:r::/toolkit",
       "tabs": [{"name": "Coordinator", "command": "python3 -u coordinator.py work"},
                {"name": "dashboard", "command": "python3 -u dashboard.py serve", "reload": False}]}


class RunningTest(unittest.TestCase):
    def test_supervisor_names_only(self):
        procs = [SUP + ["--name", "Coordinator", "--", "python3", "-u", "coordinator.py", "work"],
                 ["python3", "-u", "coordinator.py", "work"],             # the child: not a supervisor
                 ["vim", "--name", "dashboard"],                          # --name of another program
                 ['"C:\\py\\python.exe"', '".claude\\skills\\dev-automation\\tools\\supervise.py"',
                  "--name", "tg_code", "--", "python"]]
        self.assertEqual(ot.running(procs), {"Coordinator", "tg_code"})


class EnsureTest(unittest.TestCase):
    def setUp(self):
        self.created = []
        patches = [mock.patch.object(ot.orca, "available", return_value=True),
                   mock.patch.object(ot.orca, "terminal_create",
                                     side_effect=lambda wt, title, cmd: self.created.append((wt, title, cmd))
                                     or {"terminal": {"handle": f"term_{title}"}}),
                   mock.patch.object(ot, "_cmdlines",
                                     return_value=[SUP + ["--name", "Coordinator", "--", "x"]])]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_opens_only_missing(self):
        out = ot.ensure(CFG)
        self.assertEqual(out["running"], ["Coordinator"])
        self.assertEqual([t for _, t, _ in self.created], ["dashboard"])
        wt, _, cmd = self.created[0]
        self.assertEqual(wt, "id:r::/toolkit")
        name = "'dashboard'" if os.name == "nt" else "dashboard"
        self.assertIn(f"--name {name} --no-reload -- python3 -u dashboard.py serve", cmd)

    def test_dry_run_opens_nothing(self):
        out = ot.ensure(CFG, dry_run=True)
        self.assertEqual(self.created, [])
        self.assertEqual([o["name"] for o in out["opened"]], ["dashboard"])

    def test_orca_down_does_nothing(self):
        with mock.patch.object(ot.orca, "available", return_value=False):
            out = ot.ensure(CFG)
        self.assertEqual((out["orca"], self.created), ("not running", []))

    def test_create_failure_reported(self):
        with mock.patch.object(ot.orca, "terminal_create", side_effect=ot.orca.OrcaError("boom", "x")):
            out = ot.ensure(CFG)
        self.assertTrue(out["error"])
        self.assertEqual(out["failed"][0]["name"], "dashboard")


class ConfigTest(unittest.TestCase):
    def test_bad_config_rejected(self):
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "daemon_tabs.json"), "w", encoding="utf-8") as f:
            json.dump({"worktree": "w", "tabs": [{"name": "x"}]}, f)
        with mock.patch.dict(os.environ, {"WORK_DIR": d}):
            with self.assertRaises(ValueError):
                ot.load_config()


if __name__ == "__main__":
    unittest.main()
