"""Member commands of a workboard group (design §3.4, phase 1b) — anchored regexes, no LLM.

    review lại R-7        a new review round of the card's MR (policy: review)
    @bot tôi là ai        the sender's identity, links and permissions
    @bot ping             is the bot alive (heartbeat, backlog)
    @bot help             what can I type

A member's `@bot …` command must start with the prefix; `review lại R-n` needs none. The owner's messages
reach here with the prefix already stripped (prefixed=True). Replies are rendered here; the caller sends them.
"""

import re
import time

from agent_skill_toolkit.workflows.group_board import render

_BOT = re.compile(r"^@bot\b[\s:]*", re.I)
_CMDS = (
    ("review_again", re.compile(r"^review\s+lại\s+(R-\d+)\b", re.I), False),
    ("whoami", re.compile(r"^(?:tôi là ai|whoami)\s*\??\s*$", re.I), True),
    ("ping", re.compile(r"^ping\b", re.I), True),
    ("help", re.compile(r"^(?:help|giúp|hướng dẫn)\s*\??\s*$", re.I), True),
)

HELP_LINES = (
    "review giúp MR <link GitLab> — nhờ review; kết quả báo bằng thẻ R-n",
    "review lại R-<số> — review lượt mới cho MR của thẻ (sau khi đã sửa)",
    "@bot tôi là ai — định danh + quyền của bạn",
    "@bot ping — bot còn chạy không",
    "Sắp có: tạo task aTask, deploy dev/sandbox + duyệt, @bot env <repo>",
)


def parse(text: str, prefixed: bool = False):
    """(name, args) or None. prefixed: the '@bot' prefix was already removed (owner path)."""
    s = (text or "").strip()
    m = _BOT.match(s)
    has_bot = prefixed or bool(m)
    if m:
        s = s[m.end():]
    for name, rx, needs_bot in _CMDS:
        hit = rx.match(s)
        if hit and (has_bot or not needs_bot):
            return name, hit.groups()
    return None


def _who(ident: dict) -> str:
    return ident.get("name") or ident.get("fchat_id") or "?"


def whoami_text(ident: dict, owner_name: str) -> tuple:
    """(text, people). Only identity handles and perms — never profile notes from the registry."""
    name = render.one_line(_who(ident), 60)
    parts = [f"🤖 Định danh của @{name}", f"FChat id: {ident.get('fchat_id') or '?'}"]
    if ident.get("key"):
        parts.append(f"GitLab: {ident.get('gitlab') or '—'}")
        parts.append(f"aTask: {ident.get('atask') or '—'}")
        parts.append("✅ đã xác minh" if ident.get("verified") else "❔ chưa xác minh liên kết")
    else:
        parts.append(f"chưa liên kết GitLab/aTask — nhờ {owner_name} đăng ký")
    src = {"owner": "chủ nhóm", "team.json": "được cấp", "default": "mặc định thành viên",
           "ambiguous": f"id này gắn với nhiều hồ sơ — báo {owner_name} sửa danh bạ",
           "invalid": f"hồ sơ khai quyền sai — báo {owner_name} sửa danh bạ"}.get(ident["perms_source"], "?")
    text = " · ".join(parts) + f"\nQuyền: {', '.join(ident['perms']) or '—'} ({src})"
    if ident.get("owner"):
        text += "\nGhi chú: tài khoản này dùng chung với bot — lệnh cần '@bot' ở đầu; duyệt deploy qua Telegram"
    return render.defuse_body(text), [(name, ident.get("fchat_id") or "")]


def ping_text(heartbeat: dict, inbox: dict, pending_cards: list, now: float = None) -> str:
    now = now or time.time()
    age = heartbeat.get("age")
    hb = f"heartbeat {round(age)} giây trước" if age is not None else "chưa có heartbeat"
    waiting = ", ".join(c["id"] for c in pending_cards[:6]) or "không có"
    return (f"🤖 Bot đang chạy — {hb} · lệnh hôm nay: {inbox.get('commands', 0)}"
            f" · review đang chờ: {waiting}")


def help_text() -> str:
    return "🤖 Lệnh dùng được trong nhóm này:\n" + "\n".join("• " + ln for ln in HELP_LINES)


def denied_text(ident: dict, why: str, owner_name: str) -> tuple:
    name = render.one_line(_who(ident), 60)
    return (f"⛔ @{name}: {why}. Nhờ {owner_name} cấp quyền (bot kiểm theo tài khoản FChat của người gửi).",
            [(name, ident.get("fchat_id") or "")])
