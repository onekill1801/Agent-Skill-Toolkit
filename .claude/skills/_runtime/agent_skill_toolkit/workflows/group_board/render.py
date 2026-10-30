"""Render cards as chat text the extension parses (PROTOCOL.md) and people read without it.

    🤖 [R-7] idaas/etask!1388 · Fix filter by creator
    Trạng thái: cần sửa · Yêu cầu: @Lan
    Kết quả: 1 blocker · 2 major
    MR: https://gitlab…/-/merge_requests/1388

Rules (design §3.3 / §4.3): only the FIRST line starts with `🤖 [`; no line starts with `@bot` or a member command
verb (the watcher must never read a bot message as a command); people are written `@<displayName>` in place —
the caller turns them into inline mentions (send.with_mentions).
"""

import re

# state -> word on the "Trạng thái" line (the extension's STATE_WORDS map them back)
R_STATE_WORD = {"pending": "chờ bot", "comment": "có nhận xét", "changes": "cần sửa", "approve": "sẵn sàng merge",
                "merged": "đã merge", "error": "lỗi"}
VERDICT_STATE = {"APPROVE": "approve", "COMMENT": "comment", "REQUEST_CHANGES": "changes"}

# a line a member (or the watcher's keyword prefilter) could read as a request: bot prefix, card header, card
# verbs, and the free-form requests the watcher acts on (review / build / deploy / tạo task / MR …)
_CMD_LINE = re.compile(r"^\s*(@bot\b|🤖\s*\[|(duyệt|từ chối|xác nhận|huỷ|hủy|xong|deploy lại|review lại)\s+[RTDE]-"
                       r"|(review|build|deploy|merge|tạo task|mr\b(?!:)|lên dev|!review|!build|/review|/build))", re.I)


def one_line(text, limit: int = 0) -> str:
    s = " ".join(str(text or "").split())
    return s[:limit - 1] + "…" if limit and len(s) > limit else s


def defuse(text: str) -> str:
    """Free text the bot repeats (agent output, MR titles, errors): a line that looks like a command or a card
    header gets a '› ' prefix, so neither the watcher nor the extension takes it for one."""
    return "\n".join("› " + ln if _CMD_LINE.match(ln) else ln for ln in str(text or "").split("\n"))


def _value(text, limit=200) -> str:
    # field values sit after a label, but a value may itself contain ' · ' (the field separator): replace it
    return one_line(text, limit).replace(" · ", " – ")


def render_review(card: dict, requester: dict = None, note: str = "") -> tuple:
    """(text, people) for an R card. requester = {id, name} written on 'Yêu cầu' (mention target)."""
    p = card.get("payload") or {}
    title = f"{p.get('project_path') or '?'}!{p.get('iid') or '?'}"
    if p.get("mr_title"):
        title += " · " + _value(p["mr_title"], 80)
    lines = [f"🤖 [{card['id']}] {title}"]
    head = [f"Trạng thái: {R_STATE_WORD.get(card.get('state'), 'chờ bot')}"]
    people = []
    if requester and requester.get("name"):
        head.append(f"Yêu cầu: @{_value(requester['name'], 60)}")
        people.append((_value(requester["name"], 60), requester.get("id") or ""))
    lines.append(" · ".join(head))
    if p.get("summary"):
        lines.append(f"Kết quả: {_value(p['summary'])}")
    if note:
        lines.append(f"Ghi chú: {_value(note, 300)}")
    if p.get("url"):
        lines.append(f"MR: {p['url']}")
    return defuse_body("\n".join(lines)), people


def defuse_body(text: str) -> str:
    """Card text: keep the header line, defuse the rest (golden rule: one card header per message)."""
    first, _, rest = text.partition("\n")
    return first + ("\n" + defuse(rest) if rest else "")
