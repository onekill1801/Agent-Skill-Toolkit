#!/usr/bin/env python3
"""FortiGate SSL-VPN helper — status / up / down / ensure / watch (user + password, no OTP).

  macOS / Linux : openfortivpn (brew install openfortivpn | apt install openfortivpn).
                  Needs root (pppd): run `sudo python3 vpn.py up`, or allow it in sudoers:
                    <you> ALL=(root) NOPASSWD: /opt/homebrew/bin/openfortivpn, /usr/bin/pkill -f openfortivpn
  Windows       : FortiSSLVPNclient.exe when the installed FortiClient ships it; otherwise
                  FortiClient is opened and we wait until the internal network answers
                  (FortiClient keeps its own saved password).

Password is NEVER read from .env: macOS Keychain (`security`), Windows DPAPI-encrypted
file (per Windows user), Linux `secret-tool` (libsecret). Store it once, interactively:
    python vpn.py set-password

Config (.env):
    FORTIVPN_HOST=vpn.company.vn   FORTIVPN_PORT=443   FORTIVPN_USER=chungtv8
    FORTIVPN_REALM=                 # optional
    FORTIVPN_TRUSTED_CERT=          # sha256 of the gateway cert (`vpn.py fingerprint`)
    FORTIVPN_CHECK=gitlab.fis.vn:443,10.14.121.30:8443   # "internal network is up" probes

    python vpn.py status            # exit 0 = internal network reachable
    python vpn.py up [--wait 60]    # connect (no-op if already reachable)
    python vpn.py ensure            # status, and up if down
    python vpn.py down
    python vpn.py watch [--interval 60]   # keep-alive: reconnect when the probes fail
    python vpn.py fingerprint       # print the gateway certificate sha256
"""

import argparse
import base64
import getpass
import hashlib
import json
import os
import platform
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time

import config
import probe_common as pc

SERVICE = "agent-skill-toolkit-fortivpn"
SYSTEM = platform.system()


class VpnError(Exception):
    pass


# --------------------------------------------------------------------------- config / probes

def cfg() -> dict:
    return {"host": config.get("FORTIVPN_HOST"), "port": config.get("FORTIVPN_PORT", "443") or "443",
            "user": config.get("FORTIVPN_USER"), "realm": config.get("FORTIVPN_REALM"),
            "trusted_cert": config.get("FORTIVPN_TRUSTED_CERT"),
            "check": [x.strip() for x in (config.get("FORTIVPN_CHECK") or "gitlab.fis.vn:443").split(",") if x.strip()]}


def _reachable(hostport: str, timeout: float = 3.0) -> bool:
    host, _, port = hostport.rpartition(":")
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except (OSError, ValueError):
        return False


def internal_up(c: dict = None) -> dict:
    c = c or cfg()
    res = {hp: _reachable(hp) for hp in c["check"]}
    return {"reachable": all(res.values()) if res else False, "probes": res}


def _state_dir() -> str:
    base = os.path.dirname(os.path.abspath(__file__))
    root = os.path.normpath(os.path.join(base, "..", "..", "..", ".."))
    d = os.path.join(root, "temp", "vpn")
    os.makedirs(d, exist_ok=True)
    return d


# --------------------------------------------------------------------------- password store

def _dpapi(data: bytes, protect: bool) -> bytes:
    import ctypes
    from ctypes import wintypes

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    inp, out = BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), BLOB()
    fn = ctypes.windll.crypt32.CryptProtectData if protect else ctypes.windll.crypt32.CryptUnprotectData
    if not fn(ctypes.byref(inp), None, None, None, None, 0, ctypes.byref(out)):
        raise VpnError("DPAPI call failed")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def _win_secret_path() -> str:
    return os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), SERVICE, "password.dpapi")


def store_password(user: str, password: str) -> str:
    if SYSTEM == "Darwin":
        subprocess.run(["security", "add-generic-password", "-U", "-s", SERVICE, "-a", user, "-w", password],
                       check=True, capture_output=True)
        return "macOS Keychain"
    if SYSTEM == "Windows":
        path = _win_secret_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(base64.b64encode(_dpapi(password.encode("utf-8"), True)))
        return "DPAPI file (this Windows user only)"
    if shutil.which("secret-tool"):
        subprocess.run(["secret-tool", "store", "--label", SERVICE, "service", SERVICE, "user", user],
                       input=password.encode(), check=True, capture_output=True)
        return "libsecret keyring"
    raise VpnError("no OS keyring available (install libsecret-tools)")


