#!/usr/bin/env python3
"""Telegram Bot API client (stdlib urllib only).

Multi-bot: every function takes an optional `bot` name. `bot=None` (the default)
reads the bare keys TELEGRAM_BOT_TOKEN / TELEGRAM_ALLOWED_CHATS — i.e. existing
callers are unchanged. A named bot 'X' reads TELEGRAM_BOT_TOKEN_X /
TELEGRAM_ALLOWED_CHATS_X, falling back to the default bot when those are unset
(so splitting bots can be turned on incrementally without breaking anything).

Used by telegram_bridge.py (the daemon), telegram_approve.py (the hook), and the
watchers/notifiers (which route to the ops bot via notify_bot()/approval_bot()).

CLI (handy for testing the token/chat wiring); add a bot name as the last arg:
    python tg_api.py me [bot]                  # getMe -> bot identity
    python tg_api.py updates [bot]             # show chat_id of anyone who messaged the bot
    python tg_api.py send <chat_id> "text" [bot]   # send a message
    python tg_api.py test [bot]                # ping every allowed chat
"""

import json
import os
import ssl
import sys
import urllib.error
import urllib.request

import rc_config as cfg

API = "https://api.telegram.org"
MAX_LEN = 4000  # Telegram hard limit is 4096; leave headroom for HTML tags.

_SSL_WARNED = False


def _verify_ssl() -> bool:
    """TELEGRAM_VERIFY_SSL wins; falls back to the repo-wide SSL_VERIFY. Default true."""
    val = cfg.get("TELEGRAM_VERIFY_SSL") or cfg.get("SSL_VERIFY") or "true"
    return val.strip().lower() not in ("false", "0", "no", "off")


def _ssl_ctx():
    """Build an SSL context. Honors a corporate CA bundle, or disables verify
    (with a one-time warning) on networks that do TLS inspection."""
    global _SSL_WARNED
    ca = cfg.get("TELEGRAM_CA_BUNDLE") or cfg.get("CA_BUNDLE")
    if _verify_ssl():
        if ca and os.path.isfile(ca):
            return ssl.create_default_context(cafile=ca)
        return ssl.create_default_context()
    if not _SSL_WARNED:
        sys.stderr.write("⚠️  TELEGRAM_VERIFY_SSL=false — bỏ qua xác thực chứng chỉ TLS "
                         "tới api.telegram.org (chấp nhận do proxy SSL nội bộ).\n")
        _SSL_WARNED = True
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _bot_suffix(bot) -> str:
    """Map a logical bot name to its env-key suffix. Default bot (None/empty) ->
    bare keys (TELEGRAM_BOT_TOKEN); named bot 'ops' -> TELEGRAM_BOT_TOKEN_OPS."""
    return f"_{bot.upper()}" if bot else ""


def _token(bot=None) -> str:
    suf = _bot_suffix(bot)
    tok = cfg.get(f"TELEGRAM_BOT_TOKEN{suf}")
    if not tok and bot:               # named bot unconfigured -> fall back to default
        tok = cfg.get("TELEGRAM_BOT_TOKEN")
    if not tok:
        raise RuntimeError(f"TELEGRAM_BOT_TOKEN{suf} missing in .env")
    return tok


def allowed_chats(bot=None) -> list:
    suf = _bot_suffix(bot)
    chats = cfg.get_list(f"TELEGRAM_ALLOWED_CHATS{suf}")
    if not chats and bot:             # named bot unconfigured -> default allowlist
        chats = cfg.get_list("TELEGRAM_ALLOWED_CHATS")
    return chats


def is_allowed(chat_id, bot=None) -> bool:
    allow = allowed_chats(bot)
    if not allow:
        return False  # fail closed: no allowlist => nobody is authorized
    return str(chat_id) in allow


def notify_bot():
    """Logical bot for one-way notifications (watchers, bg_notify, digests).
    TELEGRAM_NOTIFY_BOT wins, else the shared TELEGRAM_OPS_BOT, else None — and
    None means the default bot, i.e. exactly the pre-split behaviour."""
    return cfg.get("TELEGRAM_NOTIFY_BOT") or cfg.get("TELEGRAM_OPS_BOT") or None


def approval_bot():
    """Logical bot that shows approve/deny buttons (telegram_approve hook + the
    watchers' approval cards). TELEGRAM_APPROVAL_BOT wins, else TELEGRAM_OPS_BOT,
    else None (default bot). Its poller must run: telegram_bridge.py --bot <name>
    --mode approvals-only (or the full bridge, if it IS the default bot)."""
    return cfg.get("TELEGRAM_APPROVAL_BOT") or cfg.get("TELEGRAM_OPS_BOT") or None


def call(method: str, params: dict, timeout: int = 35, bot=None) -> dict:
    """POST a Bot API method with a JSON body. Returns the parsed response."""
    url = f"{API}/bot{_token(bot)}/{method}"
    data = json.dumps(params).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx()) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            return json.loads(body)
        except ValueError:
            return {"ok": False, "error_code": e.code, "description": body}
    except Exception as e:  # noqa: BLE001 - surface as structured error
        return {"ok": False, "description": str(e)}


