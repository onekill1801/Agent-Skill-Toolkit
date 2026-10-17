#!/usr/bin/env python3
"""Telegram <-> Claude Code bridge daemon.

Long-polls Telegram. Each text message from an allowed chat is handed to a
headless `claude -p` run in the repo root, so the FULL agent (every skill) is
reachable from your phone. The agent's risky actions are gated by the Telegram
approval hook (telegram_approve.py) — the daemon keeps polling while an agent
runs, so it can deliver the inline button press back to the waiting hook.

Run it on the always-on machine (your Windows box):
    python telegram_bridge.py            # start the daemon
    python telegram_bridge.py --test     # ping allowed chats and exit

Stop with Ctrl+C.

Commands inside Telegram:
    /help          show help
    /hosts         list SSH hosts from work/hosts.json
    /reset         drop the conversation, start fresh next message
    /whoami        show your chat id (to confirm allowlisting)
"""

import json
import os
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import approvals  # noqa: E402
import choices  # noqa: E402
import md2tg  # noqa: E402
import rc_config as cfg  # noqa: E402
import ssh_exec  # noqa: E402
import tg_api  # noqa: E402

_DEV_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__),
                                        "..", "..", "dev-automation", "tools"))
sys.path.insert(0, _DEV_DIR)
try:
    import daemon_common  # noqa: E402  (backoff + shared health log)
except Exception:  # noqa: BLE001
    daemon_common = None


def _hlog(event, detail=""):
    if daemon_common is not None:
        daemon_common.health_log("telegram_bridge", event, detail)

HOOK_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "telegram_approve.py")

# ── which bot this daemon drives ──────────────────────────────────
# A daemon process drives exactly ONE bot (Telegram allows one getUpdates poller
# per token). _BOT / _MODE are set once in serve() before any thread starts, so
# the threads only ever READ them — no locking needed. Every tg_api call goes
# through these thin wrappers so the bot binding is applied in one place.
#   _MODE "full"           : receive messages -> run claude -p agents (+ choices)
#   _MODE "approvals-only" : light poller — only handle appr: button callbacks
#                            (for the ops bot: notify + approve, no agent)
_BOT = None
_MODE = "full"


def _send(chat, text, **kw):
    return tg_api.send_message(chat, text, bot=_BOT, **kw)


def _answer(cb_id, text=""):
    return tg_api.answer_callback(cb_id, text, bot=_BOT)


def _edit(chat, message_id, text):
    return tg_api.edit_message_text(chat, message_id, text, bot=_BOT)


def _allowed_chats():
    return tg_api.allowed_chats(_BOT)


def _is_allowed(chat):
    return tg_api.is_allowed(chat, _BOT)


def _get_updates(offset, timeout=30):
    return tg_api.get_updates(offset, timeout, bot=_BOT)

# Taught to every bridge-spawned agent so plan/decision prompts arrive as
# tappable Telegram buttons instead of unselectable prose. The bridge detects
# this block in the reply (choices.parse) and renders an inline keyboard.
CHOICE_SYS = (
    "Bạn đang trả lời qua Telegram (không có giao diện chọn tương tác). "
    "KHI VÀ CHỈ KHI bạn cần người dùng chọn giữa các phương án (ví dụ chọn hướng "
    "plan, xác nhận một lựa chọn), ĐỪNG hỏi bằng văn xuôi. Thay vào đó kết thúc "
    "câu trả lời bằng đúng một khối theo định dạng:\n"
    "[[TG_CHOICE]]\n"
    "question: <câu hỏi ngắn gọn>\n"
    "1. <phương án 1>\n"
    "2. <phương án 2>\n"
    "[[/TG_CHOICE]]\n"
    "Mỗi phương án một dòng, tối đa 8 phương án, mỗi phương án dưới 60 ký tự để "
    "hiển thị làm nút bấm. Không dùng khối này nếu không thực sự cần người dùng quyết định."
)

