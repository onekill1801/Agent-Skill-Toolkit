"""Review cards R-n (design §4.1, §4.4, phase 1a). State over core.board_store; sending is the caller's.

One card per (group, MR) for its whole life: `ref = <gitlab project id>!<iid>`. Less noise than the old flow:
  * a request does NOT send an ack; only if no result came within ACK_AFTER_S the card goes out as `chờ bot`
  * the MR asked again while its head commit is the one already reviewed -> the existing card is shown again
  * asked again while that round is still running -> the card is shown, the round's clock is NOT reset
  * still `chờ bot` after OVERDUE_S -> the review job is checked every RECHECK_S; a job that ended without
    updating the card is reconciled (`posted` -> its verdict, failed/cancelled/rejected -> `lỗi`)

A ROUND is identified by the head commit it reviews (`round_head`); a result for another head never moves the
card (a late result of round A cannot overwrite round B).

Ordering across processes (watcher threads, the review_post sweeper): every "update the card, render, SEND" of
one MR runs under `lock(group, ref)` — a stale `chờ bot` can never be sent after the result.
"""

import re
import time

from agent_skill_toolkit.core import locks, paths
from agent_skill_toolkit.workflows.group_board import render

ACK_AFTER_S = 180
OVERDUE_S = 30 * 60
RECHECK_S = 5 * 60
JOB_DEAD = ("failed", "cancelled", "rejected")
VERDICT_STATES = tuple(render.VERDICT_STATE.values())


def ref_of(job: dict) -> str:
    return f"{job.get('project_id')}!{job.get('iid')}"


def lock(group: str, ref: str, timeout: float = 120):
    """Cross-process lock of one MR's card: hold it from reading the card to the end of sending."""
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", f"board-{group}-{ref}")
    return locks.file_lock(paths.state_dir("locks", name + ".lock"), timeout=timeout)


def _payload(job: dict) -> dict:
    return {k: v for k, v in {"project_id": job.get("project_id"), "iid": job.get("iid"),
                              "project_path": job.get("project_path"), "url": job.get("web_url"),
                              "mr_title": job.get("title"), "job_id": job.get("id")}.items() if v}


def _open(board, group, job):
    return board.card_open(group, "R", ref_of(job), title=render.one_line(job.get("title"), 120), payload=_payload(job))


def _requested_head(job: dict) -> str:
    # a request merged into a running job with a newer head: the queue re-reviews that head next
    return job.get("rerequested_head") or job.get("head_sha") or ""


def current_round(card: dict) -> str:
    """Head commit the card is about: the round in progress, else the last reviewed one."""
    p = card.get("payload") or {}
    return p.get("round_head") or p.get("head") or ""


def on_request(board, group: str, job: dict, requester: dict, unchanged: bool = False, now: float = None) -> dict:
    """A member asked for a review of `job`'s MR (caller holds lock(group, ref_of(job))).
    now = when the request STARTED being queued: a response older than the card's current round (two workers,
    the slower one finishing last) only shows the card — it never moves it.
    Returns {"card", "send": (text, people) | None}."""
    now = now or time.time()
    card = _open(board, group, job)
    p = card["payload"]
    head = _requested_head(job)
    if p.get("requested_at") and now < float(p["requested_at"]):
        card = board.card_update(group, card["id"], requester=requester)
        return {"card": card, "send": render.render_review(card, requester, note="yêu cầu đã gộp vào lượt hiện tại")}
    # already reviewed at this head (the queue said so, or the result landed before this intake ran)
    if card["state"] in VERDICT_STATES and ((head and p.get("head") == head) or (unchanged and not p.get("head"))):
        card = board.card_update(group, card["id"], requester=requester, **_payload(job))
        return {"card": card, "send": render.render_review(
            card, requester, note="chưa có commit mới từ lượt review trước — giữ kết quả này")}
    # (an "unchanged" answer that started AFTER the round in progress is the queue's current truth: the job is
    #  posted at that head — a stale one was already caught by the time check above)
    if unchanged and job.get("verdict"):        # posted before the board existed: the card learns the verdict
        card = board.card_update(group, card["id"], state=render.VERDICT_STATE.get(job["verdict"], "comment"),
                                 requester=requester, head=head, round_head=head, requested_at=now,
                                 ack_sent=True, **_payload(job))
        return {"card": card, "send": render.render_review(
            card, requester, note="chưa có commit mới từ lượt review trước — giữ kết quả này")}
    if card["state"] == "pending" and p.get("round_head") == head:
        # same round still running: show the card, keep its clock (no reset of the ack / overdue timers)
        card = board.card_update(group, card["id"], requester=requester)
        return {"card": card, "send": render.render_review(card, requester, note="đang review lượt này")}
    card = board.card_update(group, card["id"], state="pending", requester=requester, requested_at=now,
                             round_head=head, ack_sent=False, overdue_next_at=now + OVERDUE_S,
                             rounds=int(p.get("rounds") or 0) + 1, summary="", **_payload(job))
    return {"card": card, "send": None}


