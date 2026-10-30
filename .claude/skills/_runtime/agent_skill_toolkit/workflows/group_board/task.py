"""Task cards T-n (design §6, phase 3): a member drafts an aTask task in the group, confirms it, the bot creates it.

    @bot tạo task: <tiêu đề> | giao: @X | hạn: dd/mm/yyyy | mô tả: … | list: <mã>
        -> T-n "chờ xác nhận" (nothing created yet; the draft expires after DRAFT_TTL)
    xác nhận T-n        requester only -> create on aTask (idempotency key in the description) -> "đã tạo" + link
    huỷ T-n             requester only, before anything exists on aTask -> "huỷ" (internal: no re-ask)
    xong T-n            requester or assignee -> bot asks again (closing = final state on an external system)
    xác nhận xong T-n   the same person within DONE_TTL -> complete_task -> "xong"

Lists: `work/task_lists.json` {"default": "<mã>", "lists": {"<mã>": {name, project, project_id, list_id}}} —
only those lists may be named with `list:` (user decision 30/10: default #6 Backend Project Task / Task BE).
The task is created by the owner's aTask PAT (spike S7: create_task has no creator) — the card says so.

Every function takes the board store and an aTask client (integrations.atask or a fake) and returns
{"card": card, "send": (text, people)} for a card message, or {"reply": (text, people)} for a short answer.
"""

import datetime as dt
import json
import os
import re
import time
import uuid

from agent_skill_toolkit.core import paths
from agent_skill_toolkit.workflows.group_board import render

DRAFT_TTL = 24 * 3600
DONE_TTL = 600
STUCK_S = 300            # a "creating"/"completing" claim older than this = the process died mid-call
TITLE_MAX = 200
CODE_RETRIES = (2, 2)     # 3 lookups in all: create_task returns no taskCode and aTask's search index lags (real run 30/10)

_LABELS = {"giao": "assignee", "giao cho": "assignee", "hạn": "due", "han": "due", "mô tả": "desc",
           "mo ta": "desc", "mota": "desc", "list": "list"}
_SELF = {"tôi", "toi", "mình", "minh", "me"}
VN = dt.timezone(dt.timedelta(hours=7))       # the team's day: a due date ends at 23:59:59 Vietnam time


def due_instant(iso_date: str) -> str:
    """aTask stores due dates as UTC instants (BPT-31: 2026-08-07T16:59:59Z = 07/08 23:59:59 +07) and rejects a
    bare date ("could not be parsed at index 10", real run 30/10)."""
    if not iso_date:
        return ""
    end = dt.datetime.combine(dt.date.fromisoformat(iso_date), dt.time(23, 59, 59), VN)
    return end.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- configuration
def lists_path() -> str:
    return os.path.join(paths.work_dir(), "task_lists.json")


