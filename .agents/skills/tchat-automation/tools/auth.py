#!/usr/bin/env python3
"""TChat — QR login helper (best-effort) + session check.

The web client logs in by showing a QR that you scan with the TChat mobile
app. The endpoint paths below are VERIFIED from traffic, but their request/
response BODY shapes are [UNVERIFIED] (not captured). Treat this as a scaffold:
if a step returns an error, capture the real body once (DevTools -> Network ->
the /auth/qr/* request -> Payload/Response) and adjust the field names here.

For day-to-day read-only use you usually DON'T need this — just paste a Bearer
token from a logged-in browser session into FCHAT_BEARER_TOKEN.

Usage:
  python auth.py whoami        # verify current token (GET /user/me)
  python auth.py refresh       # force access-token refresh via POST /auth/refresh-tokens
  python auth.py token-status  # show token expiry / seconds left
  python auth.py qr-generate   # [unverified] start QR login, print QR token
  python auth.py qr-scan <qr_token>      # [unverified] poll waiting-scan
  python auth.py qr-confirm <qr_token>   # [unverified] poll waiting-confirm -> token
  python auth.py logout        # POST /auth/logout
"""

import sys

import client
import config
import tokens


def whoami() -> dict:
    r = client.api_get("/user/me")
    client.check_error(r, "whoami")
    return r


def qr_generate() -> dict:
    # [VERIFIED from HAR] body is {} -> response {"token": "<qr_token>"}
    return client.api_post("/auth/qr/generate", {}, auth=False)


def qr_waiting_scan(qr_token: str) -> dict:
    # [Inference] reuse the token from qr_generate; scan/confirm bodies not captured
    return client.api_post("/auth/qr/waiting-scan", {"token": qr_token}, auth=False)


def qr_waiting_confirm(qr_token: str) -> dict:
    # [Inference] reuse the token from qr_generate; response should carry the session JWT
    return client.api_post("/auth/qr/waiting-confirm", {"token": qr_token}, auth=False)


def logout() -> dict:
    return client.api_post("/auth/logout", {})


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(0)
    cmd = sys.argv[1]

    if cmd == "whoami":
        missing = config.validate()
        if missing:
            print(f"[ERROR] Missing config: {', '.join(missing)}", file=sys.stderr)
            sys.exit(1)
        client.print_json(whoami())
    elif cmd == "refresh":
        r = tokens.refresh()
        if r.get("error"):
            client.print_json(r)
        else:  # redact token values
            client.print_json({
                "refreshed": True,
                "access_token": f"<{len(r['access_token'])} chars>",
                "refresh_token": f"<{len(r['refresh_token'])} chars>",
                "access_exp": tokens.exp(r["access_token"]),
            })
    elif cmd == "token-status":
        import time as _t
        acc = tokens.current_access()
        e = tokens.exp(acc)
        client.print_json({
            "has_access": bool(acc),
            "has_refresh": bool(tokens.current_refresh()),
            "access_exp": e,
            "access_seconds_left": int(e - _t.time()) if e else None,
            "expiring_soon": tokens.is_expiring(acc),
        })
    elif cmd == "qr-generate":
        client.print_json(qr_generate())
    elif cmd == "qr-scan":
        if len(sys.argv) < 3:
            print("usage: auth.py qr-scan <qr_token>", file=sys.stderr); sys.exit(1)
        client.print_json(qr_waiting_scan(sys.argv[2]))
    elif cmd == "qr-confirm":
        if len(sys.argv) < 3:
            print("usage: auth.py qr-confirm <qr_token>", file=sys.stderr); sys.exit(1)
        client.print_json(qr_waiting_confirm(sys.argv[2]))
    elif cmd == "logout":
        client.print_json(logout())
    else:
        print(f"Unknown command: {cmd}\n", file=sys.stderr)
        print(__doc__)
        sys.exit(1)
