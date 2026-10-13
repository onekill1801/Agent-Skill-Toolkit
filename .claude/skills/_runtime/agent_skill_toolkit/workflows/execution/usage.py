"""Usage throttling + hard budgets (design §7.1, checklist F.11).

The toolkit cannot read a subscription's usage percentage itself; YOU (or a script of yours) report
it, and it expires after USAGE_TTL_S so a forgotten value never throttles forever:

    coordinator.py usage 85 [--source claude]     # -> tier "save"
    coordinator.py budget --daily-agent-runs 40   # hard cap per local day (0 = none)

Tiers (one policy for every worker — applied inside Store.claim through `claim_policy`):
    normal    < 80%   everything
    save     >= 80%   no proposal lane / weekly scan; models: opus->sonnet, light roles -> haiku
    critical >= 95%   only review_mr and the small lane (nho); the rest waits in the queue

Hard budgets (engine, before a step runs):
    task data {"budget": {"agent_runs": N, "agent_seconds": S}}  -> over it: blocked (a human decides)
    daily_agent_runs (setting)                                     -> over it: paused until midnight
"""

import json
import time

SAVE, CRITICAL = 80, 95
USAGE_TTL_S = 6 * 3600
AGENT_BACKEND = "agent_runner"          # usage rows written for agent turns (ctx.usage)
LIGHT_ROLES = ("log_read", "classify", "summarize", "triage", "handoff")
SAVE_MODEL_MAP = {"opus": "sonnet"}

POLICIES = {
    "normal": {},
    "save": {"deny_kinds": ["proposal"], "deny_lanes": ["de_xuat"], "deny_sources": ["schedule"]},
    "critical": {"only_kinds": ["review_mr"], "only_lanes": ["nho"]},
}


def tier_of(pct) -> str:
    if pct is None:
        return "normal"
    return "critical" if pct >= CRITICAL else "save" if pct >= SAVE else "normal"


def set_level(st, pct: float, source: str = "", ttl_s: int = USAGE_TTL_S) -> dict:
    """Record the reported usage and install the matching claim policy (expires with the report)."""
    pct = float(pct)
    if not 0 <= pct <= 100:
        raise ValueError("usage must be a percentage 0..100")
    tier = tier_of(pct)
    now = time.time()
    st.set_setting("usage", json.dumps({"pct": pct, "at": now, "source": source, "tier": tier}))
    st.set_setting("claim_policy", json.dumps({**POLICIES[tier], "tier": tier, "expires_at": now + ttl_s}))
    return level(st)


def level(st) -> dict:
    """{"pct", "tier", "at", "source", "fresh"} — a stale or missing report counts as normal."""
    try:
        u = json.loads(st.get_setting("usage") or "{}")
    except ValueError:
        u = {}
    fresh = bool(u) and time.time() - float(u.get("at") or 0) < USAGE_TTL_S
    pct = u.get("pct") if fresh else None
    return {"pct": pct, "tier": tier_of(pct), "at": u.get("at"), "source": u.get("source", ""), "fresh": fresh}


def model_for(role: str, model: str, tier: str) -> str:
    """Model to use for an agent candidate under the current tier (design §8: model per step)."""
    if tier not in ("save", "critical") or not model:
        return model
    if role in LIGHT_ROLES:
        return "haiku" if model in ("opus", "sonnet") else model
    return SAVE_MODEL_MAP.get(model, model)


# ----------------------------------------------------------------------------- hard budgets

def _local_midnight(now: float = None) -> float:
    lt = time.localtime(now or time.time())
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))


def agent_usage(st, task_id: str = "", since: float = 0) -> dict:
    q = "select count(*) n, coalesce(sum(seconds),0) s from usage where backend=? and ts>=?"
    args = [AGENT_BACKEND, since]
    if task_id:
        q += " and task_id=?"
        args.append(task_id)
    r = st.db.execute(q, args).fetchone()
    return {"runs": r["n"], "seconds": float(r["s"] or 0)}


def check_budgets(st, task: dict) -> tuple:
    """(kind, message, resume_at) when a budget is spent, else None.
    kind = "task" (block: a human raises or drops it) | "daily" (pause until local midnight)."""
    b = (task.get("data") or {}).get("budget") or {}
    if b:
        used = agent_usage(st, task["id"])
        if b.get("agent_runs") is not None and used["runs"] >= int(b["agent_runs"]):
            return ("task", f"budget spent: {used['runs']}/{b['agent_runs']} agent runs for this task", None)
        if b.get("agent_seconds") is not None and used["seconds"] >= float(b["agent_seconds"]):
            return ("task", f"budget spent: {used['seconds']:.0f}/{b['agent_seconds']} agent seconds", None)
    try:
        cap = int(st.get_setting("daily_agent_runs") or 0)
    except ValueError:
        cap = 1                                   # unreadable cap: the safest reading
    if cap:
        today = agent_usage(st, since=_local_midnight())
        if today["runs"] >= cap:
            return ("daily", f"daily budget spent: {today['runs']}/{cap} agent runs today",
                    _local_midnight() + 86400 + 60)
    return None