def load_password(user: str) -> str:
    if SYSTEM == "Darwin":
        p = subprocess.run(["security", "find-generic-password", "-s", SERVICE, "-a", user, "-w"],
                           capture_output=True, text=True)
        if p.returncode == 0:
            return p.stdout.rstrip("\n")
    elif SYSTEM == "Windows":
        try:
            with open(_win_secret_path(), "rb") as f:
                return _dpapi(base64.b64decode(f.read()), False).decode("utf-8")
        except OSError:
            pass
    elif shutil.which("secret-tool"):
        p = subprocess.run(["secret-tool", "lookup", "service", SERVICE, "user", user],
                           capture_output=True, text=True)
        if p.returncode == 0:
            return p.stdout.rstrip("\n")
    raise VpnError("no stored VPN password — run `python vpn.py set-password` in a terminal")


# --------------------------------------------------------------------------- openfortivpn (macOS/Linux)

def _openfortivpn() -> str:
    for p in (shutil.which("openfortivpn"), "/opt/homebrew/bin/openfortivpn", "/usr/local/bin/openfortivpn",
              "/usr/bin/openfortivpn"):
        if p and os.path.isfile(p):
            return p
    raise VpnError("openfortivpn not installed (macOS: brew install openfortivpn; Ubuntu: apt install openfortivpn)")


def _up_openfortivpn(c: dict, wait: int) -> dict:
    exe = _openfortivpn()
    pw = load_password(c["user"])
    # Password goes through a 0600 temp config (not argv, which `ps` shows); removed once connected.
    fd, conf = tempfile.mkstemp(prefix="fvpn-", suffix=".conf", dir=_state_dir())
    with os.fdopen(fd, "w") as f:
        f.write(f"host = {c['host']}\nport = {c['port']}\nusername = {c['user']}\npassword = {pw}\n")
        if c["realm"]:
            f.write(f"realm = {c['realm']}\n")
        if c["trusted_cert"]:
            f.write(f"trusted-cert = {c['trusted_cert']}\n")
    os.chmod(conf, 0o600)
    log = os.path.join(_state_dir(), "openfortivpn.log")
    argv = [exe, "-c", conf]
    if getattr(os, "geteuid", lambda: 0)() != 0:
        argv = ["sudo", "-n"] + argv            # -n: fail instead of prompting (agents have no TTY)
    try:
        with open(log, "a") as lf:
            proc = subprocess.Popen(argv, stdout=lf, stderr=lf, stdin=subprocess.DEVNULL, start_new_session=True)
        deadline = time.time() + wait
        while time.time() < deadline:
            if proc.poll() is not None:
                tail = open(log, errors="replace").read()[-600:]
                if "a password is required" in tail or "sudo:" in tail:
                    raise VpnError("needs root: run `! sudo python3 vpn.py up` or add the sudoers line (see --help)")
                raise VpnError(f"openfortivpn exited: {tail.strip()[-300:]}")
            if internal_up(c)["reachable"]:
                return {"connected": True, "pid": proc.pid, "log": log}
            time.sleep(1)
        return {"connected": False, "pid": proc.pid, "log": log, "hint": "tunnel started but probes still fail"}
    finally:
        time.sleep(1)
        try:
            os.remove(conf)
        except OSError:
            pass


def _down_openfortivpn() -> dict:
    cmd = ["pkill", "-f", "openfortivpn"]
    if getattr(os, "geteuid", lambda: 0)() != 0:
        cmd = ["sudo", "-n"] + cmd
    p = subprocess.run(cmd, capture_output=True, text=True)
    return {"stopped": p.returncode == 0, "detail": (p.stderr or "").strip()[-200:] or None}


# --------------------------------------------------------------------------- Windows FortiClient

def _forti_dir() -> str:
    for base in (os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("ProgramFiles(x86)", "")):
        d = os.path.join(base, "Fortinet", "FortiClient") if base else ""
        if d and os.path.isdir(d):
            return d
    raise VpnError("FortiClient not found under Program Files")


