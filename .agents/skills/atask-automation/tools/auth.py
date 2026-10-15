#!/usr/bin/env python3
"""Personal Access Token (PAT) management for aTask AI-agent.

NOTE: 'create' requires a session JWT (OAuth2/cookie), NOT the PAT.
      Run the curl command manually to bootstrap ATASK_PAT_TOKEN.
      ENABLE_PAT_MGMT=false by default — enable only when managing tokens.

Usage:
  python3 auth.py list
  python3 auth.py revoke <pat_id>

  # Bootstrap (requires session JWT, run in your browser's dev tools or curl):
  #   curl -X POST $ATASK_BASE_URL/api/account/tokens \\
  #     -H "Authorization: Bearer <SESSION_JWT>" \\
  #     -H "Content-Type: application/json" \\
  #     -d '{"name":"claude-skill","scopes":["task:read","task:write","checklist:read","checklist:write","comment:read","comment:write","workspace:read","project:read","sprint:read","list:read"]}'
"""

import sys

import client
import config


def list_pats() -> dict:
    """List all active PATs for the current user."""
    r = client.api_get("/api/account/tokens")
    client.check_error(r, "list_pats")
    return r


def revoke_pat(pat_id: str) -> dict:
    """Revoke (delete) a PAT by ID."""
    r = client.api_delete(f"/api/account/tokens/{pat_id}")
    # DELETE returns 204 No Content → empty dict is success
    if r.get("error"):
        client.check_error(r, "revoke_pat")
    return {"revoked": pat_id, "status": "ok"}


if __name__ == "__main__":
    missing = config.validate()
    if missing:
        print(f"[ERROR] Missing config: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)

    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(0)

    cmd = sys.argv[1]

    if cmd == "list":
        client.print_json(list_pats())

    elif cmd == "revoke":
        if len(sys.argv) < 3:
            print("Usage: auth.py revoke <pat_id>", file=sys.stderr)
            sys.exit(1)
        pat_id = sys.argv[2]
        client.print_json(revoke_pat(pat_id))

    else:
        print(f"Unknown command: {cmd}\n", file=sys.stderr)
        print(__doc__)
        sys.exit(1)
