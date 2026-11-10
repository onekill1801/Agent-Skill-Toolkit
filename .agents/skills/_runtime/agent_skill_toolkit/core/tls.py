"""VERIFIED TLS for calls that carry a credential (never CERT_NONE, whatever SSL_VERIFY says).

Some Pythons on Windows have an empty trust store: msys2/mingw builds ship no `cert.pem` and have no
`ssl.enum_certificates`, so every verified HTTPS call fails. context() then loads the Windows root +
intermediate certificate stores IN MEMORY (exported by PowerShell — no file is written). If that is not
possible either, verification simply fails: a token is never sent over an unverified connection.

    from agent_skill_toolkit.core import tls
    urllib.request.urlopen(req, context=tls.context())
"""

import ssl
import subprocess
import sys
import threading

_lock = threading.Lock()
_ctx = None

_PS_EXPORT = (
    "$c=@(Get-ChildItem Cert:\\LocalMachine\\Root)+@(Get-ChildItem Cert:\\CurrentUser\\Root)"
    "+@(Get-ChildItem Cert:\\LocalMachine\\CA)+@(Get-ChildItem Cert:\\CurrentUser\\CA);"
    "foreach($x in $c){'-----BEGIN CERTIFICATE-----';"
    "[Convert]::ToBase64String($x.RawData,'InsertLineBreaks');'-----END CERTIFICATE-----'}")


def windows_roots_pem(timeout: float = 30) -> str:
    """PEM text of the Windows certificate stores ('' when unavailable)."""
    from agent_skill_toolkit.core import proc
    try:
        p = proc.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", _PS_EXPORT], timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return p.stdout if p.returncode == 0 and "BEGIN CERTIFICATE" in (p.stdout or "") else ""


def _load_pem(ctx: ssl.SSLContext, pem: str) -> None:
    """Load every certificate of `pem`, skipping the ones this OpenSSL rejects (one bad entry in a
    system store must not drop all the others)."""
    try:
        ctx.load_verify_locations(cadata=pem)
        return
    except ssl.SSLError:
        pass
    end = "-----END CERTIFICATE-----"
    for block in pem.split(end):
        if "BEGIN CERTIFICATE" in block:
            try:
                ctx.load_verify_locations(cadata=block.strip() + "\n" + end + "\n")
            except ssl.SSLError:
                continue


def context() -> ssl.SSLContext:
    """A verifying context with a non-empty trust store when the OS has one. Built once per process."""
    global _ctx
    with _lock:
        if _ctx is not None:
            return _ctx
        ctx = ssl.create_default_context()
        if sys.platform == "win32" and not ctx.cert_store_stats().get("x509_ca"):
            pem = windows_roots_pem()
            if pem:
                _load_pem(ctx, pem)
        _ctx = ctx
        return ctx
