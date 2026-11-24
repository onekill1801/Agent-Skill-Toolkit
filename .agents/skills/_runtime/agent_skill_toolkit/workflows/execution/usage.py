"""Usage throttling + hard budgets (design §7.1, checklist F.11).

The toolkit cannot read a subscription's usage percentage itself; YOU (or a script of yours) report
it, and it expires after USAGE_TTL_S so a forgotten value never throttles forever:

    coordinator.py usage 85 [--source claude] [--resets-at EPOCH]   # -> tier "save"
    coordinator.py budget --daily-agent-runs 40                     # hard cap per local day (0 = none)

Tiers (one policy for every worker — applied inside Store.claim through `claim_policy`):
    normal     < 80%   everything
    save      >= 80%   no proposal lane / weekly scan; models: opus->sonnet, light roles -> haiku
    critical  >= 95%   only review_mr and the small lane (nho); the rest waits in the queue
    exhausted   100%   nothing new is claimed and every agent turn is refused (pause until reset)

Hard budgets are enforced at the AGENT TURN boundary (agent_runner.run_turn -> reserve()), not per
step: every turn — fallback candidates, cross-review chunks, fixers, direct callers — reserves
atomically BEFORE it starts, with its worst-case duration, and is settled to the real duration after.
A turn killed mid-way keeps its worst case (Codex F.11 #1-#4).
    task data {"budget": {"agent_runs": N, "agent_seconds": S}} -> refused: kind "budget" (blocked)
    daily_agent_runs setting (all callers, all tasks)          -> refused: kind "quota" until midnight
"""

import json
import time

SAVE, CRITICAL, EXHAUSTED = 80, 95, 100
MIN_TURN_S = 10                         # a turn with less time left than this is refused, not squeezed
USAGE_TTL_S = 6 * 3600
AGENT_BACKEND = "agent_runner"          # usage rows of agent turns (reserve / settle)
LIGHT_ROLES = ("log_read", "classify", "summarize", "triage", "handoff")
SAVE_MODEL_MAP = {"opus": "sonnet"}
TIERS = ("normal", "save", "critical", "exhausted")

POLICIES = {
    "normal": {},
    "save": {"deny_kinds": ["proposal"], "deny_lanes": ["de_xuat"], "deny_sources": ["schedule"]},
    "critical": {"only_kinds": ["review_mr"], "only_lanes": ["nho"]},
    "exhausted": {"only_kinds": [], "only_lanes": []},
}


class BudgetSpent(Exception):
    """kind "budget" (this task's budget: a human decides) | "quota" (daily cap / usage exhausted:
    pause until resume_at)."""

    def __init__(self, kind: str, message: str, resume_at: float = None):
        super().__init__(message)
        self.kind, self.resume_at = kind, resume_at


def tier_of(pct) -> str:
    if pct is None:
        return "normal"
    return ("exhausted" if pct >= EXHAUSTED else "critical" if pct >= CRITICAL
            else "save" if pct >= SAVE else "normal")


def set_level(st, pct: float, source: str = "", ttl_s: int = USAGE_TTL_S, resets_at: float = None) -> dict:
    """Record the reported usage and install the matching claim policy (expires with the report, or
    at `resets_at` when you know the provider's reset time)."""
    pct = float(pct)
    if not 0 <= pct <= 100:
        raise ValueError("usage must be a percentage 0..100")
    tier = tier_of(pct)
    now = time.time()
    until = float(resets_at) if resets_at and float(resets_at) > now else now + ttl_s
    st.set_setting("usage", json.dumps({"pct": pct, "at": now, "until": until, "source": source, "tier": tier}))
    st.set_setting("claim_policy", json.dumps({**POLICIES[tier], "tier": tier, "expires_at": until}))
    return level(st)


def level(st) -> dict:
    """{"pct", "tier", "at", "until", "source", "fresh"} — a stale or missing report counts as normal."""
    try:
        u = json.loads(st.get_setting("usage") or "{}")
        u = u if isinstance(u, dict) else {}
    except ValueError:
        u = {}
    try:
        fresh = bool(u) and time.time() < float(u.get("until") or (float(u.get("at") or 0) + USAGE_TTL_S))
        pct = float(u["pct"]) if fresh else None
    except (TypeError, ValueError, KeyError):
        fresh, pct = False, None
    return {"pct": pct, "tier": tier_of(pct), "at": u.get("at"), "until": u.get("until"),
            "source": u.get("source", ""), "fresh": fresh}