def _up_windows(c: dict, wait: int) -> dict:
    d = _forti_dir()
    cli = os.path.join(d, "FortiSSLVPNclient.exe")
    if os.path.isfile(cli) and c["user"]:
        pw = load_password(c["user"])
        subprocess.Popen([cli, "connect", "-h", f"{c['host']}:{c['port']}", "-u", f"{c['user']}:{pw}", "-i", "-q"],
                         creationflags=0x08000000)        # CREATE_NO_WINDOW
        mode = "FortiSSLVPNclient.exe"
    else:
        os.startfile(os.path.join(d, "FortiClient.exe"))  # noqa: S606 — user clicks Connect (saved password)
        mode = "FortiClient GUI opened — click Connect"
    deadline = time.time() + wait
    while time.time() < deadline:
        if internal_up(c)["reachable"]:
            return {"connected": True, "mode": mode}
        time.sleep(2)
    return {"connected": False, "mode": mode, "hint": "internal probes still fail"}


def _down_windows() -> dict:
    cli = os.path.join(_forti_dir(), "FortiSSLVPNclient.exe")
    if os.path.isfile(cli):
        p = subprocess.run([cli, "disconnect"], capture_output=True, text=True)
        return {"stopped": p.returncode == 0}
    return {"stopped": False, "hint": "disconnect in FortiClient (no CLI in this FortiClient build)"}


# --------------------------------------------------------------------------- commands

def up(wait: int = 60) -> dict:
    c = cfg()
    st = internal_up(c)
    if st["reachable"]:
        return {"connected": True, "already": True, **st}
    if not c["host"] or not c["user"]:
        raise VpnError("set FORTIVPN_HOST and FORTIVPN_USER in .env")
    out = _up_windows(c, wait) if SYSTEM == "Windows" else _up_openfortivpn(c, wait)
    return {**out, **internal_up(c)}


def down() -> dict:
    return _down_windows() if SYSTEM == "Windows" else _down_openfortivpn()


def fingerprint() -> dict:
    c = cfg()
    if not c["host"]:
        raise VpnError("set FORTIVPN_HOST in .env")
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE                       # we only read the cert to pin it
    with socket.create_connection((c["host"], int(c["port"])), timeout=10) as s:
        with ctx.wrap_socket(s, server_hostname=c["host"]) as t:
            der = t.getpeercert(binary_form=True)
    return {"host": c["host"], "port": c["port"], "sha256": hashlib.sha256(der).hexdigest(),
            "set_in_env": "FORTIVPN_TRUSTED_CERT=<sha256 above> (verify it with your IT team first)"}


def watch(interval: int) -> None:
    fails = 0
    while True:
        if internal_up()["reachable"]:
            fails = 0
        else:
            fails += 1
            if fails >= 2:                                 # two misses in a row → reconnect
                try:
                    print(json.dumps(up(), ensure_ascii=False), flush=True)
                except VpnError as e:
                    print(json.dumps({"error": True, "message": str(e)}, ensure_ascii=False), flush=True)
                fails = 0
        time.sleep(interval)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="vpn.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    p = sub.add_parser("up")
    p.add_argument("--wait", type=int, default=60)
    p = sub.add_parser("ensure")
    p.add_argument("--wait", type=int, default=60)
    sub.add_parser("down")
    sub.add_parser("set-password")
    sub.add_parser("fingerprint")
    p = sub.add_parser("watch")
    p.add_argument("--interval", type=int, default=60)
    a = ap.parse_args(argv)
    try:
        if a.cmd == "status":
            out = {**internal_up(), "os": SYSTEM}
            pc.emit(out)
            return 0 if out["reachable"] else 2
        if a.cmd == "set-password":
            if not sys.stdin.isatty():
                raise VpnError("set-password must be run by a human in a terminal")
            c = cfg()
            user = c["user"] or input("VPN user: ").strip()
            where = store_password(user, getpass.getpass(f"VPN password for {user}: "))
            out = {"stored": True, "user": user, "where": where}
        elif a.cmd in ("up", "ensure"):
            out = up(a.wait)
        elif a.cmd == "down":
            out = down()
        elif a.cmd == "fingerprint":
            out = fingerprint()
        else:
            watch(a.interval)
            return 0
    except (VpnError, subprocess.CalledProcessError, OSError) as e:
        out = {"error": True, "message": str(e)}
    return pc.emit(out)


if __name__ == "__main__":
    sys.exit(main())
