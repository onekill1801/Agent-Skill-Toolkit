#!/usr/bin/env python3
"""TChat — message-query (read side) tools.

Endpoints verified from authenticated traffic capture.

NOTE: message *content* is END-TO-END ENCRYPTED (beatchat, RSA-OAEP). `list` now
AUTO-DECRYPTS via crypto.py IF the private key is present
(work/secrets/fchat_private.pem — dựng bằng `crypto.py unwrap-indexeddb`). Không có
key → trả ciphertext như cũ. Tắt giải mã: `--no-decrypt`. Metadata luôn dùng được.

Usage:
  python messages.py list <group_id> [--limit N]
        # GET /message-query/group/{id}/message
  python messages.py scheduled <group_id>
        # GET /message-query/group/{id}/scheduled-message
  python messages.py media <group_id> --type MEDIA|FILE|LINK|VOICE [--limit N]
        # GET /message-query/message/search-media
  python messages.py marked [--status UNDONE] [--limit N]
        # GET /message-query/message/marked-message
  python messages.py count-marked [--status UNDONE]
        # GET /message-query/message/total-marked-message
"""

import argparse
import sys

import client
import config
try:
    import crypto   # E2E decrypt (cần work/secrets/fchat_private.pem); vắng key → no-op
except Exception:   # noqa: BLE001
    crypto = None


def _decrypt_in_place(r: dict) -> dict:
    """Giải mã content các tin TEXT (nếu có key). Không có key/không mã hoá → giữ nguyên."""
    if not crypto or not isinstance(r, dict):
        return r
    for bucket in ("regulars", "pins"):
        for m in r.get(bucket) or []:
            if m.get("type") == "TEXT" and m.get("content"):
                m["content"] = crypto.decrypt_if_needed(m["content"])
    return r


def list_messages(group_id: str, limit=50, decrypt=True) -> dict:
    r = client.api_get(f"/message-query/group/{group_id}/message", {"limit": limit})
    client.check_error(r, "list_messages")
    return _decrypt_in_place(r) if decrypt else r


def list_scheduled(group_id: str) -> dict:
    r = client.api_get(f"/message-query/group/{group_id}/scheduled-message")
    client.check_error(r, "list_scheduled")
    return r


def search_media(group_id: str, media_type: str, limit=30) -> dict:
    r = client.api_get("/message-query/message/search-media",
                       {"groupId": group_id, "type": media_type, "limit": limit})
    client.check_error(r, "search_media")
    return r


def list_marked(status="UNDONE", limit=30) -> dict:
    r = client.api_get("/message-query/message/marked-message",
                       {"status": status, "limit": limit})
    client.check_error(r, "list_marked")
    return r


def count_marked(status="UNDONE") -> dict:
    r = client.api_get("/message-query/message/total-marked-message", {"status": status})
    client.check_error(r, "count_marked")
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
        p = argparse.ArgumentParser(prog="messages.py list")
        p.add_argument("group_id")
        p.add_argument("--limit", type=int, default=50)
        p.add_argument("--no-decrypt", dest="decrypt", action="store_false",
                       help="giữ ciphertext, không tự giải mã E2E")
        a = p.parse_args(sys.argv[2:])
        client.print_json(list_messages(a.group_id, a.limit, a.decrypt))
    elif cmd == "scheduled":
        if len(sys.argv) < 3:
            print("usage: messages.py scheduled <group_id>", file=sys.stderr); sys.exit(1)
        client.print_json(list_scheduled(sys.argv[2]))
    elif cmd == "media":
        p = argparse.ArgumentParser(prog="messages.py media")
        p.add_argument("group_id")
        p.add_argument("--type", required=True, choices=["MEDIA", "FILE", "LINK", "VOICE"])
        p.add_argument("--limit", type=int, default=30)
        a = p.parse_args(sys.argv[2:])
        client.print_json(search_media(a.group_id, a.type, a.limit))
    elif cmd == "marked":
        p = argparse.ArgumentParser(prog="messages.py marked")
        p.add_argument("--status", default="UNDONE")
        p.add_argument("--limit", type=int, default=30)
        a = p.parse_args(sys.argv[2:])
        client.print_json(list_marked(a.status, a.limit))
    elif cmd == "count-marked":
        p = argparse.ArgumentParser(prog="messages.py count-marked")
        p.add_argument("--status", default="UNDONE")
        a = p.parse_args(sys.argv[2:])
        client.print_json(count_marked(a.status))
    else:
        print(f"Unknown command: {cmd}\n", file=sys.stderr)
        print(__doc__)
        sys.exit(1)
