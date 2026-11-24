#!/usr/bin/env python3
"""Listen for incoming TChat messages over the SocketCluster realtime socket.

Behaviour:
  - DIRECT (1-1) messages from someone else  -> spawn a Claude Code terminal
    (your Pro subscription, no API key) to draft & send a reply via send.py.
  - GROUP messages                           -> log only, never auto-reply.
  - your own messages / typing / seen / etc. -> ignored.

Transport (verified): <FCHAT_WS_URL in .env>, JWT in
Sec-WebSocket-Protocol. Incoming items arrive on topic user_<myId> as
{"type":"message","data":{... ,"type":"TEXT","content","senderId",
"group":{"isDirectChat":bool,"name":...}, "messageIdInc", ...}}.

Heartbeat 'ping' keeps the socket alive; auto-reconnect + token refresh on drop.
Runs until Ctrl+C.

Usage:
  python listen.py                       # reply mode = claude (default)
  python listen.py --reply notify        # just print DMs, no auto-reply
  python listen.py --reply off           # log everything, take no action
  python listen.py --cooldown 30         # min seconds between claude spawns per chat
"""

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import time

import client
import config
import tokens
import ws_client
try:
    import crypto   # E2E decrypt (cần work/secrets/fchat_private.pem); vắng key → no-op
except Exception:   # noqa: BLE001
    crypto = None

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(TOOLS_DIR, "..", "..", "..", ".."))

# import the existing fork-terminal tool (used on macOS/Linux)
_FT_DIR = os.path.abspath(os.path.join(TOOLS_DIR, "..", "..", "fork-terminal", "tools"))
sys.path.insert(0, _FT_DIR)
try:
    import fork_terminal  # noqa: E402
except Exception:
    fork_terminal = None

# context files for forked Claude sessions live in gitignored temp/
_INBOX = os.path.join(REPO_ROOT, "temp", "fchat_incoming")


def _now():
    return time.strftime("%H:%M:%S")


def _looks_encrypted(text) -> bool:
    if not isinstance(text, str) or len(text) < 40 or " " in text:
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9+/=]+", text))


def _fetch_history(gid, me, limit=40):
    """Recent TEXT messages in this conversation, oldest->newest, labelled.

    'Tôi' = the account owner's own messages (so Claude can mimic their style);
    everyone else by display name. Encrypted content shows as a placeholder.
    """
    r = client.api_get(f"/message-query/group/{gid}/message", {"limit": limit})
    if not isinstance(r, dict) or r.get("error"):
        return []
    lines = []
    for m in sorted(r.get("regulars") or [], key=lambda x: x.get("messageIdInc") or 0):
        if m.get("type") != "TEXT":
            continue
        c = m.get("content")
        if not c:
            continue
        if _looks_encrypted(c):
            c = crypto.decrypt_if_needed(c) if crypto else "<đã mã hoá>"
        if _looks_encrypted(c):        # vẫn là ciphertext (không có key / lỗi)
            c = "<đã mã hoá>"
        elif len(c) > 400:
            c = c[:400] + "…"
        who = "Tôi" if m.get("senderId") == me else ((m.get("user") or {}).get("displayName") or "Họ")
        lines.append(f"[{who}] {c}")
    return lines


def _qpath(gid):
    return os.path.join(_INBOX, f"queue_{gid}.jsonl")


def _lockpath(gid):
    return os.path.join(_INBOX, f"worker_{gid}.lock")


def _worker_alive(gid) -> bool:
    """A worker owns this conversation if its lock heartbeat is recent (<90s)."""
    p = _lockpath(gid)
    if not os.path.isfile(p):
        return False
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        return (time.time() - d.get("heartbeat", 0)) < 90
    except Exception:
        return False


def _enqueue(gid, ev, fresh=False):
    os.makedirs(_INBOX, exist_ok=True)
    with open(_qpath(gid), "w" if fresh else "a", encoding="utf-8") as f:
        f.write(json.dumps(ev, ensure_ascii=False) + "\n")