def _split_at(text: str) -> int:
    """Pick a cut point <= MAX_LEN that does NOT land inside an HTML tag.

    Telegram's HTML parser rejects a chunk whose `<...>` tag is cut in half
    ("Bad Request: can't parse entities"). We prefer the last newline in the
    final 25% of the window, then the last tag boundary ('>'), then a hard cut.
    """
    if len(text) <= MAX_LEN:
        return len(text)
    window = text[:MAX_LEN]
    nl = window.rfind("\n", MAX_LEN * 3 // 4)
    if nl != -1:
        return nl + 1
    gt = window.rfind(">")
    lt = window.rfind("<")
    if lt > gt:           # window ends inside an unclosed tag -> cut before it
        return lt
    return MAX_LEN


def _chunks(text: str):
    while text:
        cut = _split_at(text)
        yield text[:cut]
        text = text[cut:]


_BALANCE_TAGS = ("pre", "code", "b", "i", "u", "s")


def _balance(parts: list) -> list:
    """Keep each chunk's HTML valid on its own: close tags still open at a
    chunk's end, and reopen them at the next chunk's start. Telegram parses
    every message independently, so an unclosed <pre> would break the parse."""
    if not parts:
        return parts
    out, carry = [], ""
    for part in parts:
        body = carry + part
        reopen = ""
        for tag in _BALANCE_TAGS:
            if body.count(f"<{tag}>") > body.count(f"</{tag}>"):
                body += f"</{tag}>"
                reopen += f"<{tag}>"
        out.append(body)
        carry = reopen
    return out


def send_message(chat_id, text: str, reply_markup=None,
                 parse_mode: str = "HTML", disable_preview: bool = True,
                 bot=None) -> dict:
    """Send text (auto-chunked). Markup is only attached to the last chunk."""
    text = text if text.strip() else "(trống)"
    parts = list(_chunks(text)) or [""]
    if parse_mode == "HTML":
        parts = _balance(parts)
    last = {}
    for i, part in enumerate(parts):
        params = {
            "chat_id": chat_id,
            "text": part,
            "disable_web_page_preview": disable_preview,
        }
        if parse_mode:
            params["parse_mode"] = parse_mode
        if reply_markup is not None and i == len(parts) - 1:
            params["reply_markup"] = reply_markup
        last = call("sendMessage", params, bot=bot)
        # If HTML parse fails (unbalanced tags from agent output), retry as plain.
        if not last.get("ok") and parse_mode:
            params.pop("parse_mode", None)
            last = call("sendMessage", params, bot=bot)
    return last


def get_updates(offset: int, timeout: int = 30, bot=None) -> dict:
    return call("getUpdates", {
        "offset": offset,
        "timeout": timeout,
        "allowed_updates": ["message", "callback_query"],
    }, timeout=timeout + 10, bot=bot)


def answer_callback(callback_query_id: str, text: str = "", bot=None) -> dict:
    return call("answerCallbackQuery",
                {"callback_query_id": callback_query_id, "text": text}, bot=bot)


def edit_message_text(chat_id, message_id, text: str,
                      parse_mode: str = "HTML", bot=None) -> dict:
    return call("editMessageText", {
        "chat_id": chat_id, "message_id": message_id,
        "text": text[:MAX_LEN], "parse_mode": parse_mode,
    }, bot=bot)


def approve_keyboard(req_id: str) -> dict:
    return {"inline_keyboard": [[
        {"text": "✅ Duyệt", "callback_data": f"appr:{req_id}:yes"},
        {"text": "❌ Từ chối", "callback_data": f"appr:{req_id}:no"},
    ]]}


def choices_keyboard(token: str, options: list, per_row: int = 4) -> dict:
    """Compact numbered selector — the full option text is shown in the message
    body, so buttons are just the numbers (up to `per_row` per row). The choice
    is resolved by index server-side, keeping callback_data tiny (<64 bytes)."""
    btns = [{"text": str(i + 1), "callback_data": f"choice:{token}:{i}"}
            for i in range(len(options))]
    return {"inline_keyboard": [btns[i:i + per_row]
                                for i in range(0, len(btns), per_row)]}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    cmd = sys.argv[1].lower()
    if cmd == "me":
        bot = sys.argv[2] if len(sys.argv) >= 3 else None
        print(json.dumps(call("getMe", {}, bot=bot), indent=2, ensure_ascii=False))
    elif cmd == "send" and len(sys.argv) >= 4:
        bot = sys.argv[4] if len(sys.argv) >= 5 else None
        print(json.dumps(send_message(sys.argv[2], sys.argv[3], bot=bot),
                         indent=2, ensure_ascii=False))
    elif cmd == "updates":
        bot = sys.argv[2] if len(sys.argv) >= 3 else None
        resp = call("getUpdates", {"timeout": 0}, bot=bot)
        seen = {}
        for upd in resp.get("result", []):
            msg = upd.get("message") or upd.get("callback_query", {}).get("message", {})
            chat = msg.get("chat", {})
            if chat.get("id") is not None:
                seen[chat["id"]] = chat.get("username") or chat.get("title") \
                    or chat.get("first_name", "")
        if not seen:
            print("Chưa thấy update nào. Hãy NHẮN một tin cho bot trước, rồi chạy lại.")
        for cid, name in seen.items():
            print(f"chat_id = {cid}   ({name})")
    elif cmd == "test":
        bot = sys.argv[2] if len(sys.argv) >= 3 else None
        chats = allowed_chats(bot)
        if not chats:
            key = f"TELEGRAM_ALLOWED_CHATS{_bot_suffix(bot)}"
            print(f"{key} is empty — set it in .env first.")
            sys.exit(1)
        for c in chats:
            r = send_message(c, "✅ <b>remote-control</b> bridge: kết nối OK.", bot=bot)
            print(f"{c}: {'ok' if r.get('ok') else r.get('description')}")
    else:
        print(__doc__)
        sys.exit(1)