def load_lists() -> dict:
    """{"default": key, "lists": {key: {...}}}; missing file = no list (task commands refuse)."""
    try:
        with open(lists_path(), encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {"default": "", "lists": {}}
    lists = data.get("lists") if isinstance(data, dict) else None
    if not isinstance(lists, dict) or not all(isinstance(v, dict) and v.get("list_id") for v in lists.values()):
        raise ValueError("task_lists.json: 'lists' must map a key to {list_id, …}")
    return {"default": str(data.get("default") or ""), "lists": {str(k): v for k, v in lists.items()}}


def pick_list(cfg: dict, wanted: str):
    """(key, entry) or (None, reason). `wanted` = a key, or a unique part of the list name."""
    lists = cfg.get("lists") or {}
    w = (wanted or cfg.get("default") or "").strip()
    if not w:
        return None, "chưa đặt list mặc định (work/task_lists.json)"
    if w in lists:
        return w, lists[w]
    low = w.lower()
    hits = [(k, v) for k, v in lists.items() if low in (str(v.get("name") or "") + " " + str(v.get("project") or "")).lower()]
    if len(hits) == 1:
        return hits[0]
    return None, ("list '%s' không có trong danh sách được phép" % w if not hits else
                  "list '%s' khớp nhiều list: %s" % (w, ", ".join(k for k, _ in hits[:6])))


def list_label(entry: dict) -> str:
    return " › ".join(x for x in (entry.get("project"), entry.get("name")) if x) or entry.get("list_id", "?")


def lists_text(cfg: dict) -> str:
    rows = [f"• {k}: {list_label(v)}" + (" (mặc định)" if k == cfg.get("default") else "")
            for k, v in sorted((cfg.get("lists") or {}).items(), key=lambda kv: (len(kv[0]), kv[0]))]
    return "🤖 List aTask dùng được với 'list: <mã>':\n" + ("\n".join(rows) or "• (chưa cấu hình)")


# ---------------------------------------------------------------- parsing
def parse_create(body: str, today: dt.date = None):
    """('ok', fields) or ('error', reason). Fields: title, assignee (raw), due (date|None), desc, list (raw)."""
    today = today or dt.date.today()
    parts = [p.strip() for p in re.split(r"\s*\|\s*", (body or "").strip())]
    title = render.one_line(parts[0] if parts else "")
    if not title:
        return "error", "thiếu tiêu đề — @bot tạo task: <tiêu đề> | giao: @X | hạn: dd/mm/yyyy"
    if len(title) > TITLE_MAX:
        return "error", f"tiêu đề dài quá {TITLE_MAX} ký tự"
    out = {"title": title, "assignee": "", "due": None, "desc": "", "list": ""}
    for part in parts[1:]:
        label, sep, value = part.partition(":")
        field = _LABELS.get(" ".join(label.lower().split()))
        if not sep or not field:
            return "error", f"không hiểu '{render.one_line(part, 40)}' — dùng giao: / hạn: / mô tả: / list:"
        out[field] = value.strip()
    if out["due"]:
        m = re.match(r"^(\d{1,2})/(\d{1,2})(?:/(\d{4}))?$", out["due"])
        if not m:
            return "error", "hạn phải dạng dd/mm/yyyy"
        try:
            due = dt.date(int(m.group(3) or today.year), int(m.group(2)), int(m.group(1)))
        except ValueError:
            return "error", "hạn không phải ngày có thật"
        if due < today:
            return "error", "hạn đã qua"
        out["due"] = due
    else:
        out["due"] = None
    return "ok", out


def resolve_assignee(raw: str, mentions, team: dict, requester: dict):
    """(person|None, error|''). person = {key, name, fchat_id, atask}. A mention only POINTS at someone: the
    aTask id comes from the registry (a unique, linked member), never from the message."""
    from agent_skill_toolkit.core import identity
    name = (raw or "").strip()
    if not name:
        return None, ""
    if name.lower() in _SELF:
        key, rec, amb = identity.find(team, requester.get("id"))
        label = requester.get("name") or ""
    else:
        name = name.lstrip("@").strip()
        uid = next((str(m.get("userId")) for m in mentions or () if isinstance(m, dict)
                    and render.one_line(m.get("target")).lower() == name.lower() and m.get("userId")), "")
        if uid:
            key, rec, amb = identity.find(team, uid)
        else:
            hits = [(k, r) for k, r in team.items() if isinstance(r, dict)
                    and render.one_line(r.get("name")).lower() == name.lower()]
            key, rec, amb = (*hits[0], False) if len(hits) == 1 else (None, None, len(hits) > 1)
        label = name
    if amb:
        return None, f"'{label}' khớp nhiều hồ sơ trong danh bạ"
    h = (rec or {}).get("handles") or {}
    atask = str(h.get("atask_user_id") or "")
    if not rec or not atask.isdigit() or not str(h.get("fchat_id") or "").strip():
        return None, f"'{label}' chưa liên kết đủ FChat + aTask trong danh bạ (team.py set … --fchat-id … --atask-id …)"
    if sum(1 for r in team.values() if isinstance(r, dict)
           and str((r.get("handles") or {}).get("atask_user_id") or "") == atask) > 1:
        return None, f"tài khoản aTask của '{label}' gắn với nhiều hồ sơ trong danh bạ — báo chủ nhóm sửa"
    return {"key": key, "name": rec.get("name") or label,
            "fchat_id": str((rec.get("handles") or {}).get("fchat_id") or ""), "atask": atask}, ""


# ---------------------------------------------------------------- rendering
T_STATE_WORD = {"draft": "chờ xác nhận", "creating": "chờ xác nhận", "uncertain": "lỗi", "error": "lỗi",
                "created": "đã tạo", "done_asked": "đã tạo", "completing": "đã tạo", "done": "xong",
                "cancelled": "huỷ", "expired": "huỷ"}


def render_task(card: dict, note: str = "") -> tuple:
    p = card.get("payload") or {}
    req, asg = p.get("requester") or {}, p.get("assignee") or {}
    people = []
    head = [f"Trạng thái: {T_STATE_WORD.get(card.get('state'), 'chờ xác nhận')}"]
    for label, who in (("Yêu cầu", req), ("Giao", asg)):
        if who.get("name"):
            n = render._value(who["name"], 60)
            head.append(f"{label}: @{n}")
            if who.get("fchat_id") or who.get("id"):
                people.append((n, who.get("fchat_id") or who.get("id")))
    lines = [f"🤖 [{card['id']}] {render._value(p.get('title') or card.get('title') or '?', 120)}", " · ".join(head)]
    meta = []
    if p.get("due"):
        meta.append("Hạn: " + dt.date.fromisoformat(p["due"]).strftime("%d/%m/%Y"))
    if p.get("list"):
        meta.append("List: " + render._value(list_label(p["list"]), 100))
    if p.get("code"):
        meta.append("aTask: " + render._value(p["code"], 40))
    if meta:
        lines.append(" · ".join(meta))
    if p.get("desc"):
        lines.append("Mô tả: " + render._value(p["desc"], 300))
    if note:
        lines.append("Ghi chú: " + render._value(note, 400))
    if p.get("link"):
        lines.append("🔗 " + p["link"])
    return render.defuse_body("\n".join(lines)), people


def _card(card, note=""):
    return {"card": card, "send": render_task(card, note)}


def _reply(ident: dict, text: str):
    n = render.one_line(ident.get("name") or "?", 60)
    return {"reply": (render.defuse(f"🤖 @{n}: {text}"), [(n, ident.get("fchat_id") or "")])}


def marker(group: str, card_id: str) -> str:
    """Idempotency key written into the task description: finds a task created by an attempt whose answer
    was lost (timeout) instead of creating a second one."""
    return f"[FChat {card_id} · nhóm {group}]"


# ---------------------------------------------------------------- transitions
def draft(board, group: str, ident: dict, fields: dict, assignee, entry: tuple, owner_name: str, now: float = None,
          owner_id: str = ""):
    now = now or time.time()
    key, lst = entry
    payload = {"requester": {"id": ident.get("fchat_id") or "", "name": ident.get("name") or ""},
               "assignee": assignee or {}, "title": fields["title"], "desc": fields.get("desc") or "",
               "due": fields["due"].isoformat() if fields.get("due") else "",
               "list": {"key": key, **{k: lst.get(k) for k in ("name", "project", "project_id", "list_id")}},
               "drafted_at": now, "owner_name": owner_name, "owner_id": str(owner_id or "")}
    card = board.card_open(group, "T", f"draft:{uuid.uuid4().hex}", fields["title"], payload)
    card = board.card_update(group, card["id"], state="draft")
    return _card(card, f"gõ \"xác nhận {card['id']}\" để tạo trên aTask, \"huỷ {card['id']}\" để bỏ "
                       f"(nháp hết hạn sau 24 giờ); task đứng tên tài khoản {owner_name}")


def _get(board, group, card_id):
    card = board.card_get(group, (card_id or "").upper())
    return card if card and card.get("kind") == "T" else {}


def _is_requester(card, ident):
    return str((card["payload"].get("requester") or {}).get("id") or "") == str(ident.get("fchat_id") or "-")


def _done_people(card):
    p = card["payload"]
    return {str((p.get("requester") or {}).get("id") or ""), str((p.get("assignee") or {}).get("fchat_id") or "")} - {""}


def confirm(board, group: str, card_id: str, ident: dict, at, now: float = None, wait=time.sleep):
    """Create the task (requester only). Single winner: the draft is CLAIMED (revision check) before calling
    aTask; any earlier attempt ⇒ look for the marker first (a lost answer must not create a duplicate)."""
    now = now or time.time()
    card = _get(board, group, card_id)
    if not card:
        return _reply(ident, f"không thấy thẻ {card_id.upper()} trong nhóm này")
    if not _is_requester(card, ident):
        return _reply(ident, f"chỉ người tạo nháp {card['id']} mới xác nhận được")
    p, st = card["payload"], card["state"]
    if st in ("created", "done_asked", "completing", "done"):
        return _reply(ident, f"{card['id']} đã tạo trên aTask rồi" + (f": {p['link']}" if p.get("link") else ""))
    if st in ("cancelled", "expired"):
        return _reply(ident, f"{card['id']} đã huỷ — tạo nháp mới")
    if st == "creating" and now - (p.get("claimed_at") or 0) < STUCK_S:
        return _reply(ident, f"{card['id']} đang được tạo")
    if st == "draft" and now - (p.get("drafted_at") or now) > DRAFT_TTL:
        board.card_update(group, card["id"], state="expired")
        return _reply(ident, f"nháp {card['id']} đã quá 24 giờ — tạo nháp mới")
    attempts = int(p.get("attempts") or 0)
    claimed = board.card_update(group, card["id"], state="creating", if_revision=card["revision"],
                                attempts=attempts + 1, claimed_at=now)
    if not claimed:
        return _reply(ident, f"{card['id']} đang được xử lý")
    lst, mk = p["list"], marker(group, card["id"])
    task = {}
    if attempts:                                              # an earlier attempt may have created it
        task = at.find_by_marker(p["title"], lst["list_id"], mk)
        if task is None:
            card = board.card_update(group, card["id"], state="uncertain",
                                     last_error="aTask không trả lời khi đối soát")
            return _card(card, f"chưa biết task đã được tạo chưa (aTask không trả lời) — gõ \"xác nhận {card['id']}\" "
                               "lại sau, bot đối soát trước khi tạo")
    if not task:
        owner = p.get("owner_name") or "chủ nhóm"
        desc = "\n".join(x for x in (p.get("desc"), f"Tạo từ FPT Chat bởi {p['requester'].get('name') or '?'} "
                                                    f"(bằng tài khoản {owner}). {mk}") if x)
        r = at.create_task(p["title"], lst["list_id"], desc, due_instant(p.get("due") or ""))
        if r.get("error"):
            state = "uncertain" if r.get("uncertain") else "error"
            card = board.card_update(group, card["id"], state=state, last_error=render.one_line(r.get("message"), 200))
            why = ("không chắc aTask đã nhận — gõ \"xác nhận %s\" lại, bot đối soát trước khi tạo" % card["id"]
                   if state == "uncertain" else
                   "aTask từ chối: %s — sửa rồi gõ \"xác nhận %s\" lại, hoặc \"huỷ %s\""
                   % (render.one_line(r.get("message"), 160), card["id"], card["id"]))
            return _card(card, why)
        task = at.content(r)
        if not task.get("id"):
            card = board.card_update(group, card["id"], state="uncertain", last_error="create_task không trả id")
            return _card(card, "aTask không trả mã task — gõ \"xác nhận %s\" lại để đối soát" % card["id"])
    for delay in (0,) + CODE_RETRIES:
        if task.get("taskCode"):
            break
        if delay:
            wait(delay)
        found = at.find_by_marker(p["title"], lst["list_id"], mk) or {}
        task = {**found, **{k: v for k, v in task.items() if v}}
    upd = {"task_id": task["id"], "code": task.get("taskCode") or "", "link": at.task_link(task.get("taskCode") or "")}
    # names in notes are plain text: the people are already tagged once on the header line
    owner, who = p.get("owner_name") or "chủ nhóm", render._value(p["requester"].get("name"), 60)
    same = bool(p.get("owner_id")) and str(p["requester"].get("id")) == p["owner_id"]     # by id, never by name
    note = f"tạo bằng tài khoản {owner}" + ("" if same else f" thay {who}")
    if not task.get("taskCode"):
        note += "; aTask chưa trả mã task — xem trong list"
    asg = p.get("assignee") or {}
    if asg.get("atask") and not p.get("assigned"):
        r = at.assign_users(task["id"], [asg["atask"]])
        if r.get("error"):
            upd["assign_error"] = render.one_line(r.get("message"), 200)
            note += f"; CHƯA giao được cho {render._value(asg.get('name'), 60)} trên aTask — giao tay"
        else:
            upd["assigned"] = True
    card = board.card_update(group, card["id"], state="created", created_at=now, **upd)
    return _card(card, note)


def cancel(board, group: str, card_id: str, ident: dict):
    card = _get(board, group, card_id)
    if not card:
        return _reply(ident, f"không thấy thẻ {card_id.upper()} trong nhóm này")
    if not _is_requester(card, ident):
        return _reply(ident, f"chỉ người tạo nháp {card['id']} mới huỷ được")
    st = card["state"]
    if st in ("cancelled", "expired"):
        return _reply(ident, f"{card['id']} đã huỷ rồi")
    if st in ("uncertain", "creating"):
        return _reply(ident, f"chưa biết task {card['id']} đã có trên aTask chưa — gõ \"xác nhận {card['id']}\" để "
                             "bot đối soát trước")
    if st != "draft" and st != "error":
        return _reply(ident, f"{card['id']} đã tạo trên aTask — đóng/xoá trên aTask, bot không xoá task")
    card = board.card_update(group, card["id"], state="cancelled", if_revision=card["revision"])
    if not card:
        return _reply(ident, "thẻ vừa đổi trạng thái — thử lại")
    return _card(card, "đã huỷ nháp — chưa tạo gì trên aTask")


def done(board, group: str, card_id: str, ident: dict, now: float = None):
    """Ask again before closing (guardrail 2): only the requester or the assignee."""
    now = now or time.time()
    card = _get(board, group, card_id)
    if not card:
        return _reply(ident, f"không thấy thẻ {card_id.upper()} trong nhóm này")
    if str(ident.get("fchat_id") or "-") not in _done_people(card):
        return _reply(ident, f"chỉ người yêu cầu hoặc người được giao {card['id']} mới báo xong được")
    if card["state"] == "done":
        return _reply(ident, f"{card['id']} đã xong rồi")
    stuck = card["state"] == "completing" and now - (card["payload"].get("claimed_at") or 0) >= STUCK_S
    if card["state"] == "completing" and not stuck:
        return _reply(ident, f"{card['id']} đang được đóng")
    if card["state"] not in ("created", "done_asked") and not stuck:
        return _reply(ident, f"{card['id']} chưa được tạo trên aTask")
    board.card_update(group, card["id"], state="done_asked", done_by=ident.get("fchat_id"), done_asked_at=now)
    return _reply(ident, f"đóng {card['id']} ({card['payload'].get('code') or 'aTask'}) là trạng thái cuối — gõ "
                         f"\"xác nhận xong {card['id']}\" trong 10 phút để đóng")


def confirm_done(board, group: str, card_id: str, ident: dict, at, now: float = None):
    now = now or time.time()
    card = _get(board, group, card_id)
    if not card:
        return _reply(ident, f"không thấy thẻ {card_id.upper()} trong nhóm này")
    p = card["payload"]
    if card["state"] == "done":
        return _reply(ident, f"{card['id']} đã xong rồi")
    if card["state"] == "completing":           # a claim in flight, or one whose process died (then: "xong" again)
        return _reply(ident, f"{card['id']} đang được đóng" if now - (p.get("claimed_at") or 0) < STUCK_S else
                      f"lần đóng trước của {card['id']} bị ngắt — gõ \"xong {card['id']}\" để làm lại")
    if card["state"] != "done_asked" or str(p.get("done_by")) != str(ident.get("fchat_id")) \
            or now - (p.get("done_asked_at") or 0) > DONE_TTL:
        return _reply(ident, f"gõ \"xong {card['id']}\" trước (xác nhận có hạn 10 phút, cùng một người)")
    claimed = board.card_update(group, card["id"], state="completing", if_revision=card["revision"], claimed_at=now)
    if not claimed:
        return _reply(ident, f"{card['id']} đang được xử lý")
    r = at.complete_task(p["task_id"])
    if r.get("error"):
        card = board.card_update(group, card["id"], state="created",
                                 last_error=render.one_line(r.get("message"), 200))
        return _card(card, ("không chắc đã đóng — kiểm trên aTask" if r.get("uncertain") else
                            "aTask từ chối đóng: " + render.one_line(r.get("message"), 160)))
    card = board.card_update(group, card["id"], state="done", done_at=now)
    n = render._value(ident.get("name") or "?", 60)
    return _card(card, f"đã đóng trên aTask theo xác nhận của {n}")
