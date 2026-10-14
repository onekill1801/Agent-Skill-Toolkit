"""Who-does-what + how much is left, per agent account (work/agents.json).

Pure merge of four readings — the facade (auto-dev/tools/accounts.py) gathers them:
    cfg      work/agents.json (accounts.<backend>.<name>, optional "orca_id" = Orca managed account id;
             "system-default" on a codex account = the ~/.codex login Orca reports)
    orca     `orca account list --json` result, or None (Orca not running / CLI missing)
    slots    [{"key": "claude@dungpd15", "info": {...}, "mtime": t}] — live agent_runner slot files
    turns    usage rows of agent turns since local midnight (model = "backend:model@account")

Usage numbers come ONLY from Orca: the active account is live, the others are Orca's last cached
reading (`age_min` says how old). The toolkit never calls the provider's usage API itself.
"""

import time

SYSTEM_DEFAULT = "system-default"   # agents.json orca_id of the codex account Orca reads (~/.codex)


def _pct_left(bucket, now):
    if not isinstance(bucket, dict):
        return None
    used = bucket.get("usedPercent")
    if isinstance(used, bool) or not isinstance(used, (int, float)):
        return None
    resets_at = (bucket.get("resetsAt") or 0) / 1000 or None
    if resets_at and resets_at <= now:
        # the window reset AFTER this reading: the old number is wrong, the new one is unknown
        return {"used": None, "left": None, "resets_at": resets_at, "reset_passed": True}
    return {"used": used, "left": max(0, 100 - used), "resets_at": resets_at}


def _limits(entry, now):
    if not isinstance(entry, dict) or entry.get("status") not in (None, "ok") or entry.get("error"):
        return None
    upd = entry.get("updatedAt")
    return {"session": _pct_left(entry.get("session"), now), "weekly": _pct_left(entry.get("weekly"), now),
            "age_min": round((now - upd / 1000) / 60) if isinstance(upd, (int, float)) and upd else None}


def orca_usage(orca: dict, now: float) -> dict:
    """{orca_id: {"email", "org", "active", "usage", "source"}} from `orca account list`."""
    out = {}
    if not isinstance(orca, dict):
        return out
    claude = orca.get("claude") or {}
    rl = orca.get("rateLimits") or {}
    active = claude.get("activeAccountId")
    cached = {e.get("accountId"): e.get("rateLimits") for e in rl.get("inactiveClaudeAccounts") or []
              if isinstance(e, dict)}
    live = rl.get("claude") or {}
    live_owner = ((live.get("usageMetadata") or {}).get("authProvenance") or "")
    for a in claude.get("accounts") or []:
        aid = a.get("id")
        usage, source = None, "none"
        if aid == active and live_owner == f"managed:{aid}":
            usage, source = _limits(live, now), "live"
        elif aid in cached:
            usage, source = _limits(cached[aid], now), "cached"
        out[aid] = {"email": a.get("email"), "org": a.get("organizationName"), "active": aid == active,
                    "usage": usage, "source": source if usage else "none"}
    codex = rl.get("codex")
    if isinstance(codex, dict):              # Orca's own Codex reading = the machine's default login
        default = ((orca.get("codex") or {}).get("systemDefault") or {})
        u = _limits(codex, now)
        out[SYSTEM_DEFAULT] = {"email": default.get("email"), "org": default.get("workspaceLabel"),
                               "active": True, "usage": u, "source": "live" if u else "none"}
    return out


def _split_model(model: str):
    """'claude:sonnet@dungpd15' -> ('claude', 'sonnet', 'dungpd15'); no '@' -> account None."""
    head, _, account = (model or "").partition("@")
    backend, _, m = head.partition(":")
    return backend, m, account or None


def report(cfg: dict, orca: dict, slots: list, turns: list, now: float = None, stale_s: float = 1800) -> dict:
    """`stale_s` = agent_runner.SLOT_STALE_S: a slot file older than that (by mtime, the same basis
    agent_runner reclaims on) is a crashed run, not work in progress."""
    now = now or time.time()
    by_orca = orca_usage(orca, now)
    rows, index = [], {}
    for backend, accts in ((cfg or {}).get("accounts") or {}).items():
        for name, a in (accts or {}).items():
            a = a or {}
            o = by_orca.get(a.get("orca_id")) if a.get("orca_id") else None
            row = {"account": f"{backend}@{name}", "backend": backend, "name": name,
                   "orca_id": a.get("orca_id"), "email": (o or {}).get("email"), "org": (o or {}).get("org"),
                   "orca_active": bool((o or {}).get("active")),
                   "usage": (o or {}).get("usage"), "usage_source": (o or {}).get("source", "none"),
                   "running": [], "today": {"runs": 0, "seconds": 0.0}, "last": None}
            if a.get("orca_id") and o is None and orca is not None:
                row["warning"] = "orca_id không có trong Orca (đã xoá tài khoản?)"
            rows.append(row)
            index[row["account"]] = row
    other = {"account": "(không rõ tài khoản)", "running": [], "today": {"runs": 0, "seconds": 0.0}, "last": None}

    for s in slots or []:
        info = s.get("info") or {}
        started = s.get("mtime") or info.get("started") or now
        if now - started > stale_s:
            continue
        backend, _, name = (s.get("key") or "").partition("@")
        target = index.get(f"{backend}@{name}") or other
        target["running"].append({"role": info.get("role"), "task_id": info.get("task_id") or None,
                                  "step": info.get("step") or None, "spec": info.get("spec"),
                                  "pid": info.get("pid"), "minutes": round((now - started) / 60, 1)})
    for t in turns or []:
        backend, model, name = _split_model(t.get("model"))
        target = index.get(f"{backend}@{name}") if name else None
        target = target or other
        target["today"]["runs"] += 1
        target["today"]["seconds"] += float(t.get("seconds") or 0)
        if not target["last"] or (t.get("ts") or 0) >= target["last"]["ts"]:
            target["last"] = {"ts": t.get("ts") or 0, "task_id": t.get("task_id"), "step": t.get("step"),
                              "model": model or None}
    out = {"accounts": rows, "orca": "ok" if orca is not None else "unavailable"}
    if other["running"] or other["today"]["runs"]:
        out["unattributed"] = other
    return out
