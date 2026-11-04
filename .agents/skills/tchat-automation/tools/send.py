#!/usr/bin/env python3
"""TChat — send a chat message over the SocketCluster realtime socket.

Protocol (verified from a captured WebSocket session):
  - connect <FCHAT_WS_URL in .env>
    with header  Sec-WebSocket-Protocol: <JWT access token>
    and          Origin: <FCHAT_WEB_ORIGIN in .env>
  - server sends {"type":"Connected"}
  - send: {"type":"message","data":{
             "requestId": <uuid>, "content": <text>, "groupId": <id>,
             "groupType": <TYPE>, "type": "TEXT", "metadata": {},
             "senderId": <my user id> }}
  - server echoes the message back (with messageIdInc) -> delivery confirmed.

⚠️  END-TO-END ENCRYPTION: in "secure" conversations the client encrypts
`content` (beatchat keypair) before sending. This tool sends content as
PLAINTEXT, which is correct ONLY for non-secure conversations. Sending plaintext
into a secure group will store garbage / be rejected. The crypto path is NOT
implemented (see SKILL.md gaps).

⚠️  This sends a real message visible to real people. Dry-run by default; pass
--yes only after the user explicitly confirms.

Recall (delete for everyone) — verified WS frame:
  {"type":"message","data":{"requestId":<uuid>,"type":"ALL_DELETE","groupId":<id>,
     "senderId":<my id>,"metadata":{"allDelete":{"refs":[{"refId":<messageIdInc>}]}}}}
  server replies type "DELETED" with isDeletedForAll:true.

Usage:
  python send.py text   --group GROUP_ID --content "hello" [--group-type TYPE] [--yes]
  python send.py recall --group GROUP_ID --inc MESSAGE_ID_INC [--yes]
"""

import argparse
import json
import sys
import uuid

import client
import config
import tokens
import ws_client


def _resolve_sender_id() -> str:
    r = client.api_get("/user/me")
    client.check_error(r, "get_me(for send)")
    return r.get("id")


def _resolve_group_type(group_id: str):
    """Best-effort: read group detail and pull a groupType/type field."""
    r = client.api_get(f"/group-management/group/{group_id}")
    if isinstance(r, dict) and not r.get("error"):
        for k in ("groupType", "type"):
            if r.get(k):
                return r[k], r
    return None, r


def build_frame(group_id, content, sender_id, group_type, request_id=None,
                metadata=None) -> dict:
    return {
        "type": "message",
        "data": {
            "requestId": request_id or str(uuid.uuid4()),
            "content": content,
            "groupId": group_id,
            "groupType": group_type,
            "type": "TEXT",
            "metadata": metadata or {},
            "senderId": sender_id,
        },
    }


def with_mentions_prefix(body, people):
    """Prefix `body` with inline @mentions and return (content, metadata).

    `people` = list of (display_name, user_id); entries with a falsy id/name are
    skipped (text-only, no ping). Verified wire format (see SKILL.md): the literal
    '@<displayName>' sits in `content`, and metadata.mentions carries
    {userId, target, length, offset} where `offset` is the CODE-POINT index of '@'
    in content and `length` = len('@' + displayName). Use 'EVERYONE' as the id to
    tag @All.
    """
    tokens, mentions, offset = [], [], 0
    for name, uid in people:
        if not name or not uid:
            continue
        tok = f"@{name}"
        mentions.append({"userId": uid, "target": name,
                         "length": len(tok), "offset": offset})
        tokens.append(tok)
        offset += len(tok) + 1   # +1 for the space separating tokens / body
    if not mentions:
        return body, {}
    content = " ".join(tokens) + (f" {body}" if body else "")
    return content, {"mentions": mentions}


def send_text(group_id, content, group_type=None, sender_id=None, timeout=10,
              metadata=None) -> dict:
    sender_id = sender_id or _resolve_sender_id()
    if not group_type:
        group_type, _ = _resolve_group_type(group_id)
    if not group_type:
        return {"error": True, "status": 0,
                "message": "could not resolve groupType; pass --group-type explicitly"}

    frame = build_frame(group_id, content, sender_id, group_type, metadata=metadata)
    req_id = frame["data"]["requestId"]

    ws = ws_client.WebSocket(
        config.ws_url(),
        subprotocols=[tokens.ensure_fresh()],
        origin=config.web_origin(),
        timeout=timeout,
        verify=config.verify_ssl(),
    )
    try:
        ws.send_text(json.dumps(frame))
        # read frames briefly to catch the echo confirming our requestId
        confirmed = None
        for _ in range(40):
            try:
                msg = ws.recv()
            except Exception:
                break
            if msg is None:
                break
            try:
                obj = json.loads(msg) if isinstance(msg, str) else None
            except Exception:
                obj = None
            if isinstance(obj, dict):
                d = obj.get("data") or {}
                if isinstance(d, dict) and d.get("requestId") == req_id and d.get("messageIdInc"):
                    confirmed = obj
                    break
        return {
            "sent": True,
            "requestId": req_id,
            "confirmed": bool(confirmed),
            "echo": confirmed,
        }
    finally:
        ws.close()