# Bridge-imposed constraint: every turn is a ONE-SHOT headless `claude -p` — the
# process exits the instant the turn ends. So a long task run inline blocks until
# the agent timeout (and gets killed), and a self-spawned background poll is
# orphaned the moment the turn ends → the user never hears back. The fix is
# bg_notify.py, which detaches from claude -p and pushes the result to Telegram
# itself. This guidance lives here (not in a skill) so EVERY bridge agent gets it
# regardless of which skill a build/deploy request happens to trigger.
LONGTASK_SYS = (
    "MÔI TRƯỜNG: bạn chạy headless one-shot — khi bạn KẾT THÚC LƯỢT, tiến trình "
    "thoát NGAY. Do đó với BẤT KỲ lệnh chạy lâu (build/compile/test/deploy, "
    "jenkins build --wait, mvn/gradle/npm build, hay gì >~1 phút): TUYỆT ĐỐI "
    "KHÔNG chạy chặn trong lượt (sẽ bị giết vì timeout), và KHÔNG tự spawn "
    "thread/tiến trình nền để 'hẹn báo sau' (sẽ mồ côi, không ai gửi tin). "
    "Thay vào đó BẮT BUỘC bọc lệnh bằng bg_notify.py — nó tách rời khỏi claude -p "
    "và tự gửi kết quả (✅/❌ + thời lượng) về Telegram khi xong. Mẫu "
    "(chạy từ gốc repo, dùng đường dẫn đầy đủ để khỏi cd):\n"
    "  python .claude/skills/dev-automation/tools/bg_notify.py --label \"Build atask dev\" "
    "-- python .claude/skills/dev-automation/tools/jenkins.py build --project atask --env dev --wait\n"
    "Sau khi nó in {\"detached\": true,…}, hãy trả lời ngắn gọn (vd 'Đã chạy nền, "
    "sẽ nhắn khi xong') rồi KẾT THÚC LƯỢT — đừng chờ, đừng poll."
)

# One appended system prompt carries every bridge-specific behaviour.
BRIDGE_SYS = CHOICE_SYS + "\n\n" + LONGTASK_SYS
SESSIONS_FILE = os.path.join(cfg.temp_dir(), "..", "tg_sessions.json")
_busy = set()           # chat ids with a running agent
_busy_lock = threading.Lock()


