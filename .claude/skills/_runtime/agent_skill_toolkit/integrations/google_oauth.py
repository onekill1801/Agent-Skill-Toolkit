"""Google OAuth (installed/Desktop app) — stdlib only.

The toolkit acts AS the user's own Google account: a one-time loopback consent
yields a durable refresh token; every run after that trades it for a short-lived
access token over plain HTTPS. No gcloud, no google-auth, no pip.

Env (.env):
  GOOGLE_OAUTH_CLIENT_ID       OAuth client id (Desktop app)
  GOOGLE_OAUTH_CLIENT_SECRET   OAuth client secret
  GOOGLE_OAUTH_REFRESH_TOKEN   obtained once via run_consent()

Scopes default to spreadsheets read/write.

    from agent_skill_toolkit.integrations import google_oauth
    tok = google_oauth.access_token()                      # runtime
    google_oauth.run_consent()                             # one-time setup
"""

import http.server
import json
import secrets
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

from ..core import env, tls

AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"
DEFAULT_SCOPES = ("https://www.googleapis.com/auth/spreadsheets",)

_CACHE = {"token": None, "exp": 0.0}


# ── config ──────────────────────────────────────────────────────────────────

def client_id() -> str:
    return env.get("GOOGLE_OAUTH_CLIENT_ID")


def client_secret() -> str:
    return env.get("GOOGLE_OAUTH_CLIENT_SECRET")


def refresh_token() -> str:
    return env.get("GOOGLE_OAUTH_REFRESH_TOKEN")


# ── token exchange ──────────────────────────────────────────────────────────

def _post_token(fields: dict) -> dict:
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(TOKEN_URI, data=data,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=30, context=tls.context()) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise RuntimeError(f"token endpoint {e.code}: {body}") from None


def access_token(force: bool = False) -> str:
    """Trade the stored refresh token for an access token (cached in-process)."""
    now = time.time()
    if not force and _CACHE["token"] and now < _CACHE["exp"] - 60:
        return _CACHE["token"]
    cid, sec, rt = client_id(), client_secret(), refresh_token()
    missing = [k for k, v in (("GOOGLE_OAUTH_CLIENT_ID", cid),
                              ("GOOGLE_OAUTH_CLIENT_SECRET", sec),
                              ("GOOGLE_OAUTH_REFRESH_TOKEN", rt)) if not v]
    if missing:
        raise RuntimeError("missing in .env: " + ", ".join(missing)
                           + " — run the one-time consent (run_consent) first")
    tok = _post_token({"client_id": cid, "client_secret": sec,
                       "refresh_token": rt, "grant_type": "refresh_token"})
    if "access_token" not in tok:
        raise RuntimeError(f"no access_token in response: {tok}")
    _CACHE["token"] = tok["access_token"]
    _CACHE["exp"] = now + int(tok.get("expires_in", 3600))
    return _CACHE["token"]


# ── one-time consent (loopback flow) ────────────────────────────────────────

def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def run_consent(scopes=DEFAULT_SCOPES, open_browser: bool = True, timeout: int = 300) -> dict:
    """Run the installed-app loopback flow and return the token response
    (includes refresh_token). Requires client id/secret in .env."""
    cid, sec = client_id(), client_secret()
    if not cid or not sec:
        raise RuntimeError("set GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET in .env first")

    port = _free_port()
    redirect_uri = f"http://127.0.0.1:{port}/"
    state = secrets.token_urlsafe(16)
    result = {}
    done = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            result.update({k: v[0] for k, v in q.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            ok = "code" in result and result.get("state") == state
            msg = "Xong. Quay lai terminal." if ok else "That bai: " + result.get("error", "state mismatch")
            self.wfile.write(f"<html><body style='font:16px system-ui;padding:40px'>{msg}</body></html>".encode())
            done.set()

        def log_message(self, *a):
            pass

    httpd = http.server.HTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    params = {
        "client_id": cid,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
        "access_type": "offline",
        "prompt": "consent",          # force a refresh_token every time
        "state": state,
    }
    url = AUTH_URI + "?" + urllib.parse.urlencode(params)
    print("Mo URL nay de dong y (neu trinh duyet khong tu mo):\n" + url + "\n")
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    got = done.wait(timeout)
    httpd.shutdown()
    if not got:
        raise RuntimeError("consent timed out")
    if "code" not in result:
        raise RuntimeError(f"consent failed: {result}")
    if result.get("state") != state:
        raise RuntimeError("state mismatch (possible CSRF) — aborted")

    tok = _post_token({
        "client_id": cid, "client_secret": sec,
        "code": result["code"], "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    })
    if "refresh_token" not in tok:
        raise RuntimeError(f"no refresh_token returned (ensure consent screen granted offline access): {tok}")
    return tok
