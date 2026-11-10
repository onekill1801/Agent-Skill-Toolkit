"""Google service-account auth (JWT bearer) — stdlib + openssl, no pip.

A service account authenticates server-to-server: sign a short JWT with the
account's private key, trade it for an access token. RS256 signing is done by
shelling out to `openssl` (stdlib has no RSA), so no google-auth / cryptography.

Env (.env):
  GOOGLE_SA_KEY_FILE   path to the downloaded service-account JSON key
  GOOGLE_OPENSSL_BIN   openssl binary (default: "openssl")

    from agent_skill_toolkit.integrations import google_sa
    tok = google_sa.access_token()                 # default: spreadsheets scope
    google_sa.client_email()                        # the robot address to share with
"""

import base64
import json
import os
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

from ..core import env, tls

DEFAULT_SCOPES = ("https://www.googleapis.com/auth/spreadsheets",)
_CACHE = {}   # scope-key -> (token, exp)


def key_file() -> str:
    return env.get("GOOGLE_SA_KEY_FILE")


def _load_key() -> dict:
    path = key_file()
    if not path:
        raise RuntimeError("GOOGLE_SA_KEY_FILE not set in .env")
    if not os.path.isfile(path):
        # allow a path relative to the toolkit root
        from ..core import paths
        alt = os.path.join(paths.toolkit_root(), path)
        path = alt if os.path.isfile(alt) else path
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def client_email() -> str:
    return _load_key().get("client_email", "")


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _sign_rs256(signing_input: bytes, private_key_pem: str) -> bytes:
    openssl = env.get("GOOGLE_OPENSSL_BIN") or "openssl"
    fd, pem_path = tempfile.mkstemp(suffix=".pem")
    try:
        os.write(fd, private_key_pem.encode())
        os.close(fd)
        proc = subprocess.run([openssl, "dgst", "-sha256", "-sign", pem_path],
                              input=signing_input, capture_output=True)
        if proc.returncode != 0:
            raise RuntimeError("openssl sign failed: " + proc.stderr.decode(errors="replace"))
        return proc.stdout
    finally:
        try:
            os.remove(pem_path)
        except OSError:
            pass


def _build_assertion(key: dict, scopes) -> str:
    now = int(time.time())
    token_uri = key.get("token_uri", "https://oauth2.googleapis.com/token")
    header = {"alg": "RS256", "typ": "JWT"}
    claims = {
        "iss": key["client_email"],
        "scope": " ".join(scopes),
        "aud": token_uri,
        "iat": now,
        "exp": now + 3600,
    }
    signing_input = (_b64url(json.dumps(header, separators=(",", ":")).encode())
                     + "." + _b64url(json.dumps(claims, separators=(",", ":")).encode())).encode()
    sig = _sign_rs256(signing_input, key["private_key"])
    return signing_input.decode() + "." + _b64url(sig)


def access_token(scopes=DEFAULT_SCOPES, force: bool = False) -> str:
    scopes = tuple(scopes)
    now = time.time()
    cached = _CACHE.get(scopes)
    if cached and not force and now < cached[1] - 60:
        return cached[0]
    key = _load_key()
    assertion = _build_assertion(key, scopes)
    data = urllib.parse.urlencode({
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion": assertion,
    }).encode()
    req = urllib.request.Request(key.get("token_uri", "https://oauth2.googleapis.com/token"),
                                 data=data, headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=30, context=tls.context()) as r:
            tok = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"token endpoint {e.code}: {e.read().decode(errors='replace')}") from None
    if "access_token" not in tok:
        raise RuntimeError(f"no access_token: {tok}")
    _CACHE[scopes] = (tok["access_token"], now + int(tok.get("expires_in", 3600)))
    return tok["access_token"]
