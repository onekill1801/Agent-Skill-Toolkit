"""`atask_intake` tasks (queued by `atask_watch --mode store`, checklist F.5/F.7): a task newly
assigned to you in aTask -> a PROPOSAL (kind of work + lane), then it waits for you.

    triage   kind/lane from the task's name + type (deterministic; the text is data)
    propose  blocks the task with the proposal; you decide:
               coordinator.py accept <id> [--lane vua]   -> done, decision recorded
               coordinator.py cancel <id>                -> dropped

Nothing is executed from here: accepting records the decision; starting the work is a separate,
explicit step (design §3: aTask = medium trust, "phân tích -> đề xuất làn (bạn xác nhận)").
"""

from agent_skill_toolkit.workflows import engine as E
from agent_skill_toolkit.workflows.intake import lanes

KIND = "atask_intake"
PROPOSAL = "proposal: "      # last_error prefix of a task waiting for your decision


def step_triage(ctx) -> dict:
    a = ctx.data.get("atask") or {}
    text = " ".join(str(a.get(k) or "") for k in ("name", "description"))
    kind = lanes.kind_of(str(a.get("type") or ""), text)
    lane = lanes.lane_for("standard", kind, text)
    return {"kind": kind, "lane": lane["lane"], "reason": lane["reason"],
            "title": lanes.clean(a.get("name") or "", 200)}


def step_propose(ctx) -> dict:
    t = ctx.outputs.get("triage") or {}
    raise E.NeedsHuman(f"{PROPOSAL}{t.get('kind')} / lane {t.get('lane') or '-'} ({t.get('reason')}) for "
                       f"{lanes.quoted(t.get('title') or '', 120)} — accept: coordinator.py accept "
                       f"{ctx.task['id']} [--lane nho|vua|lon|de_xuat], or cancel it")


STEPS = [("triage", step_triage), ("propose", step_propose)]
REGISTRY = {KIND: STEPS}
