#!/usr/bin/env python3
"""TChat — group / conversation tools (read-only).

Endpoints verified from authenticated traffic capture.

Usage:
  python groups.py list [--limit N] [--before ISO_DATETIME] [--filter DIRECT_CHAT]
        # GET /group-management/group  (?limit=&latestMessageAt=&filter=)  newest first;
        # --before is the latestMessageAt cursor: returns conversations older than it.
        # Response: {"pins":[...], "regulars":[...]} (message.content is E2E ciphertext)
  python groups.py search [--q TEXT] [--limit N] [--page N]
        # GET /group-management/group/search/all  (global conversation search)
  python groups.py get <group_id>                 # GET /group-management/group/{id}
  python groups.py participants <group_id> [--limit N] [--page N]
                                                  # GET .../group/{id}/participant
  python groups.py setting <group_id>             # GET /group-management/group-setting/{id}
  python groups.py folders                        # GET /group-management/group-folder
"""

import argparse
import sys

import client
import config


def list_groups(limit=30, before=None, flt=None) -> dict:
    r = client.api_get("/group-management/group",
                       {"limit": limit, "latestMessageAt": before, "filter": flt})
    client.check_error(r, "list_groups")
    return r


def search_groups(q=None, limit=30, page=1) -> dict:
    r = client.api_get("/group-management/group/search/all",
                       {"search": q, "limit": limit, "page": page})
    client.check_error(r, "search_groups")
    return r


def get_group(group_id: str) -> dict:
    r = client.api_get(f"/group-management/group/{group_id}")
    client.check_error(r, "get_group")
    return r


def get_participants(group_id: str, limit=50, page=1) -> dict:
    r = client.api_get(f"/group-management/group/{group_id}/participant",
                       {"limit": limit, "page": page})
    client.check_error(r, "get_participants")
    return r


def get_group_setting(group_id: str) -> dict:
    r = client.api_get(f"/group-management/group-setting/{group_id}")
    client.check_error(r, "get_group_setting")
    return r


def list_folders() -> dict:
    r = client.api_get("/group-management/group-folder")
    client.check_error(r, "list_folders")
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
    if cmd == "list":
        p = argparse.ArgumentParser(prog="groups.py list")
        p.add_argument("--limit", type=int, default=30)
        p.add_argument("--before", default=None, help="latestMessageAt ISO cursor")
        p.add_argument("--filter", dest="flt", default=None, help="e.g. DIRECT_CHAT")
        a = p.parse_args(sys.argv[2:])
        client.print_json(list_groups(a.limit, a.before, a.flt))
    elif cmd == "search":
        p = argparse.ArgumentParser(prog="groups.py search")
        p.add_argument("--q", default=None)
        p.add_argument("--limit", type=int, default=30)
        p.add_argument("--page", type=int, default=1)
        a = p.parse_args(sys.argv[2:])
        client.print_json(search_groups(a.q, a.limit, a.page))
    elif cmd == "get":
        if len(sys.argv) < 3:
            print("usage: groups.py get <group_id>", file=sys.stderr); sys.exit(1)
        client.print_json(get_group(sys.argv[2]))
    elif cmd == "participants":
        p = argparse.ArgumentParser(prog="groups.py participants")
        p.add_argument("group_id")
        p.add_argument("--limit", type=int, default=50)
        p.add_argument("--page", type=int, default=1)
        a = p.parse_args(sys.argv[2:])
        client.print_json(get_participants(a.group_id, a.limit, a.page))
    elif cmd == "setting":
        if len(sys.argv) < 3:
            print("usage: groups.py setting <group_id>", file=sys.stderr); sys.exit(1)
        client.print_json(get_group_setting(sys.argv[2]))
    elif cmd == "folders":
        client.print_json(list_folders())
    else:
        print(f"Unknown command: {cmd}\n", file=sys.stderr)
        print(__doc__)
        sys.exit(1)
