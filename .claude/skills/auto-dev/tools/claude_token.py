#!/usr/bin/env python3
"""Long-lived Claude token for an agent account — ONE command does every step.

    python agentkit.py claude_token setup <account> [<account> ...] [--no-test]
    python agentkit.py claude_token check [<account> ...]          # test the saved tokens (1 tiny turn each)
    python agentkit.py claude_token list                           # which accounts use a token file

`setup`, per account:
  1. runs `claude setup-token` in an EMPTY temporary CLAUDE_CONFIG_DIR (the Orca login of that account
     and the profile of the running session are never touched); you log in in the browser and paste the
     code back, as usual
  2. captures the printed token from the screen (macOS/Linux: pseudo-terminal, wide so it never wraps;
     Windows / not found: asks you to paste it — input hidden)
  3. saves it to work/secrets/claude-<account>.token (mode 600) — never printed, never in agents.json
  4. work/agents.json: accounts.claude.<account>.env_files.CLAUDE_CODE_OAUTH_TOKEN -> that file
     (backup in temp/), clears the account's "login failed" mark
  5. runs one tiny turn with the token alone to prove it works (skip: --no-test)
  6. removes the temporary config dir

The token is the agent's own auth: agent_runner passes it as CLAUDE_CODE_OAUTH_TOKEN, which wins over a
dead CLAUDE_CONFIG_DIR login (verified 04/11). Log in with the RIGHT e-mail in the browser — the token
does not say which account it belongs to.
"""

import argparse
import getpass
import json
import os
import re
import runpy
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

runpy.run_path(str(Path(__file__).resolve().parents[2] / "_runtime" / "bootstrap.py"))["activate"]()
from agent_skill_toolkit.core import paths  # noqa: E402

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(1, os.path.join(_HERE, "..", "..", "dev-automation", "tools"))
import agent_runner  # noqa: E402
import probe_common as pc  # noqa: E402,F401  (UTF-8 stdout on Windows)

ENV_VAR = "CLAUDE_CODE_OAUTH_TOKEN"
TOKEN_RX = re.compile(r"sk-ant-oat\d{2}-[A-Za-z0-9_\-]{40,}")
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
PTY_COLUMNS = 400          # wide pseudo-terminal: the CLI never wraps the token across lines
TEST_MODEL = "claude-haiku-5-5"


# ----------------------------------------------------------------------------- pure helpers
def extract_token(screen: str) -> str:
    """Last token printed on a captured terminal screen (ANSI codes and line wraps removed)."""
    text = _ANSI.sub("", screen or "").replace("\r", "")
    found = TOKEN_RX.findall(text)
    if not found:                                   # wrapped anyway: try with line breaks removed
        found = TOKEN_RX.findall(re.sub(r"\n", "", text))
    return found[-1] if found else ""


def token_path(account: str) -> str:
    return os.path.join(paths.work_dir(), "secrets", f"claude-{account}.token")


def agents_path() -> str:
    return os.path.join(paths.work_dir(), "agents.json")


def load_agents() -> tuple:
    with open(agents_path(), encoding="utf-8") as f:
        raw = f.read()
    return json.loads(raw), (2 if '\n  "' in raw else 4)


