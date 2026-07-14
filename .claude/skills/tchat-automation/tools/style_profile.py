#!/usr/bin/env python3
"""Per-conversation messaging STYLE PROFILE store for TChat.

Lưu "phong cách nhắn của chủ tài khoản với từng người" để bản nháp/auto-reply soạn
ĐÚNG GIỌNG bạn dùng với người đó, ổn định qua các lần.

Profile sống ở `<skill>/profiles/<conversation_id>.md` — **gitignored** (dữ liệu cá nhân).
Hybrid learning (do chat E2E mã hoá): `gather` kéo phần ĐỌC ĐƯỢC (tin bạn gửi + tin
plaintext; tin mã hoá bị bỏ qua) để agent phân tích; agent ghi profile bằng `save`.
`reply_worker.py` nạp profile qua `get` và chèn vào prompt khi soạn.

Zero pip — stdlib only.

Usage:
  python style_profile.py list
  python style_profile.py path <gid>
  python style_profile.py get <gid>
  python style_profile.py gather <gid> [--limit N]     # raw material đọc được để phân tích
  python style_profile.py save <gid> [--file F | --stdin] [--name "Tên hiển thị"]
  python style_profile.py template [--gid <gid>] [--name "Tên"]
"""

import argparse
import json
import os
import sys

import config  # noqa: F401  (loads .env; also keeps tool consistent with the skill)

for _s in (sys.stdin, sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

_PROFILE_DIR = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "profiles"))

TEMPLATE = """---
conversation_id: {gid}
display_name: {name}
---
# Phong cách nhắn với {name}

- **Xưng hô** (tôi ↔ họ): <vd: em ↔ anh / mình ↔ bạn / tôi ↔ anh>
- **Mức trang trọng**: <thân mật | trung tính | trang trọng>
- **Ngôn ngữ**: <tiếng Việt | Anh | trộn | teencode>
- **Emoji / icon**: <hay dùng gì, hay không>
- **Độ dài câu**: <ngắn gọn | đầy đủ>
- **Câu chào / mở đầu**: <vd: "hi anh", "dạ anh", ...>
- **Câu kết / chốt**: <vd: "ok anh nhé", "thanks anh">
- **Mẫu câu / từ hay dùng**: <liệt kê>
- **Tránh**: <điều không nên dùng với người này>

> Nguồn: <auto-gather plaintext + tin tự gửi> + chỉnh tay. Cập nhật khi giọng đổi.
"""


def _dir() -> str:
    os.makedirs(_PROFILE_DIR, exist_ok=True)
    return _PROFILE_DIR


def profile_path(gid: str) -> str:
    safe = "".join(c for c in str(gid) if c.isalnum() or c in "-_.")
    return os.path.join(_dir(), f"{safe}.md")


def get(gid: str) -> str:
    """Return profile text for a conversation, or '' if none."""
    p = profile_path(gid)
    if os.path.isfile(p):
        with open(p, encoding="utf-8") as f:
            return f.read()
    return ""


def save(gid: str, text: str) -> str:
    p = profile_path(gid)
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)
    return p


def list_profiles() -> list:
    out = []
    if os.path.isdir(_PROFILE_DIR):
        for fn in sorted(os.listdir(_PROFILE_DIR)):
            if fn.endswith(".md"):
                out.append(fn[:-3])
    return out


def gather(gid: str, limit: int = 40) -> dict:
    """Readable raw material for the agent to synthesize a style profile.

    Reuses the listener's history fetch (labels '[Tôi]' = your own messages — the
    primary signal — and skips/marks E2E-encrypted ciphertext).
    """
    import client
    import listen
    me = client.api_get("/user/me").get("id")
    hist = listen._fetch_history(gid, me, limit=limit)
    return {
        "conversation_id": gid,
        "me": me,
        "history": hist,
        "note": ("Dòng '[Tôi]' = tin bạn đã gửi (nguồn chính để học giọng). "
                 "Tin E2E mã hoá không đọc được nên bị bỏ/đánh dấu — chỉ học từ phần plaintext."),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="TChat per-conversation style profiles")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list")
    sp = sub.add_parser("path"); sp.add_argument("gid")
    sg = sub.add_parser("get"); sg.add_argument("gid")
    sga = sub.add_parser("gather"); sga.add_argument("gid"); sga.add_argument("--limit", type=int, default=40)
    ss = sub.add_parser("save")
    ss.add_argument("gid"); ss.add_argument("--file"); ss.add_argument("--stdin", action="store_true")
    ss.add_argument("--name", default="")
    st = sub.add_parser("template"); st.add_argument("--gid", default="<gid>"); st.add_argument("--name", default="<tên>")

    args = parser.parse_args()

    if args.cmd == "list":
        print(json.dumps({"profiles": list_profiles(), "dir": _PROFILE_DIR}, ensure_ascii=False, indent=2))
    elif args.cmd == "path":
        print(profile_path(args.gid))
    elif args.cmd == "get":
        text = get(args.gid)
        if not text:
            print(json.dumps({"exists": False, "gid": args.gid}, ensure_ascii=False))
        else:
            sys.stdout.write(text)
    elif args.cmd == "gather":
        print(json.dumps(gather(args.gid, args.limit), ensure_ascii=False, indent=2))
    elif args.cmd == "save":
        if args.file:
            with open(args.file, encoding="utf-8") as f:
                text = f.read()
        elif args.stdin:
            text = sys.stdin.read()
        else:
            print("[ERROR] provide --file or --stdin", file=sys.stderr)
            return 1
        p = save(args.gid, text)
        print(json.dumps({"saved": p}, ensure_ascii=False))
    elif args.cmd == "template":
        sys.stdout.write(TEMPLATE.format(gid=args.gid, name=args.name))
    return 0


if __name__ == "__main__":
    sys.exit(main())
