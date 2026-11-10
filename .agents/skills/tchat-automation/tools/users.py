#!/usr/bin/env python3
"""TChat — user & directory tools (read-only).

Endpoints verified from authenticated traffic capture.

Usage:
  python users.py me                         # current user profile  (GET /user/me)
  python users.py setting                    # user settings         (GET /user/setting)
  python users.py search [--q TEXT] [--group GROUP_ID] [--limit N] [--page N]
                                             # directory search      (GET /user/search)
  python users.py lookup --ids ID1,ID2       # batch lookup by id    (POST /user/search)
  python users.py server-key                 # E2E server RSA key    (GET /key-manager/rsa/get-server-key)
"""

import argparse
import sys

import client
import config


def get_me() -> dict:
    r = client.api_get("/user/me")
    client.check_error(r, "get_me")
    return r


def get_user_setting() -> dict:
    r = client.api_get("/user/setting")
    client.check_error(r, "get_user_setting")
    return r


def search_users(q=None, group_id=None, limit=30, page=1) -> dict:
    r = client.api_get("/user/search",
                       {"q": q, "groupId": group_id, "limit": limit, "page": page})
    client.check_error(r, "search_users")
    return r


def lookup_users(ids, fields=None) -> dict:
    """Batch-resolve users by _id (read-only). Verified body shape from traffic."""
    id_list = ids if isinstance(ids, list) else [s for s in str(ids).split(",") if s]
    payload = {
        "id": "_id",
        "value": id_list,
        "fields": fields or ["_id", "displayName", "avatar", "username",
                             "department", "nickname"],
    }
    r = client.api_post("/user/search", payload)
    client.check_error(r, "lookup_users")
    return r


def get_server_rsa_key() -> dict:
    r = client.api_get("/key-manager/rsa/get-server-key")
    client.check_error(r, "get_server_rsa_key")
    return r


if __name__ == "__main__":
    missing = config.validate()
    if missing:
        print(f"[ERROR] Missing config: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(0)

    cmd = sys.argv[1]
    if cmd == "me":
        client.print_json(get_me())
    elif cmd == "setting":
        client.print_json(get_user_setting())
    elif cmd == "search":
        p = argparse.ArgumentParser(prog="users.py search")
        p.add_argument("--q", default=None)
        p.add_argument("--group", dest="group_id", default=None)
        p.add_argument("--limit", type=int, default=30)
        p.add_argument("--page", type=int, default=1)
        a = p.parse_args(sys.argv[2:])
        client.print_json(search_users(a.q, a.group_id, a.limit, a.page))
    elif cmd == "lookup":
        p = argparse.ArgumentParser(prog="users.py lookup")
        p.add_argument("--ids", required=True, help="Comma-separated user _id values")
        a = p.parse_args(sys.argv[2:])
        client.print_json(lookup_users(a.ids))
    elif cmd == "server-key":
        client.print_json(get_server_rsa_key())
    else:
        print(f"Unknown command: {cmd}\n", file=sys.stderr)
        print(__doc__)
        sys.exit(1)