def recall_message(group_id, message_id_inc, sender_id=None, timeout=10) -> dict:
    """[WRITE] Recall (delete for everyone) a message via WS ALL_DELETE."""
    sender_id = sender_id or _resolve_sender_id()
    req_id = str(uuid.uuid4())
    frame = {"type": "message", "data": {
        "requestId": req_id, "type": "ALL_DELETE", "groupId": group_id,
        "senderId": sender_id,
        "metadata": {"allDelete": {"refs": [{"refId": int(message_id_inc)}]}},
    }}
    ws = ws_client.WebSocket(config.ws_url(), subprotocols=[config.ws_token()],
                             origin=config.web_origin(), timeout=timeout,
                             verify=config.verify_ssl())
    try:
        ws.send_text(json.dumps(frame))
        for _ in range(40):
            try:
                msg = ws.recv()
            except Exception:
                break
            if msg is None:
                break
            try:
                obj = json.loads(msg) if isinstance(msg, str) else None
            except Exception:
                obj = None
            if isinstance(obj, dict):
                d = obj.get("data") or {}
                if d.get("messageIdInc") == int(message_id_inc) and d.get("type") == "DELETED":
                    return {"recalled": True, "messageIdInc": int(message_id_inc),
                            "isDeletedForAll": d.get("isDeletedForAll")}
                if d.get("requestId") == req_id and d.get("error"):
                    return {"error": True, "status": 0, "message": d.get("message")}
        return {"recalled": False, "messageIdInc": int(message_id_inc),
                "note": "no DELETED confirmation received"}
    finally:
        ws.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(0)
    cmd = sys.argv[1]

    if cmd == "recall":
        missing = config.validate()
        if missing:
            print(f"[ERROR] Missing config: {', '.join(missing)}", file=sys.stderr)
            sys.exit(1)
        p = argparse.ArgumentParser(prog="send.py recall")
        p.add_argument("--group", dest="group_id", required=True)
        p.add_argument("--inc", dest="inc", type=int, required=True, help="messageIdInc to recall")
        p.add_argument("--sender", dest="sender_id", default=None)
        p.add_argument("--yes", action="store_true", help="actually recall; omit for dry-run")
        a = p.parse_args(sys.argv[2:])
        if not a.yes:
            print(f"[DRY-RUN] Would recall (delete for everyone) messageIdInc={a.inc} "
                  f"in group {a.group_id}. Re-run with --yes.", file=sys.stderr)
            sys.exit(0)
        client.print_json(recall_message(a.group_id, a.inc, a.sender_id))
    elif cmd == "text":
        missing = config.validate()
        if missing:
            print(f"[ERROR] Missing config: {', '.join(missing)}", file=sys.stderr)
            sys.exit(1)
        p = argparse.ArgumentParser(prog="send.py text")
        p.add_argument("--group", dest="group_id", required=True)
        p.add_argument("--content", required=True)
        p.add_argument("--group-type", dest="group_type", default=None,
                       help="e.g. SUPER_PRIVATE / PRIVATE / PUBLIC (auto-detected if omitted)")
        p.add_argument("--sender", dest="sender_id", default=None, help="my user id (default: /user/me)")
        p.add_argument("--yes", action="store_true", help="actually send; omit for dry-run")
        a = p.parse_args(sys.argv[2:])

        if not a.yes:
            sender = a.sender_id or "<resolved from /user/me at send time>"
            gtype = a.group_type or "<auto-detected from group detail>"
            preview = build_frame(a.group_id, a.content, sender, gtype)
            print("[DRY-RUN] No message sent. Re-run with --yes to send this frame:",
                  file=sys.stderr)
            print("[DRY-RUN] WARNING: plaintext content — only correct for NON-secure groups.",
                  file=sys.stderr)
            client.print_json(preview)
            sys.exit(0)

        client.print_json(send_text(a.group_id, a.content, a.group_type, a.sender_id))
    else:
        print(f"Unknown command: {cmd}\n", file=sys.stderr)
        print(__doc__)
        sys.exit(1)
