#!/usr/bin/env python3
"""Telegram gate — gửi MỌI mốc duyệt của pipeline qua Telegram, trả lời TỰ DO.

Người dùng yêu cầu: các mốc duyệt (after_plan, before_mr, diff của fix_loop...) phải
đến qua Telegram dưới dạng CÁC MỤC ĐÁNH SỐ để họ viết comment phản hồi từng mục —
không phải nút bấm chọn. Mục 'ok'/'đồng ý'/bỏ qua = duyệt theo đề xuất; text tự do =
yêu cầu chỉnh (agent đọc bằng `parse` rồi thực hiện + ghi feedback ledger).

Items được lưu tại temp/runs/<RID>_gate_<gate>.json để `parse` khớp lại theo thứ tự.
Tái dùng hạ tầng của task_queue: _send_tg (chat/bot từ remote-control) + _parse_reply.

Usage:
    python tg_gate.py send --run <RID> --gate after_plan --title "..." \\
        --item "Plan: ... || đề xuất: duyệt" --item "Verify: ... || đề xuất: duyệt"
    #   phần sau '||' là đề xuất hiển thị nghiêng; có thể thay --item bằng --items-file (JSON list)
    python tg_gate.py parse --run <RID> --gate after_plan --text "<nguyên văn tin nhắn trả lời>"
    #   -> {"approved_all": bool, "items": [{"text","comment","approved"}...]}
"""

import argparse
import html
import json
import os
import re
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

_HERE = os.path.dirname(os.path.abspath(__file__))
_SKILLS = os.path.dirname(os.path.dirname(_HERE))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(_SKILLS, "dev-automation", "tools"))

import task_queue  # noqa: E402  (dùng _send_tg + _parse_reply + _runs_dir)
import run_log     # noqa: E402

GATE_ICON = {"after_plan": "🗺️", "before_mr": "🚀", "before_notify": "📣", "fix_diff": "🩹"}


def _gate_path(run_id, gate):
    safe = "".join(c for c in f"{run_id}_gate_{gate}" if c.isalnum() or c in "-_.")
    return os.path.join(run_log._runs_dir(), f"{safe}.json")


def _split_item(raw):
    """'nội dung || đề xuất: duyệt' -> (text, proposed)."""
    if "||" in raw:
        text, prop = raw.split("||", 1)
        return text.strip(), prop.strip()
    return raw.strip(), "duyệt"


def _message(run_id, gate, title, items):
    icon = GATE_ICON.get(gate, "🔎")
    lines = [f"{icon} <b>Duyệt {html.escape(gate)} — {html.escape(title)}</b>",
             f"<code>{html.escape(run_id)}</code>", ""]
    for i, (text, prop) in enumerate(items, 1):
        lines.append(f"{i}. {html.escape(text)}")
        lines.append(f"   → <i>{html.escape(prop)}</i>")
    lines += ["",
              "✍️ Trả lời bằng MỘT tin nhắn, ý kiến tự do từng mục:",
              f"<code>{html.escape(run_id)} 1: ok; 2: sửa lại chỗ X; 3: ok</code>",
              "(mục bỏ qua hoặc 'ok/đồng ý' = duyệt theo đề xuất)"]
    return "\n".join(lines)


def cmd_send(args):
    items = []
    if args.items_file:
        with open(args.items_file, encoding="utf-8") as f:
            items = [_split_item(x) if isinstance(x, str)
                     else (x.get("text", ""), x.get("proposed", "duyệt"))
                     for x in json.load(f)]
    items += [_split_item(x) for x in (args.item or [])]
    if not items:
        return {"error": True, "message": "không có mục nào (--item / --items-file)"}
    ok, detail = task_queue._send_tg(_message(args.run, args.gate, args.title or args.run, items))
    if not ok:
        return {"error": True, "message": f"gửi Telegram thất bại: {detail}"}
    with open(_gate_path(args.run, args.gate), "w", encoding="utf-8") as f:
        json.dump([{"text": t, "proposed": p} for t, p in items], f, ensure_ascii=False, indent=2)
    return {"ok": True, "gate": args.gate, "items_sent": len(items),
            "next": f"nhận trả lời -> tg_gate.py parse --run {args.run} --gate {args.gate} "
                    f"--text \"<tin nhắn>\""}


def _reply_path(run_id, gate):
    return _gate_path(run_id, gate).replace(".json", "_reply.txt")


def cmd_reply(args):
    """Ghi nhận tin nhắn trả lời (bridge/agent gọi khi nhận được) + parse luôn.

    Đây là nửa còn lại của `wait`: wait poll file này; reply do telegram_bridge
    (hoặc người dán tay vào phiên) ghi vào.
    """
    with open(_reply_path(args.run, args.gate), "w", encoding="utf-8") as f:
        f.write(args.text)
    return cmd_parse(args)


