#!/usr/bin/env python3
"""Stdlib unittest for the agentkit entry point.

agentkit is the one place that knows where tools live. The contracts worth
pinning: colliding tool names (config.py exists in three skills) must be
refused rather than silently resolved, and the owning skill's directory must
come first on sys.path so a tool imports its own `config`, not a sibling's.
"""

import os
import subprocess
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, ".."))
sys.path.insert(0, _ROOT)

import agentkit  # noqa: E402


class IndexTest(unittest.TestCase):
    def setUp(self):
        self.index = agentkit.tool_index()

    def test_finds_tools_across_skills(self):
        for tool in ("run_log", "verify_gen", "tasks", "tg_gate"):
            self.assertIn(tool, self.index, f"{tool} missing from index")

    def test_skips_private_and_non_python(self):
        self.assertFalse([t for t in self.index if t.startswith("_")])

    def test_colliding_name_is_refused_not_guessed(self):
        self.assertGreater(len(self.index["config"]), 1)
        with self.assertRaises(ValueError) as cm:
            agentkit.resolve("config", self.index)
        self.assertIn("qualify it", str(cm.exception))

    def test_collision_resolved_by_skill_prefix(self):
        skill, path = agentkit.resolve("dev-automation:config", self.index)
        self.assertEqual(skill, "dev-automation")
        self.assertTrue(path.endswith(os.path.join("dev-automation", "tools", "config.py")))

    def test_unknown_tool_and_unknown_skill(self):
        with self.assertRaises(ValueError):
            agentkit.resolve("no_such_tool", self.index)
        with self.assertRaises(ValueError):
            agentkit.resolve("auto-dev:config", self.index)   # config is not in auto-dev


class PathOrderTest(unittest.TestCase):
    def test_owner_dir_is_first(self):
        _, path = agentkit.resolve("run_log")
        owner = os.path.dirname(path)
        built = agentkit.build_path(owner)
        self.assertEqual(built[0], owner)
        self.assertEqual(len(built), len(set(built)))          # no duplicates
        self.assertGreater(len(built), 1)                      # siblings reachable


class InvocationTest(unittest.TestCase):
    """End-to-end: the entry point must actually run a tool."""

    def _run(self, *args):
        return subprocess.run([sys.executable, os.path.join(_ROOT, "agentkit.py"), *args],
                              capture_output=True, text=True, cwd=_ROOT)

    def test_where_prints_the_file_that_would_run(self):
        r = self._run("--where", "run_log")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(r.stdout.strip().endswith("run_log.py"))

    def test_list_filters(self):
        r = self._run("--list", "run_log")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("run_log", r.stdout)

    def test_runs_a_real_tool(self):
        r = self._run("run_log", "list")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(r.stdout.strip().startswith("["))

    def test_ambiguous_name_exits_nonzero(self):
        r = self._run("config")
        self.assertEqual(r.returncode, 2)
        self.assertIn("qualify it", r.stderr)


if __name__ == "__main__":
    unittest.main()