def model_for(role: str, model: str, tier: str) -> str:
    """Model to use for a claude turn under the current tier (design §8: model per step)."""
    if tier not in ("save", "critical", "exhausted") or not model:
        return model
    if (role or "") in LIGHT_ROLES:
        return "haiku" if model in ("opus", "sonnet") else model
    return SAVE_MODEL_MAP.get(model, model)


# ----------------------------------------------------------------------------- hard budgets

def local_midnight(now: float = None) -> float:
    lt = time.localtime(now or time.time())
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))


def next_local_midnight(now: float = None) -> float:
    """The next calendar day's 00:00 local time (DST-safe: mktime normalises day+1 — Codex F.11 #8)."""
    lt = time.localtime(now or time.time())
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday + 1, 0, 0, 0, 0, 0, -1))


def agent_usage(st, task_id: str = "", since: float = 0) -> dict:
    q = "select count(*) n, coalesce(sum(seconds),0) s from usage where backend=? and ts>=?"
    args = [AGENT_BACKEND, since]
    if task_id:
        q += " and task_id=?"
        args.append(task_id)
    r = st.db.execute(q, args).fetchone()
    return {"runs": r["n"], "seconds": float(r["s"] or 0)}


def _int_setting(st, key: str) -> int:
    try:
        return max(0, int(st.get_setting(key) or 0))
    except ValueError:
        return 1                                   # unreadable cap: the safest reading


def reserve(st, task_id: str, step: str, backend: str, model: str, timeout: float,
            account: str = "") -> dict:
    """Atomically admit ONE agent turn: tier, daily cap, task budget — then record it at its worst
    case (the timeout it may use); `account` (agents.json name) is kept in the model column as
    "backend:model@account" so `accounts.py` can tell which account did what. Returns {"row", "timeout"} (timeout possibly shortened to what is
    left of the task's seconds budget). Raises BudgetSpent."""
    timeout = float(timeout)
    with st._tx():
        lv = level(st)
        if lv["tier"] == "exhausted":
            raise BudgetSpent("quota", f"usage reported at {lv['pct']:.0f}% — agent turns paused until the reset",
                              lv["until"] or time.time() + 1800)
        cap = _int_setting(st, "daily_agent_runs")
        if cap:
            today = agent_usage(st, since=local_midnight())
            if today["runs"] >= cap:
                raise BudgetSpent("quota", f"daily budget spent: {today['runs']}/{cap} agent runs today",
                                  next_local_midnight() + 60)
        if task_id:
            row = st.db.execute("select data from tasks where id=?", (task_id,)).fetchone()
            b = (json.loads(row["data"] or "{}") if row else {}).get("budget") or {}
            used = agent_usage(st, task_id)
            if b.get("agent_runs") is not None and used["runs"] >= int(b["agent_runs"]):
                raise BudgetSpent("budget", f"budget spent: {used['runs']}/{b['agent_runs']} agent runs for this task")
            if b.get("agent_seconds") is not None:
                left = float(b["agent_seconds"]) - used["seconds"]
                if left < MIN_TURN_S:                      # never launch a turn longer than what is left
                    raise BudgetSpent("budget", f"budget spent: {used['seconds']:.0f}/{b['agent_seconds']} "
                                                "agent seconds for this task")
                timeout = min(timeout, left)
        cur = st.db.execute("insert into usage(task_id, step, backend, model, seconds, ts) values (?,?,?,?,?,?)",
                            (task_id or None, step or "", AGENT_BACKEND,
                             f"{backend}:{model or ''}" + (f"@{account}" if account else ""),
                             timeout, time.time()))
    return {"row": cur.lastrowid, "timeout": timeout}


def settle(st, row: int, seconds: float) -> None:
    """Replace the worst case by the REAL duration — even if it ran over (accounting tells the truth;
    the hard stop is the turn's deadline — Codex F.11 R2)."""
    st.db.execute("update usage set seconds=? where id=?", (float(seconds), int(row)))
