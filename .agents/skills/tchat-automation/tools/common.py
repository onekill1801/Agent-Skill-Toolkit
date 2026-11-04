#!/usr/bin/env python3
"""TChat — common / misc read-only tools.

Usage:
  python common.py settings                 # GET /common/settings (no auth)
  python common.py notification-setting     # GET /notification/setting
  python common.py stickers [--page N] [--limit N]   # GET /common/sticker/favorite
"""

import argparse
import sys

import client
import config


def get_common_settings() -> dict:
    # /common/settings is public (no Authorization required)
    r = client.api_get("/common/settings", auth=False)
    client.check_error(r, "get_common_settings")
    return r


def get_notification_setting() -> dict:
    r = client.api_get("/notification/setting")
    client.check_error(r, "get_notification_setting")
    return r


def list_favorite_stickers(page=1, limit=30) -> dict:
    r = client.api_get("/common/sticker/favorite", {"page": page, "limit": limit})
    client.check_error(r, "list_favorite_stickers")
    return r


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(0)
    cmd = sys.argv[1]

    # /common/settings needs no token; other commands do.
    if cmd != "settings":
        missing = config.validate()
        if missing:
            print(f"[ERROR] Missing config: {', '.join(missing)}", file=sys.stderr)
            sys.exit(1)

    if cmd == "settings":
        client.print_json(get_common_settings())
    elif cmd == "notification-setting":
        client.print_json(get_notification_setting())
    elif cmd == "stickers":
        p = argparse.ArgumentParser(prog="common.py stickers")
        p.add_argument("--page", type=int, default=1)
        p.add_argument("--limit", type=int, default=30)
        a = p.parse_args(sys.argv[2:])
        client.print_json(list_favorite_stickers(a.page, a.limit))
    else:
        print(f"Unknown command: {cmd}\n", file=sys.stderr)
        print(__doc__)
        sys.exit(1)
