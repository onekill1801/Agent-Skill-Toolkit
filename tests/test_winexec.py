#!/usr/bin/env python3
"""core.winexec — never let cmd.exe re-parse untrusted arguments (design §19 #5, checklist 0.7)."""

import os
import runpy
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
runpy.run_path(os.path.join(_ROOT, ".claude", "skills", "_runtime", "bootstrap.py"))["activate"]()
from agent_skill_toolkit.core import winexec as w  # noqa: E402

PAYLOADS = ['x" & echo PWNED_CMD & "', "$(Write-Output PWNED_PS)", "a'b;Write-Output PWNED_SEMI", "%PATH% ^& !x!"]


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="\r\n") as f:
        f.write(text)
    return path


class ResolveTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="winexec-")

    def test_npm_shim_to_node_script(self):
        shim = _write(os.path.join(self.d, "tool.cmd"),
                      '@ECHO off\n"%_prog%"  "%dp0%\\node_modules\\pkg\\bin\\tool.js" %*\n')
        script = _write(os.path.join(self.d, "node_modules", "pkg", "bin", "tool.js"), "")
        node = _write(os.path.join(self.d, "node.exe"), "")
        self.assertEqual(w.resolve(shim), [os.path.normpath(node), os.path.normpath(script)])

    def test_powershell_wrapper_to_ps1(self):
        for ref in ('"%~dp0\\x.ps1"', '"%SCRIPT_DIR%\\x.ps1"'):
            shim = _write(os.path.join(self.d, "x.cmd"),
                          f"@echo off\npowershell.exe -NoProfile -ExecutionPolicy Bypass -File {ref} %*\n")
            ps1 = _write(os.path.join(self.d, "x.ps1"), "")
            out = w.resolve(shim)
            self.assertEqual(out[-2:], ["-File", os.path.normpath(ps1)], ref)

    def test_sibling_exe_wins(self):
        shim = _write(os.path.join(self.d, "orca.cmd"), "@echo off\nsomething %*\n")
        exe = _write(os.path.join(self.d, "orca.exe"), "")
        self.assertEqual(w.resolve(shim), [exe])

    def test_unresolvable_batch_refuses_metacharacters(self):
        shim = _write(os.path.join(self.d, "plain.cmd"), "@echo off\necho %*\n")
        with mock.patch("os.name", "nt"), mock.patch("shutil.which", return_value=shim):
            for p in ('x" & echo PWNED_CMD & "', "%PATH% ^& !x!", "a | b", "line1\nline2"):   # cmd.exe metachars
                with self.assertRaises(w.UnsafeLauncher, msg=p):
                    w.command(["plain", p])
            self.assertEqual(w.command(["plain", "--version"]), ["cmd", "/d", "/c", shim, "--version"])
            for arg in ("two words", ""):                       # Codex R2/R3: spaces, empty "" args
                with self.assertRaises(w.UnsafeLauncher, msg=repr(arg)):
                    w.command(["plain", arg])


@unittest.skipUnless(os.name == "nt", "real cmd.exe / PowerShell behaviour")
class WindowsLiveTest(unittest.TestCase):
    def test_unresolved_batch_under_a_path_with_spaces_still_runs(self):
        d = os.path.join(tempfile.mkdtemp(prefix="winexec-"), "Program Files X")
        shim = _write(os.path.join(d, "plain.cmd"), "@echo off\necho GOT=%1\n")
        with mock.patch("shutil.which", return_value=shim):
            out = subprocess.run(w.command(["plain", "--version"]), capture_output=True, text=True, timeout=30)
        self.assertEqual(out.stdout.strip(), "GOT=--version", out.stderr)

    def test_powershell_file_gets_payloads_verbatim(self):
        d = tempfile.mkdtemp(prefix="winexec-live-")
        with open(os.path.join(d, "echo.ps1"), "w") as f:
            f.write('foreach ($a in $args) { Write-Output ("ARG=[" + $a + "]") }\n')
        cmd = _write(os.path.join(d, "echoargs.cmd"),
                     "@echo off\n%SystemRoot%\\System32\\WindowsPowerShell\\v1.0\\powershell.exe "
                     '-NoProfile -ExecutionPolicy Bypass -File "%~dp0\\echo.ps1" %*\n')
        for p in PAYLOADS:
            out = subprocess.run(w.command([cmd, p]), capture_output=True, text=True, timeout=60).stdout
            self.assertEqual(out.strip(), f"ARG=[{p}]", p)


if __name__ == "__main__":
    unittest.main()
