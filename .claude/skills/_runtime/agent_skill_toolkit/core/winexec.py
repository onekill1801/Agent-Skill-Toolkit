"""Run a CLI found on PATH without letting cmd.exe re-parse our arguments.

On Windows many CLIs are `.cmd`/`.bat` launchers (npm shims, Cursor's agent.cmd). Running them
needs cmd.exe, and cmd.exe re-parses the command line — so untrusted text we pass as an argument
(an MR title, a prompt containing MR content) can run commands (`x" & calc & "`; confirmed on this
machine, design §19 #5). Instead, we resolve what the launcher would run and call that directly:

  npm shim     "%dp0%\\node_modules\\pkg\\bin\\x.js" %*      ->  node.exe x.js ...
  PowerShell   powershell ... -File "%SCRIPT_DIR%\\x.ps1" %*  ->  powershell -NoProfile -File x.ps1 ...
  sibling exe  x.cmd next to x.exe                           ->  x.exe ...

If none applies, `command()` refuses arguments containing cmd.exe metacharacters.
"""

import os
import re
import shutil

_BATCH = (".cmd", ".bat")
_CMD_META = re.compile(r'[&|<>^%!"()\r\n]')
_NPM_JS = re.compile(r'"%dp0%\\([^"]+?\.(?:js|cjs|mjs))"', re.I)
_PS1 = re.compile(r'-File\s+"%(?:~dp0|(?:SCRIPT_DIR|dp0)%)\\?([^"]+?\.ps1)"', re.I)


class UnsafeLauncher(Exception):
    pass


def _powershell() -> str:
    root = os.environ.get("SystemRoot") or r"C:\Windows"
    p = os.path.join(root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    return p if os.path.isfile(p) else (shutil.which("powershell") or "powershell")


def resolve(path: str) -> list:
    """argv prefix that runs `path` without cmd.exe, or [] if it cannot be resolved."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".ps1":
        return [_powershell(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", path]
    if ext not in _BATCH:
        return [path]
    base = os.path.dirname(path)
    stem = os.path.splitext(os.path.basename(path))[0]
    sibling = os.path.join(base, stem + ".exe")
    if os.path.isfile(sibling):
        return [sibling]
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return []
    m = _NPM_JS.search(text)
    if m:
        script = os.path.normpath(os.path.join(base, m.group(1)))
        node = os.path.join(base, "node.exe")
        node = node if os.path.isfile(node) else shutil.which("node")
        if node and os.path.isfile(script):
            return [os.path.normpath(node), script]
    m = _PS1.search(text)
    if m:
        ps1 = os.path.normpath(os.path.join(base, m.group(1)))
        if os.path.isfile(ps1):
            return [_powershell(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ps1]
    return []


def command(argv: list) -> list:
    """Make argv[0] (a name on PATH or a path) directly executable, never via cmd.exe with
    untrusted arguments. Raises UnsafeLauncher when only cmd.exe could run it and an argument
    contains cmd.exe metacharacters. No-op on macOS/Linux."""
    if not argv:
        return argv
    exe = shutil.which(argv[0]) or argv[0]
    if os.name != "nt":
        return [exe, *argv[1:]] if shutil.which(argv[0]) else list(argv)
    prefix = resolve(exe)
    if prefix:
        return [*prefix, *argv[1:]]
    if os.path.splitext(exe)[1].lower() in _BATCH:
        bad = [a for a in argv[1:] if _CMD_META.search(str(a))]
        if bad:
            raise UnsafeLauncher(f"{exe} can only run through cmd.exe, which would re-parse "
                                 f"{len(bad)} argument(s) containing shell metacharacters; "
                                 f"pass the text via stdin or install a non-batch launcher")
        return ["cmd", "/c", exe, *argv[1:]]
    return [exe, *argv[1:]]