def cmd_wait(args):
    """CHỜ người trả lời gate rồi mới đi tiếp — poll reply-file (do `reply` ghi).

    `--poll-updates`: tự getUpdates thẳng từ Telegram — CHỈ dùng khi telegram_bridge
    KHÔNG chạy (2 consumer cùng getUpdates sẽ giành mất update của nhau).
    Hết `--timeout` chưa có trả lời -> {"status":"timeout"} (pipeline tạm dừng,
    gọi lại `wait` để chờ tiếp — tin nhắn đến trễ vẫn nằm ở reply-file).
    """
    deadline = time.time() + args.timeout
    rp = _reply_path(args.run, args.gate)
    offset = 0
    while time.time() < deadline:
        if os.path.isfile(rp):
            with open(rp, encoding="utf-8") as f:
                text = f.read().strip()
            if text:
                out = cmd_parse(argparse.Namespace(run=args.run, gate=args.gate, text=text))
                out["reply_text"] = text
                return out
        if args.poll_updates:
            try:
                sys.path.insert(0, os.path.join(_SKILLS, "remote-control", "tools"))
                import tg_api
                bot = tg_api.approval_bot()
                allowed = set(str(c) for c in tg_api.allowed_chats(bot))
                resp = tg_api.get_updates(offset, timeout=20, bot=bot)
                for u in (resp.get("result") or []):
                    offset = max(offset, u.get("update_id", 0) + 1)
                    msg = u.get("message") or {}
                    chat = str(((msg.get("chat") or {}).get("id")) or "")
                    text = (msg.get("text") or "").strip()
                    # nhận tin có run-id, hoặc tin dạng trả-lời-mục ("1: ...") từ chat hợp lệ
                    if chat in allowed and text and (
                            args.run in text or _REPLY_LIKE.match(text)):
                        with open(rp, "w", encoding="utf-8") as f:
                            f.write(text)
                        break
            except Exception:  # noqa: BLE001 — mạng chớp thì thử lại vòng sau
                pass
        time.sleep(args.interval)
    return {"status": "timeout", "run_id": args.run, "gate": args.gate,
            "waited_s": args.timeout,
            "next": "gọi lại wait để chờ tiếp, hoặc nhận tin rồi dùng `reply --text`"}


_REPLY_LIKE = re.compile(r"^\s*\d+\s*[:.)]")


def cmd_parse(args):
    path = _gate_path(args.run, args.gate)
    if not os.path.isfile(path):
        return {"error": True, "message": f"chưa gửi gate này ({path} không tồn tại)"}
    with open(path, encoding="utf-8") as f:
        items = json.load(f)
    questions = [{"ask": x["text"], "proposed": x.get("proposed", "duyệt")} for x in items]
    answers, matched = task_queue._parse_reply(args.text, questions)
    out_items = []
    for i, (x, a) in enumerate(zip(items, answers), 1):
        approved = not a["answer"]
        out_items.append({"n": i, "text": x["text"], "approved": approved,
                          "comment": a["answer"] or None})
    return {"ok": True, "gate": args.gate, "run_id": args.run,
            "approved_all": all(it["approved"] for it in out_items),
            "items": out_items,
            "next": ("mọi mục OK -> run_log.py checkpoint <RID> <gate> approved; "
                     "mục có comment -> THỰC HIỆN chỉnh + ghi feedback.py add --action edited")}


def main():
    ap = argparse.ArgumentParser(description="Gửi/nhận mốc duyệt pipeline qua Telegram (trả lời tự do).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("send")
    s.add_argument("--run", required=True)
    s.add_argument("--gate", required=True, help="after_plan | before_mr | before_notify | fix_diff | ...")
    s.add_argument("--title", default=None)
    s.add_argument("--item", action="append", help="'nội dung || đề xuất: ...' (lặp được)")
    s.add_argument("--items-file", default=None, help="JSON list (str hoặc {text,proposed})")
    p = sub.add_parser("parse")
    p.add_argument("--run", required=True)
    p.add_argument("--gate", required=True)
    p.add_argument("--text", required=True)

    r = sub.add_parser("reply", help="ghi nhận tin trả lời (bridge/người dán) + parse")
    r.add_argument("--run", required=True)
    r.add_argument("--gate", required=True)
    r.add_argument("--text", required=True)

    w = sub.add_parser("wait", help="CHỜ trả lời gate rồi mới đi tiếp (poll reply-file)")
    w.add_argument("--run", required=True)
    w.add_argument("--gate", required=True)
    w.add_argument("--timeout", type=int, default=1800, help="giây (mặc định 30 phút)")
    w.add_argument("--interval", type=int, default=5)
    w.add_argument("--poll-updates", action="store_true",
                   help="tự getUpdates từ Telegram — CHỈ khi bridge KHÔNG chạy")

    args = ap.parse_args()
    dispatch = {"send": cmd_send, "parse": cmd_parse, "reply": cmd_reply, "wait": cmd_wait}
    out = dispatch[args.cmd](args)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 1 if out.get("error") else 0


if __name__ == "__main__":
    sys.exit(main())