# ── session continuity ────────────────────────────────────────────
# Each chat maps to {"sid", "turns", "started"}. Old files stored a bare sid
# string; _session_rec() reads both so upgrades are seamless.
def _load_sessions() -> dict:
    try:
        with open(SESSIONS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _session_rec(chat) -> dict:
    rec = _load_sessions().get(str(chat))
    if isinstance(rec, str):          # legacy: bare sid
        return {"sid": rec, "turns": 0, "started": 0}
    return rec or {}


def _session_sid(chat):
    return _session_rec(chat).get("sid")


def _save_session(chat, sid, account=None):
    """Persist the latest sid (and which account it belongs to) for a chat.
    Resuming returns a fresh sid each turn but it's the SAME conversation, so we
    keep `started`/bump `turns` while the account is unchanged; switching account
    resets the counters. Pass sid=None to forget the conversation."""
    data = _load_sessions()
    key = str(chat)
    if not sid:
        data.pop(key, None)
    else:
        prev = data.get(key)
        prev = prev if isinstance(prev, dict) else {}
        same = prev.get("account") == account
        data[key] = {
            "sid": sid,
            "account": account,
            "turns": (prev.get("turns", 0) + 1) if same else 1,
            "started": (prev.get("started") if same else int(time.time())) or int(time.time()),
        }
    with open(SESSIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f)


# turns past which the context is "long" and a /new is worth suggesting
LONG_SESSION_TURNS = int(cfg.get("TELEGRAM_LONG_SESSION_TURNS", "20") or "20")


def _session_status(chat) -> str:
    rec = _session_rec(chat)
    if not rec.get("sid"):
        return "💤 Chưa có phiên nào. Tin nhắn tới sẽ bắt đầu một phiên mới."
    turns = rec.get("turns", 0)
    started = rec.get("started", 0)
    age = ""
    if started:
        mins = max(0, (int(time.time()) - started) // 60)
        age = f", bắt đầu {mins} phút trước" if mins else ", vừa bắt đầu"
    msg = (f"🧵 <b>Phiên đang hoạt động</b>\n"
           f"• Tài khoản: <b>{rec.get('account') or '?'}</b>\n"
           f"• Số lượt: <b>{turns}</b>{age}\n"
           f"• ID: <code>{str(rec['sid'])[:8]}…</code>")
    if turns >= LONG_SESSION_TURNS:
        msg += ("\n\n⚠️ Phiên đã dài — ngữ cảnh lớn dễ chậm/loãng. "
                "Gõ <b>/new</b> để bắt đầu sạch.")
    return msg


# ── bridge settings file (attaches the approval hook to spawned agents) ──
def _write_bridge_settings() -> str:
    path = os.path.join(cfg.temp_dir(), "..", "bridge_settings.json")
    cmd = f'"{sys.executable}" "{HOOK_PATH}"'
    settings = {"hooks": {"PreToolUse": [
        {"matcher": "*", "hooks": [{"type": "command", "command": cmd}]}
    ]}}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)
    return path


def _claude_bin() -> str:
    return cfg.get("CLAUDE_BIN") or "claude"


# ── Claude accounts (cross-platform multi-account with fallback) ──
# An "account" = a home dir holding its own .claude (creds/config). We switch
# between them by overriding the home env var per OS (HOME on macOS/Linux,
# USERPROFILE on Windows) — the same thing the claude-work/personal wrappers do.
def _base_home() -> str:
    """The real profile root on any OS, even if the bridge itself runs under a
    per-account home (…/.claude-work) — strips EVERY trailing .claude-* suffix so
    we never nest …/.claude-work/.claude-work, even when launched from a home that
    is itself doubly nested (…/.claude-work/.claude-personal)."""
    h = os.path.normpath(os.path.expanduser("~"))
    while os.path.basename(h).startswith(".claude-"):
        parent = os.path.dirname(h)
        if not parent or parent == h:
            break
        h = parent
    return h


def _account_home(label: str) -> str:
    """Resolve a label to a home dir: explicit CLAUDE_HOME_<LABEL> wins, else
    <base>/.claude-<label> (works the same on Windows/macOS/Linux)."""
    override = cfg.get(f"CLAUDE_HOME_{label.upper()}")
    return override or os.path.join(_base_home(), f".claude-{label}")


def _accounts() -> list:
    """Ordered [(label, home)] — primary first, fallbacks after. Driven by
    CLAUDE_ACCOUNTS (default 'work,personal'). A label is kept only if its home
    actually has a .claude dir (or is explicitly overridden). If none qualify —
    e.g. a plain machine with just ~/.claude — fall back to a single 'default'
    account with home=None (leave the env untouched)."""
    labels = cfg.get_list("CLAUDE_ACCOUNTS") or ["work", "personal"]
    out = []
    for label in labels:
        override = cfg.get(f"CLAUDE_HOME_{label.upper()}")
        home = _account_home(label)
        if override or os.path.isdir(os.path.join(home, ".claude")):
            out.append((label, home))
    return out or [("default", None)]


def _order_by_current(accounts: list, current: str) -> list:
    """Keep continuity: if the chat already has a session on `current`, try that
    account first (its context lives there), then the rest as fallback."""
    if not current:
        return accounts
    cur = [a for a in accounts if a[0] == current]
    return cur + [a for a in accounts if a[0] != current] if cur else accounts


# ── agent execution ───────────────────────────────────────────────
def _is_stale_session(text: str) -> bool:
    """True if `claude --resume` failed because the session no longer exists."""
    t = (text or "").lower()
    return "no conversation found" in t or "session id" in t and "not found" in t


def _is_account_unavailable(text: str) -> bool:
    """True if the error looks like the account (not the request) is the problem:
    usage/rate limit, auth/login, no credit, OR a server-side error (5xx /
    overloaded) — i.e. a sibling account is worth a try."""
    t = (text or "").lower()
    needles = (
        "usage limit", "rate limit", "rate_limit", "too many requests",
        "credit balance", "insufficient", "quota",
        "please run /login", "/login", "authentication", "invalid api key",
        "unauthorized", "401", "403", "account",
        # server-side errors: switching account is a cheap retry on another route
        "500", "502", "503", "504", "529",
        "internal server error", "server error", "overloaded",
        "api error", "service unavailable", "bad gateway", "gateway timeout",
    )
    return any(n in t for n in needles)


def _invoke_claude(chat, text, settings_path, sid, home=None):
    """Run one `claude -p` call under the given account home (USERPROFILE).
    Returns (result_text, new_sid, is_error) or None if the binary is missing /
    it timed out (already reported to the user)."""
    argv = [_claude_bin(), "-p", text,
            "--output-format", "json",
            "--append-system-prompt", BRIDGE_SYS,
            "--settings", settings_path]
    if sid:
        argv += ["--resume", sid]
    if cfg.get("TELEGRAM_AGENT_MODEL"):
        argv += ["--model", cfg.get("TELEGRAM_AGENT_MODEL")]

    env = dict(os.environ)
    env["CLAUDE_TG_BRIDGE"] = "1"
    env["CLAUDE_TG_CHAT_ID"] = str(chat)
    if home:  # select the account: claude reads creds/config from <home>/.claude
        env["HOME"] = home          # macOS / Linux
        env["USERPROFILE"] = home   # Windows

    try:
        proc = subprocess.run(
            argv, cwd=cfg.repo_root(), env=env, capture_output=True,
            text=True, encoding="utf-8", errors="replace",
            timeout=int(cfg.get("TELEGRAM_AGENT_TIMEOUT", "1800") or "1800"))
    except FileNotFoundError:
        _send(chat, "❌ Không tìm thấy <code>claude</code> trên PATH "
                            "(đặt CLAUDE_BIN trong .env nếu cần).")
        return None
    except subprocess.TimeoutExpired:
        _send(chat, "⏱️ Agent chạy quá lâu, đã hủy.")
        return None

    out = proc.stdout.strip()
    result_text, new_sid, is_error = out, None, False
    try:
        obj = json.loads(out)
        result_text = obj.get("result") or obj.get("error") or out
        new_sid = obj.get("session_id")
        is_error = bool(obj.get("is_error"))
    except ValueError:
        if proc.stderr.strip():
            result_text = (out + "\n" + proc.stderr).strip() or proc.stderr.strip()
        if proc.returncode != 0:
            is_error = True
    return result_text, new_sid, is_error


def _run_agent(chat, text, settings_path):
    """Run one turn: try the chat's current account first (default = primary),
    falling back to the next account if this one is unavailable (limit/auth).
    A stale session on an account is dropped and retried fresh on that account."""
    _send(chat, "⏳ <i>Agent đang xử lý…</i>")
    try:
        rec = _session_rec(chat)
        cur_account, cur_sid = rec.get("account"), rec.get("sid")
        accounts = _order_by_current(_accounts(), cur_account)

        for idx, (label, home) in enumerate(accounts):
            sid = cur_sid if label == cur_account else None
            res = _invoke_claude(chat, text, settings_path, sid, home)
            if res is None:
                return
            result_text, new_sid, is_error = res

            # session vanished on this account -> retry fresh on the SAME account
            if is_error and sid and _is_stale_session(result_text):
                _send(chat, "♻️ Phiên cũ không còn, bắt đầu phiên mới…")
                res = _invoke_claude(chat, text, settings_path, None, home)
                if res is None:
                    return
                result_text, new_sid, is_error = res

            # account itself unavailable -> try the next one (fresh, no sid carry)
            if is_error and _is_account_unavailable(result_text) \
                    and idx < len(accounts) - 1:
                nxt = accounts[idx + 1][0]
                _send(
                    chat, f"⚠️ Tài khoản <b>{label}</b> không khả dụng "
                    f"(giới hạn/đăng nhập). Chuyển sang <b>{nxt}</b>…")
                cur_account, cur_sid = None, None
                continue

            # success, or a terminal error on the last available account
            if is_error:
                result_text = "⚠️ " + str(result_text)
            if new_sid:
                _save_session(chat, new_sid, label)
            _deliver(chat, result_text or "(agent không trả về nội dung)")
            if new_sid and _session_rec(chat).get("turns") == LONG_SESSION_TURNS:
                _send(chat, "ℹ️ Phiên đã khá dài (đủ "
                                    f"{LONG_SESSION_TURNS} lượt). Gõ <b>/new</b> nếu "
                                    "muốn bắt đầu ngữ cảnh sạch.")
            return
    finally:
        with _busy_lock:
            _busy.discard(str(chat))


def _deliver(chat, result_text):
    """Send the agent's reply. If it ends with a TG_CHOICE block, render the
    options as tappable inline buttons instead of plain (unselectable) text.

    The plan/reasoning (preamble) and the choice card are sent as SEPARATE
    messages: a long plan auto-chunks on its own, and the choice card lists
    every option as full numbered text so nothing is hidden behind a truncated
    button label."""
    parsed = choices.parse(result_text)
    if not parsed:
        _send(chat, md2tg.to_html(result_text))
        return
    preamble, question, options = parsed
    token = choices.create(chat, question, options)
    if preamble:
        _send(chat, md2tg.to_html(preamble))  # full plan, chunked if long
    lines = [f"❓ <b>{md2tg._inline(question)}</b>", ""]
    lines += [f"<b>{i + 1}.</b> {md2tg._inline(opt)}" for i, opt in enumerate(options)]
    lines += ["", "<i>Bấm nút bên dưới để chọn.</i>"]
    _send(chat, "\n".join(lines),
                        reply_markup=tg_api.choices_keyboard(token, options))


# ── command + update handling ─────────────────────────────────────
def _handle_command(chat, text) -> bool:
    cmd = text.strip().split()[0].lower()
    if cmd in ("/start", "/help"):
        _send(chat,
            "🤖 <b>Remote-control bridge</b>\n"
            "Nhắn yêu cầu thường (vd <i>“review MR 123”</i>, <i>“task của tôi”</i>, "
            "<i>“ssh may-build chạy df -h”</i>) → agent xử lý.\n"
            "Thao tác ghi/SSH/nguy hiểm sẽ hỏi duyệt bằng nút bấm.\n\n"
            "<b>Lệnh:</b>\n"
            "• /new — phiên mới (xóa ngữ cảnh, bắt đầu sạch)\n"
            "• /session — xem phiên hiện tại (số lượt, chạy bao lâu)\n"
            "• /hosts — danh sách máy SSH\n"
            "• /whoami — chat_id của bạn")
        return True
    if cmd == "/whoami":
        _send(chat, f"chat_id của bạn: <code>{chat}</code>")
        return True
    if cmd in ("/new", "/reset", "/clear"):
        _save_session(chat, None)
        _send(chat, "🆕 <b>Đã tạo phiên mới.</b> Ngữ cảnh cũ đã xóa — "
                            "tin nhắn tới sẽ bắt đầu sạch từ đầu.")
        return True
    if cmd == "/session":
        _send(chat, _session_status(chat))
        return True
    if cmd == "/hosts":
        hosts = ssh_exec.load_hosts()
        if not hosts:
            _send(chat, "Chưa có host nào trong <code>work/hosts.json</code>.")
        else:
            lines = [f"• <code>{a}</code> → {h.get('user','')}@{h.get('host')}"
                     for a, h in hosts.items()]
            _send(chat, "<b>SSH hosts:</b>\n" + "\n".join(lines))
        return True
    return False


def _handle_callback(cbq, settings_path):
    data = cbq.get("data", "")
    cb_id = cbq.get("id")
    if data.startswith("appr:"):
        _handle_approval_cb(cbq, data, cb_id)          # works in both modes
    elif data.startswith("choice:") and _MODE == "full":
        _handle_choice_cb(cbq, data, cb_id, settings_path)
    else:
        _answer(cb_id)


def _handle_approval_cb(cbq, data, cb_id):
    _, req_id, verdict = data.split(":", 2)
    approved = verdict == "yes"
    rec = approvals.decide(req_id, approved, by=str(cbq.get("from", {}).get("id", "")))
    msg = cbq.get("message", {})
    label = "✅ ĐÃ DUYỆT" if approved else "❌ ĐÃ TỪ CHỐI"
    if rec:
        _edit(msg.get("chat", {}).get("id"), msg.get("message_id"),
                                 f"{label}\n<code>{rec.get('summary','')}</code>")
    _answer(cb_id, label)


def _handle_choice_cb(cbq, data, cb_id, settings_path):
    import html
    _, token, idx = data.split(":", 2)
    chosen = choices.resolve(token, int(idx))
    msg = cbq.get("message", {})
    if not chosen:
        _answer(cb_id, "Lựa chọn đã hết hạn.")
        return
    rec = choices.get(token)
    chat = rec.get("chat") or msg.get("chat", {}).get("id")
    _edit(
        msg.get("chat", {}).get("id"), msg.get("message_id"),
        f"❓ <b>{html.escape(rec.get('question', ''))}</b>\n"
        f"➡️ <b>Đã chọn:</b> <code>{html.escape(chosen)}</code>")
    _answer(cb_id, f"Đã chọn: {chosen[:60]}")
    # Resume the agent with the picked option as the next turn.
    _start_turn(chat, f"Tôi chọn phương án: {chosen}", settings_path)


def _handle_message(msg, settings_path):
    chat = msg.get("chat", {}).get("id")
    text = msg.get("text", "")
    if not text:
        return
    if not _is_allowed(chat):
        _send(chat, "⛔ Chat chưa được cấp quyền. "
                            f"chat_id: <code>{chat}</code> — thêm vào TELEGRAM_ALLOWED_CHATS.")
        return
    if _MODE == "approvals-only":
        # Ops bot = notify + approve only, never runs an agent. Still answer the
        # identity commands so the user can grab the chat_id for the allowlist.
        if text.strip().split()[0].lower() in ("/whoami", "/start", "/help"):
            _send(chat, "🛠️ <b>Kênh OPS</b> — theo dõi &amp; duyệt.\n"
                  f"chat_id: <code>{chat}</code>\n"
                  "<i>Kênh này chỉ nhận thông báo và nút Duyệt. Gửi yêu cầu code "
                  "ở bot CODE.</i>")
        return
    if text.startswith("/") and _handle_command(chat, text):
        return
    _start_turn(chat, text, settings_path)


def _start_turn(chat, text, settings_path) -> bool:
    """Spawn one agent turn for `chat` unless one is already running. Returns
    False (and notifies) if the chat is busy."""
    with _busy_lock:
        if str(chat) in _busy:
            _send(chat, "⏳ Đang chạy một yêu cầu, đợi xong rồi nhắn tiếp nhé.")
            return False
        _busy.add(str(chat))
    threading.Thread(target=_run_agent, args=(chat, text, settings_path),
                     daemon=True).start()
    return True


def serve(bot=None, mode="full"):
    global _BOT, _MODE
    _BOT, _MODE = bot, mode
    # Presence check on THIS bot's keys; tg_api falls back to the default bot's
    # token/allowlist when a named bot is unconfigured, so an unset ops bot still
    # boots (it just shares the default bot — harmless until you give it a token).
    try:
        tg_api._token(bot)
    except RuntimeError as e:
        print(f"Thiếu config: {e}")
        sys.exit(1)
    if not _allowed_chats():
        print(f"Thiếu config: TELEGRAM_ALLOWED_CHATS{tg_api._bot_suffix(bot)} (đặt trong .env)")
        sys.exit(1)
    # approvals-only never spawns an agent -> the hook settings file is moot.
    settings_path = _write_bridge_settings() if mode == "full" else None
    print(f"Bridge khởi động [bot={bot or 'default'}, mode={mode}]. Hub repo: {cfg.repo_root()}")
    print(f"Allowed chats: {_allowed_chats()}")
    print("Ctrl+C để dừng.")
    offset = 0
    _hlog("started", f"bot={bot or 'default'} mode={mode}")
    backoff = daemon_common.Backoff() if daemon_common else None
    fails = 0

    def _delay():
        return backoff.next() if backoff else 3.0

    while True:
        try:
            resp = _get_updates(offset, timeout=30)
            if not resp.get("ok"):
                fails += 1
                code = resp.get("error_code")
                # 401/403 = bad/expired bot token → needs a fixed .env + restart; log loudly.
                kind = "fatal" if code in (401, 403) else "transient"
                delay = _delay()
                _hlog(kind, f"getUpdates not ok (code {code}): {resp.get('description')}")
                print(f"[bridge] getUpdates lỗi (code {code}): {resp.get('description')}; "
                      f"thử lại sau {delay:.0f}s", file=sys.stderr)
                time.sleep(delay)
                continue
            if fails:
                _hlog("recovered", f"after {fails} failure(s)")
                fails = 0
                if backoff:
                    backoff.reset()
            for upd in resp.get("result", []):
                offset = upd["update_id"] + 1
                if "callback_query" in upd:
                    _handle_callback(upd["callback_query"], settings_path)
                elif "message" in upd:
                    _handle_message(upd["message"], settings_path)
        except KeyboardInterrupt:
            _hlog("stopped", "KeyboardInterrupt")
            print("\nDừng bridge.")
            break
        except Exception as e:  # noqa: BLE001 - keep the daemon alive
            fails += 1
            delay = _delay()
            _hlog("transient", f"loop error: {e}; retry in {delay:.0f}s")
            print(f"[bridge] lỗi vòng lặp: {e}; thử lại sau {delay:.0f}s")
            time.sleep(delay)


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(prog="telegram_bridge.py")
    p.add_argument("--bot", default=None,
                   help="tên bot (rỗng = bot mặc định TELEGRAM_BOT_TOKEN; vd 'ops')")
    p.add_argument("--mode", choices=["full", "approvals-only"], default="full",
                   help="full = nhận tin -> chạy agent (bot CODE); "
                        "approvals-only = poller nhẹ chỉ bắt nút Duyệt (bot OPS)")
    p.add_argument("--test", action="store_true", help="ping các allowed chat rồi thoát")
    a = p.parse_args()
    if a.test:
        _BOT = a.bot
        for c in _allowed_chats():
            r = _send(c, "✅ Bridge test OK.")
            print(f"{c}: {'ok' if r.get('ok') else r.get('description')}")
        sys.exit(0)
    serve(bot=a.bot, mode=a.mode)