def wire_account(account: str) -> dict:
    """agents.json: env_files.CLAUDE_CODE_OAUTH_TOKEN -> work:secrets/claude-<account>.token (idempotent)."""
    cfg, indent = load_agents()
    accts = ((cfg.get("accounts") or {}).get("claude") or {})
    if account not in accts:
        raise SystemExit(f"'{account}' is not in accounts.claude of {agents_path()} — known: {', '.join(accts)}")
    ref = f"work:secrets/claude-{account}.token"
    entry = accts[account]
    if (entry.get("env_files") or {}).get(ENV_VAR) == ref:
        return {"changed": False}
    backup = os.path.join(paths.toolkit_root(), "temp", f"agents.json.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    os.makedirs(os.path.dirname(backup), exist_ok=True)
    shutil.copy2(agents_path(), backup)
    entry.setdefault("env_files", {})[ENV_VAR] = ref
    tmp = agents_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(json.dumps(cfg, ensure_ascii=False, indent=indent) + "\n")
    os.replace(tmp, agents_path())
    return {"changed": True, "backup": backup}


def save_token(account: str, token: str) -> str:
    p = token_path(account)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    fd = os.open(p + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(token + "\n")
    os.replace(p + ".tmp", p)
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    return p


def mask(token: str) -> str:
    return f"{token[:13]}…{token[-4:]} ({len(token)} ký tự)" if token else "(trống)"


# ----------------------------------------------------------------------------- CLI plumbing
def claude_bin() -> str:
    for c in (shutil.which("claude"), os.path.expanduser("~/.local/bin/claude")):
        if c and os.path.exists(c):
            return c
    raise SystemExit("không tìm thấy lệnh `claude` (PATH hoặc ~/.local/bin/claude)")


def _child_env(config_dir: str, token: str = "") -> dict:
    env = {k: v for k, v in os.environ.items()
           if k not in ("ANTHROPIC_API_KEY", ENV_VAR) and not k.startswith("CLAUDE_CODE_")
           and k not in ("CLAUDECODE", "CLAUDE_PID")}
    env["CLAUDE_CONFIG_DIR"] = config_dir
    if token:
        env[ENV_VAR] = token
    return env


def _run_pty_capture(argv: list, env: dict) -> str:
    """Run interactively through a pseudo-terminal (you see and type as usual); return what was printed."""
    import fcntl
    import pty
    import select
    import struct
    import termios
    import tty
    try:
        rows = os.get_terminal_size(sys.stdout.fileno()).lines
    except OSError:
        rows = 40
    pid, fd = pty.fork()
    if pid == 0:                                    # child: wide window BEFORE the CLI reads its size
        try:
            fcntl.ioctl(sys.stdin.fileno(), termios.TIOCSWINSZ, struct.pack("HHHH", rows, PTY_COLUMNS, 0, 0))
        except OSError:
            pass
        try:
            os.execve(argv[0], argv, env)
        finally:
            os._exit(127)
    captured = bytearray()
    stdin_fd = sys.stdin.fileno()
    old = None
    if os.isatty(stdin_fd):
        old = termios.tcgetattr(stdin_fd)
        tty.setraw(stdin_fd)
    watch = [fd, stdin_fd]
    try:
        while True:
            try:
                r, _, _ = select.select(watch, [], [])
            except InterruptedError:
                continue
            if fd in r:
                try:
                    data = os.read(fd, 4096)
                except OSError:                     # EIO: child closed the terminal
                    break
                if not data:
                    break
                captured += data
                os.write(sys.stdout.fileno(), data)
            if stdin_fd in r:
                data = os.read(stdin_fd, 4096)
                if not data:                        # our input ended: keep reading the CLI until it exits
                    watch = [fd]
                    continue
                os.write(fd, data)
    finally:
        if old is not None:
            termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old)
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass
        os.close(fd)
    return captured.decode("utf-8", "replace")


def run_setup_token(config_dir: str) -> str:
    argv = [claude_bin(), "setup-token"]
    env = _child_env(config_dir)
    if os.name != "nt" and sys.stdin.isatty():
        try:
            return extract_token(_run_pty_capture(argv, env))
        except Exception as e:  # noqa: BLE001 — capture failed: fall back to pasting
            print(f"\n(không bắt được output tự động: {e})")
    subprocess.call(argv, env=env)
    return ""


def test_token(token: str) -> tuple:
    """One tiny turn with the token ALONE (empty config dir). -> (ok, message)."""
    d = tempfile.mkdtemp(prefix="claude-token-test-")
    try:
        p = subprocess.run([claude_bin(), "-p", "--model", TEST_MODEL], input="trả lời đúng một chữ: ok",
                           capture_output=True, text=True, timeout=180, env=_child_env(d, token))
        out = ((p.stdout or "") + (p.stderr or "")).strip()
        return p.returncode == 0, out.splitlines()[-1][:200] if out else f"exit {p.returncode}"
    except subprocess.TimeoutExpired:
        return False, "không phản hồi trong 180 s"
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ----------------------------------------------------------------------------- commands
def setup_one(account: str, test: bool) -> dict:
    known = ((load_agents()[0].get("accounts") or {}).get("claude") or {})   # fail early, before the browser
    if account not in known:
        raise SystemExit(f"'{account}' không có trong accounts.claude của {agents_path()}")
    print(f"\n=== {account}: đăng nhập trình duyệt bằng ĐÚNG tài khoản {account} "
          f"(nên dùng cửa sổ ẩn danh), dán code trình duyệt đưa vào đây ===\n")
    cfg_dir = tempfile.mkdtemp(prefix="claude-setup-token-")
    try:
        token = run_setup_token(cfg_dir)
    finally:
        shutil.rmtree(cfg_dir, ignore_errors=True)
    if token:
        print(f"\n→ đã bắt được token {mask(token)}")
    else:
        print("\n→ không tự bắt được token. Dán token sk-ant-oat01-… (không hiện chữ), Enter để bỏ qua:")
        token = getpass.getpass("token: ").strip()
    if not TOKEN_RX.fullmatch(token or ""):
        return {"account": account, "ok": False,
                "error": "không có token hợp lệ (phải là chuỗi sk-ant-oat01-… CLI in ra, KHÔNG phải code trình duyệt)"}
    path = save_token(account, token)
    wired = wire_account(account)
    agent_runner.clear_auth_bad(f"claude@{account}")
    res = {"account": account, "token": mask(token), "file": path, "agents_json": wired}
    if test:
        ok, msg = test_token(token)
        res.update(ok=ok, test=msg)
    else:
        res["ok"] = True
    return res


def cmd_setup(a) -> int:
    results = []
    for acct in a.accounts:
        try:
            results.append(setup_one(acct, not a.no_test))
        except KeyboardInterrupt:
            results.append({"account": acct, "ok": False, "error": "đã huỷ"})
            break
    print(json.dumps({"results": results}, ensure_ascii=False, indent=2))
    return 0 if results and all(r.get("ok") for r in results) else 1


def _token_accounts() -> list:
    cfg, _ = load_agents()
    return [n for n, v in ((cfg.get("accounts") or {}).get("claude") or {}).items()
            if (v.get("env_files") or {}).get(ENV_VAR)]


def cmd_check(a) -> int:
    out = []
    for acct in a.accounts or _token_accounts():
        try:
            token = open(token_path(acct), encoding="utf-8").read().strip()
        except OSError:
            out.append({"account": acct, "ok": False, "error": f"không có {token_path(acct)}"})
            continue
        ok, msg = test_token(token)
        out.append({"account": acct, "ok": ok, "token": mask(token), "test": msg,
                    "saved": time.strftime("%d/%m %H:%M", time.localtime(os.path.getmtime(token_path(acct))))})
    print(json.dumps({"results": out}, ensure_ascii=False, indent=2))
    return 0 if out and all(r["ok"] for r in out) else 1


def cmd_list(_a) -> int:
    cfg, _ = load_agents()
    rows = []
    for n, v in ((cfg.get("accounts") or {}).get("claude") or {}).items():
        has_file = os.path.exists(token_path(n))
        rows.append({"account": n, "env_files": bool((v.get("env_files") or {}).get(ENV_VAR)),
                     "token_file": has_file,
                     "saved": time.strftime("%d/%m %H:%M", time.localtime(os.path.getmtime(token_path(n))))
                     if has_file else None})
    print(json.dumps({"accounts": rows}, ensure_ascii=False, indent=2))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("setup", help="[WRITE] setup-token -> work/secrets + agents.json + test")
    s.add_argument("accounts", nargs="+")
    s.add_argument("--no-test", action="store_true")
    s.set_defaults(fn=cmd_setup)
    c = sub.add_parser("check", help="test saved tokens (1 tiny turn each)")
    c.add_argument("accounts", nargs="*")
    c.set_defaults(fn=cmd_check)
    sub.add_parser("list", help="accounts and their token files").set_defaults(fn=cmd_list)
    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