def due_acks(board, group: str, now: float = None) -> list:
    """Cards still `chờ bot` ACK_AFTER_S after the request, not announced yet. The caller, per card, takes
    lock(group, card.ref), calls ack_if_still_due, sends, then mark_acked."""
    now = now or time.time()
    return [c for c in board.cards_in(group, "R", ("pending",))
            if not c["payload"].get("ack_sent") and now - float(c["payload"].get("requested_at") or now) >= ACK_AFTER_S]


def ack_if_still_due(board, group: str, card_id: str, now: float = None):
    """Under the card lock: (card, (text, people)) to send, or None (result arrived / already announced)."""
    now = now or time.time()
    c = board.card_get(group, card_id)
    p = c.get("payload") or {}
    if c.get("state") != "pending" or p.get("ack_sent") or now - float(p.get("requested_at") or now) < ACK_AFTER_S:
        return None
    return c, render.render_review(c, p.get("requester"), note="đang review, kết quả sẽ báo ở đây")


def mark_acked(board, group: str, card: dict) -> dict:
    return board.card_update(group, card["id"], if_revision=card["revision"], ack_sent=True)


def overdue_due(board, group: str, now: float = None) -> list:
    """Cards `chờ bot` whose overdue check is due (first at OVERDUE_S, then every RECHECK_S)."""
    now = now or time.time()
    out = []
    for c in board.cards_in(group, "R", ("pending",)):
        p = c["payload"]
        nxt = p.get("overdue_next_at")
        if nxt is None:
            nxt = float(p.get("requested_at") or now) + OVERDUE_S
        if now >= float(nxt):
            out.append(c)
    return out


def reconcile_overdue(board, group: str, card_id: str, job: dict, now: float = None):
    """Under the card lock, with the job as read from the queue ({} = unknown). Returns (card, (text, people))
    to send when the card changed, else None (re-checked again after RECHECK_S)."""
    now = now or time.time()
    c = board.card_get(group, card_id)
    if c.get("state") != "pending":
        return None
    p = c["payload"]
    st = (job or {}).get("status") or ""
    # the job was read OUTSIDE the lock: it must be about the round the card is waiting for now
    rh = p.get("round_head")
    same_round = not rh or rh in ((job or {}).get("head_sha"), (job or {}).get("rerequested_head"))
    if not same_round:
        board.card_update(group, card_id, overdue_next_at=now + RECHECK_S)
        return None
    if st == "posted" and job.get("verdict") and (not rh or job.get("head_sha") == rh):
        c = board.card_update(group, card_id, state=render.VERDICT_STATE.get(job["verdict"], "comment"),
                              head=job.get("head_sha"), ack_sent=True,
                              summary=p.get("summary") or "đã đăng nhận xét trên GitLab")
        return c, render.render_review(c, p.get("requester"))
    if st in JOB_DEAD:
        c = board.card_update(group, card_id, state="error", summary=f"job review {st} — gửi lại yêu cầu")
        return c, render.render_review(c, p.get("requester"))
    board.card_update(group, card_id, overdue_next_at=now + RECHECK_S)      # running / unknown: look again later
    return None


def on_result(board, group: str, job: dict, verdict: str, summary: str, requester: dict) -> tuple:
    """The review of job.head_sha was posted (caller holds the card lock). Returns (card, (text, people)).
    A result for a head other than the round in progress is recorded but does not move the card."""
    card = _open(board, group, job)
    p = card["payload"]
    head = job.get("head_sha") or ""
    cur = current_round(card)
    if cur and head and head != cur:
        # a result for another commit than the card's round (a late / retried notice of an older round):
        # recorded, never moves the card — whether the card is still waiting or already has a newer verdict
        card = board.card_update(group, card["id"], last_result={"head": head, "verdict": verdict, "summary": summary})
        note = (f"kết quả lượt trước ({verdict}) — đang review commit mới" if card["state"] == "pending"
                else f"kết quả cho commit cũ ({verdict}) — thẻ giữ kết quả mới nhất")
        return card, render.render_review(card, requester or p.get("requester"), note=note)
    card = board.card_update(group, card["id"], state=render.VERDICT_STATE.get(verdict, "comment"),
                             summary=summary, head=head, round_head=head or p.get("round_head"), ack_sent=True,
                             # a result that OPENS the card's round (no request seen in the group yet) dates it:
                             # an older request answer arriving later is then caught by on_request's time check
                             **({} if p.get("requested_at") else {"requested_at": time.time()}),
                             **_payload(job))
    return card, render.render_review(card, requester or p.get("requester"))