def _spawn_worker(gid):
    """Open a terminal running reply_worker.py for this conversation."""
    worker = os.path.normpath(os.path.join(TOOLS_DIR, "reply_worker.py"))
    py = "python" if os.name == "nt" else "python3"
    try:
        if os.name == "nt":  # avoid fork_terminal (its `start ... && ...` drops the command)
            launcher = os.path.normpath(os.path.join(_INBOX, f"launch_{gid}.cmd"))
            with open(launcher, "w", encoding="utf-8") as f:
                f.write("@echo off\r\n")
                f.write(f'cd /d "{os.path.normpath(TOOLS_DIR)}"\r\n')
                f.write(f'{py} "{worker}" {gid}\r\n')
            subprocess.Popen(f'start "TChat: {gid}" cmd /k "{launcher}"', shell=True)
        elif fork_terminal is not None:
            fork_terminal.fork_terminal(f'cd "{TOOLS_DIR}" && {py} reply_worker.py {gid}')
        else:
            print(f"[{_now()}] [WARN] no terminal spawner available", file=sys.stderr)
            return
        print(f"[{_now()}]        -> mở worker terminal cho hội thoại {gid}")
    except Exception as e:
        print(f"[{_now()}] [WARN] không mở được worker: {e}", file=sys.stderr)


def handle_message(obj, me, reply_mode, seen):
    data = obj.get("data") or {}
    if data.get("type") != "TEXT":
        return
    sender = data.get("senderId")
    if not sender or sender == me:
        return  # ignore own messages (prevents reply loops)
    gid = data.get("groupId")
    inc = data.get("messageIdInc")
    key = (gid, inc)
    if key in seen:
        return
    seen.add(key)

    group = data.get("group") or {}
    is_direct = group.get("isDirectChat")
    user = data.get("user") or {}
    sender_name = user.get("displayName") or sender
    gname = group.get("name") or gid
    content = data.get("content")
    if crypto:                      # giải mã E2E nếu là ciphertext & có key
        content = crypto.decrypt_if_needed(content)

    if not is_direct:
        print(f"[{_now()}] [GROUP] {gname} | {sender_name}: {content!r}  (chỉ đọc)")
        return

    enc = _looks_encrypted(content)
    shown = "<E2E ciphertext>" if enc else repr(content)
    print(f"[{_now()}] [DM]    {sender_name}: {shown}")

    if reply_mode in ("off", "notify"):
        return
    # reply_mode == "claude": route to a per-conversation worker terminal
    ev = {"messageIdInc": inc, "senderId": sender, "senderName": sender_name,
          "content": content, "groupType": group.get("type") or group.get("groupType") or "SUPER_PRIVATE",
          "encrypted": enc, "ts": int(time.time())}
    if _worker_alive(gid):
        _enqueue(gid, ev)                       # existing terminal picks it up
        print(f"[{_now()}]        -> đưa vào worker đang mở")
    else:
        _enqueue(gid, ev, fresh=True)           # new session: reset the queue
        _spawn_worker(gid)


def run(reply_mode, idle_ping=20):
    me = client.api_get("/user/me").get("id")
    if not me:
        print("[ERROR] cannot resolve current user (token invalid?)", file=sys.stderr)
        sys.exit(1)
    print(f"[{_now()}] listening as {me} | reply={reply_mode} | Ctrl+C to stop")
    seen = set()
    backoff = 2

    while True:
        try:
            ws = ws_client.WebSocket(config.ws_url(), subprotocols=[tokens.ensure_fresh()],
                                     origin=config.web_origin(), timeout=idle_ping,
                                     verify=config.verify_ssl())
            print(f"[{_now()}] connected.")
            backoff = 2
            last_ping = time.time()
            while True:
                try:
                    msg = ws.recv()
                except (socket.timeout, TimeoutError):
                    ws.send_text("ping")          # heartbeat on idle
                    last_ping = time.time()
                    continue
                if msg is None:
                    print(f"[{_now()}] socket closed; reconnecting...")
                    break
                if msg == "pong" or msg == "ping":
                    continue
                try:
                    obj = json.loads(msg) if isinstance(msg, str) else None
                except Exception:
                    obj = None
                if isinstance(obj, dict) and obj.get("type") == "message":
                    handle_message(obj, me, reply_mode, seen)
                if time.time() - last_ping > idle_ping:
                    ws.send_text("ping")
                    last_ping = time.time()
            ws.close()
        except KeyboardInterrupt:
            print(f"\n[{_now()}] stopped.")
            return
        except Exception as e:
            print(f"[{_now()}] connection error: {e}; retry in {backoff}s", file=sys.stderr)
            # token may have expired — refresh proactively before retry
            tokens.refresh(verbose=False)
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)


if __name__ == "__main__":
    missing = config.validate()
    if missing:
        print(f"[ERROR] Missing config: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)
    p = argparse.ArgumentParser(prog="listen.py")
    p.add_argument("--reply", choices=["claude", "notify", "off"], default="claude")
    a = p.parse_args()
    try:
        run(a.reply)
    except KeyboardInterrupt:
        print("\nstopped.")
