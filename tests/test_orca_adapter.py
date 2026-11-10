#!/usr/bin/env python3
"""orca_adapter binary resolution: never run a .cmd/.bat shim (cmd.exe re-parses arguments).
    python -m unittest discover -s tests
"""

import os
import sys
import tempfile
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, ".."))
sys.path.insert(0, os.path.join(_ROOT, ".claude", "skills", "dev-automation", "tools"))

import orca_adapter as oa  # noqa: E402


def _touch(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "w").close()
    return path


class FindBinTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        env = {k: v for k, v in os.environ.items() if k != "ORCA_BIN"}
        self.env = mock.patch.dict(os.environ, env, clear=True)
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def test_path_cmd_is_swapped_for_sibling_exe(self):
        cmd = _touch(os.path.join(self.tmp, "bin", "orca.cmd"))
        exe = _touch(os.path.join(self.tmp, "bin", "orca.exe"))
        with mock.patch("shutil.which", return_value=cmd):
            self.assertEqual(oa.find_bin(), exe)

    def test_path_cmd_without_exe_falls_back_to_shim_candidates(self):
        cmd = _touch(os.path.join(self.tmp, "bin", "orca.cmd"))
        shim = _touch(os.path.join(self.tmp, "shim", "orca.exe"))
        with mock.patch("shutil.which", return_value=cmd), \
                mock.patch.object(oa, "_shim_candidates", return_value=[shim]):
            self.assertEqual(oa.find_bin(), shim)

    def test_orca_bin_batch_without_exe_is_refused(self):
        os.environ["ORCA_BIN"] = _touch(os.path.join(self.tmp, "x", "orca.bat"))
        with self.assertRaises(oa.OrcaError) as ctx:
            oa.find_bin()
        self.assertEqual(ctx.exception.code, "unsafe")

    def test_orca_bin_exe_is_used_as_is(self):
        os.environ["ORCA_BIN"] = exe = _touch(os.path.join(self.tmp, "y", "orca.exe"))
        self.assertEqual(oa.find_bin(), exe)

    def test_windows_candidates_contain_no_batch_files(self):
        with mock.patch("platform.system", return_value="Windows"):
            cands = oa._shim_candidates()
        self.assertTrue(cands)
        self.assertFalse([c for c in cands if oa._is_batch(c)])
        self.assertTrue(any(os.path.join("Programs", "orca", "resources", "bin") in c for c in cands))

    def test_available_is_false_when_only_unsafe_shim(self):
        os.environ["ORCA_BIN"] = _touch(os.path.join(self.tmp, "z", "orca.cmd"))
        self.assertFalse(oa.available())


if __name__ == "__main__":
    unittest.main()
