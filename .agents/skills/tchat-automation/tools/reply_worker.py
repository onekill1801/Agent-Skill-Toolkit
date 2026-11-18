#!/usr/bin/env python3
"""Per-conversation reply worker for TChat DMs (one terminal per conversation).

Reads queued incoming messages for ONE conversation, asks Claude (Pro CLI,
headless `claude -p`) to draft a reply in the owner's style (learned from the
conversation history), shows it, and on a y/N confirm sends it via send.py.
Stays alive until 15 minutes after the last message, then exits & cleans up.

Coordination with listen.py (same temp/fchat_incoming dir):
  queue_<gid>.jsonl  — incoming messages (one JSON per line), appended by listener
  worker_<gid>.lock  — {pid, heartbeat}; presence+fresh heartbeat = worker alive

Usage:  python reply_worker.py <group_id>
"""

import json
import os
import subprocess
import sys
import threading
import time

import client
import config
import listen   # reuse _INBOX, _fetch_history, _looks_encrypted
import send
import style_profile   # per-conversation saved messaging style

IDLE_SECONDS = 15 * 60
POLL = 1
INBOX = listen._INBOX


def _debounce_seconds():
    try:
        return max(0, int(config.get("FCHAT_REPLY_DEBOUNCE", "10") or 10))
    except ValueError:
        return 10


def _qpath(gid):
    return os.path.join(INBOX, f"queue_{gid}.jsonl")


def _lockpath(gid):
    return os.path.join(INBOX, f"worker_{gid}.lock")


def _read_events(gid):
    p = _qpath(gid)
    if not os.path.isfile(p):
        return []
    out = []
    with open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))   # skip a partial trailing line silently
            except Exception:
                pass
    return out


def _draft_reply(gid, me, evs):
    """Ask `claude -p` to draft ONE style-matched reply for all pending messages."""
    hist = listen._fetch_history(gid, me, limit=40)
    transcript = "\n".join(hist) if hist else "(chưa có lịch sử)"
    name = (evs[-1].get("senderName") if evs else None) or "Họ"
    new_msgs = "\n".join(f"- {e.get('content')}" for e in evs)
    plural = "các tin nhắn mới" if len(evs) > 1 else "tin mới"
    # Saved per-conversation style profile (if any) — takes priority over history.
    profile = style_profile.get(gid)
    profile_block = (
        f"--- PROFILE GIỌNG VỚI NGƯỜI NÀY (ƯU TIÊN TUÂN THEO) ---\n{profile.strip()}\n\n"
        if profile.strip() else ""
    )
    prompt = (
        "Bạn đang đóng vai CHỦ TÀI KHOẢN, trả lời tin nhắn riêng trên TChat.\n"
        "Viết GIỐNG HỆT phong cách của 'Tôi': xưng hô, độ dài câu, giọng điệu "
        "(trang trọng/thân mật), dùng emoji/teencode hay không, ngôn ngữ.\n"
        "Nếu có PROFILE GIỌNG bên dưới, TUÂN THEO nó trước; lịch sử để lấy ngữ cảnh.\n"
        f"Người kia vừa gửi {len(evs)} tin liên tiếp — hãy trả lời MỘT lần cho TẤT CẢ, "
        "tự nhiên, đúng ngữ cảnh.\n\n"
        f"{profile_block}"
        f"--- LỊCH SỬ TRÒ CHUYỆN ('Tôi' = chủ tài khoản) ---\n{transcript}\n\n"
        f"--- {plural.upper()} TỪ {name} ---\n{new_msgs}\n\n"
        "Chỉ in ra DUY NHẤT nội dung câu trả lời (không giải thích, không ngoặc kép bao quanh)."
    )
    model = config.get("FCHAT_REPLY_MODEL", "sonnet") or "sonnet"
    try:
        r = subprocess.run(["claude", "-p", "--model", model, prompt], capture_output=True,
                           text=True, encoding="utf-8", timeout=180)
    except FileNotFoundError:
        return None, "không tìm thấy lệnh 'claude' trên PATH"
    except Exception as e:
        return None, str(e)
    if r.returncode != 0:
        return None, (r.stderr or "claude trả về lỗi").strip()[:300]
    return (r.stdout or "").strip(), None


def _heartbeat(gid, stop):
    while not stop.is_set():
        try:
            with open(_lockpath(gid), "w", encoding="utf-8") as f:
                json.dump({"pid": os.getpid(), "heartbeat": int(time.time())}, f)
        except Exception:
            pass
        stop.wait(30)


def main(gid):
    missing = config.validate()
    if missing:
        print(f"[worker] thiếu config: {', '.join(missing)}")
        input("Enter để đóng...")
        return
    me = client.api_get("/user/me").get("id")
    debounce = _debounce_seconds()
    stop = threading.Event()
    threading.Thread(target=_heartbeat, args=(gid, stop), daemon=True).start()
    print(f"[worker] hội thoại {gid} | chờ {debounce}s im lặng rồi mới trả lời | "
          f"tự đóng sau {IDLE_SECONDS // 60}' | Ctrl+C để dừng\n")

    processed = 0
    pending = []          # unanswered messages, accumulated
    last_event = 0.0      # time the most recent message arrived
    last_activity = time.time()
    try:
        while True:
            events = _read_events(gid)
            if len(events) > processed:
                for ev in events[processed:]:
                    name = ev.get("senderName") or ev.get("senderId")
                    if ev.get("encrypted"):
                        print(f"[{name}] <tin mã hoá E2E — không đọc được, bỏ qua>")
                        continue
                    print(f"[{name}] {ev.get('content')}")
                    pending.append(ev)
                processed = len(events)
                last_event = time.time()
                last_activity = time.time()
                if pending:
                    print(f"  … chờ {debounce}s xem còn tin nữa không …")

            # reply only after a quiet gap (the other person seems done)
            if pending and (time.time() - last_event) >= debounce:
                print(f"  … Claude đang soạn trả lời cho {len(pending)} tin, theo phong cách của bạn …")
                reply, err = _draft_reply(gid, me, pending)
                if err or not reply:
                    print(f"  [không soạn được] {err or 'rỗng'}\n")
                    pending = []
                else:
                    print(f"  → Đề xuất trả lời:\n     {reply}")
                    try:
                        ans = input("  Gửi? [y/N] ").strip().lower()
                    except EOFError:
                        ans = "n"
                    if ans == "y":
                        res = send.send_text(gid, reply, pending[-1].get("groupType"))
                        ok = res.get("confirmed") or res.get("sent")
                        print("  ✓ đã gửi.\n" if ok else f"  ✗ lỗi gửi: {res}\n")
                    else:
                        print("  (bỏ qua, không gửi)\n")
                    pending = []
                last_activity = time.time()
                continue

            if not pending and (time.time() - last_activity) > IDLE_SECONDS:
                print(f"[worker] {IDLE_SECONDS // 60} phút không có tin mới — đóng.")
                break
            time.sleep(POLL)
    except KeyboardInterrupt:
        print("\n[worker] đã dừng.")
    finally:
        stop.set()
        for p in (_lockpath(gid), _qpath(gid)):
            try:
                os.remove(p)
            except Exception:
                pass


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python reply_worker.py <group_id>")
        sys.exit(1)
    main(sys.argv[1])
