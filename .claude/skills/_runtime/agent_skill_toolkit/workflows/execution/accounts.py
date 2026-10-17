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


def _api_usage(res: dict, now: float):
    """integrations.claude_usage result (possibly cached) -> usage dict with its age; a window that has
    reset since the reading becomes unknown, exactly like an old Orca reading."""
    u = dict(res["usage"])
    for k in ("session", "weekly"):
        b = u.get(k)
        if b and b.get("resets_at") and b["resets_at"] <= now:
            u[k] = {"used": None, "left": None, "resets_at": b["resets_at"], "reset_passed": True}
    u["age_min"] = round((now - (res.get("at") or now)) / 60)
    return u


API_ERRORS = {"expired": "token hết hạn (chưa gia hạn)", "missing": "chưa đăng nhập", "no_token": "chưa đăng nhập",
              "unreadable": "không đọc được credentials", "tls": "lỗi chứng chỉ TLS", "network": "lỗi mạng",
              "bad_response": "phản hồi lạ", "bad_url": "CLAUDE_USAGE_URL sai", "http_401": "token bị từ chối (401)",
              "http_403": "không có quyền (403)", "http_429": "bị giới hạn tần suất (429)"}


def report(cfg: dict, orca: dict, slots: list, turns: list, now: float = None, stale_s: float = 1800,
           api: dict = None) -> dict:
    """`stale_s` = agent_runner.SLOT_STALE_S: a slot file older than that (by mtime, the same basis
    agent_runner reclaims on) is a crashed run, not work in progress. `api` = {"claude@name":
    integrations.claude_usage result}: a successful direct reading wins over Orca's."""
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
            res = (api or {}).get(row["account"])
            if res and res.get("usage"):
                row["usage"], row["usage_source"] = _api_usage(res, now), "api"
            elif res and res.get("error"):
                row["api_error"] = API_ERRORS.get(res["error"], res["error"])
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


# ----------------------------------------------------------------------------- load balancing (F.22)
# agent_runner used to take a role's candidates in their written order: the first account did every turn
# until it ran out. order_by_load() re-orders each model group by how much each account has LEFT.

USAGE_CACHE = "account_usage.json"     # numbers only (never a token), shared across processes
USAGE_MAX_AGE_S = 900


TRANSIENT_MAX_AGE_S = 60               # a network / rate-limit miss is re-read soon, not after 15 min (R1 #2)
_TRANSIENT = ("network", "tls", "bad_response", "http_429", "http_500", "http_502", "http_503", "http_504")


def _pct(x):
    """A usage percentage, or None when it is not a finite number in 0..100 (R1 #4)."""
    import math
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) \
        and 0 <= x <= 100 else None


def normalize_readings(api: dict, now: float) -> dict:
    """read_api_usage() result -> {"claude@name": {"left": min(5h, week) % or None, "error": str?}}.
    A window that has reset since the reading counts as full (100)."""
    out = {}
    for label, res in (api or {}).items():
        if not isinstance(res, dict) or "usage" not in res:
            out[label] = {"left": None, "error": str((res or {}).get("error") or "unknown")}
            continue
        u, lefts = _api_usage(res, now), []
        for k in ("session", "weekly"):
            b = u.get(k)
            if b:
                lefts.append(100.0 if b.get("reset_passed") else _pct(b.get("left")))
        lefts = [x for x in lefts if x is not None]
        out[label] = {"left": min(lefts) if lefts else None}
    return out


def save_usage(path: str, api: dict, now: float = None, ids: dict = None) -> None:
    """Write the normalized readings atomically — a UNIQUE temp file per write (R1 #1) — with each
    account's non-secret identity (`ids`: credentials dir + endpoint fingerprint, R1 #3). Best effort."""
    import json
    import os
    import tempfile
    now = now or time.time()
    accounts = normalize_readings(api, now)
    for label, v in accounts.items():
        v["id"] = (ids or {}).get(label)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".", suffix=".tmp", dir=os.path.dirname(path))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"at": now, "accounts": accounts}, f)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
    except OSError:
        pass


def load_usage(path: str, max_age: float = USAGE_MAX_AGE_S, now: float = None, ids: dict = None):
    """{"claude@name": {"left": …}} when the cache is fresh and trustworthy, else None (= read again):
    the timestamp must be finite and not in the future (R1 #4); a transient miss older than
    TRANSIENT_MAX_AGE_S, or an account whose identity changed since (`ids`, R1 #3), voids the cache."""
    import json
    import math
    now = now or time.time()
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return None
    at = d.get("at") if isinstance(d, dict) else None
    if not isinstance(at, (int, float)) or isinstance(at, bool) or not math.isfinite(at) \
            or at > now + 60 or now - at > max_age:
        return None
    acc = d.get("accounts")
    if not isinstance(acc, dict):
        return None
    out = {}
    for label, v in acc.items():
        if not isinstance(v, dict):
            return None
        if ids is not None and label in ids and v.get("id") != ids[label]:
            return None                                    # account re-pointed: its old numbers are not its
        if v.get("error") in _TRANSIENT and now - at > TRANSIENT_MAX_AGE_S:
            return None
        out[label] = {"left": _pct(v.get("left")), **({"error": str(v["error"])} if v.get("error") else {})}
    if ids is not None and any(label not in out for label in ids):
        return None                                        # an account with no reading at all yet
    return out


def _parse(spec: str) -> tuple:
    head, _, account = spec.partition("@")
    backend, _, model = head.partition(":")
    return backend.strip(), model.strip(), account.strip()


def order_by_load(specs: list, usage: dict, runs_today: dict, min_left: float = 10.0) -> list:
    """Re-order the candidates of each (backend, model) group — the groups themselves keep their written
    order (all opus before any sonnet, codex last, …). Inside a group:
      1. accounts known to have >= min_left % left (5 h AND week), most left first,
      2. accounts with no reading (token expired, not logged in, no cache),
      3. accounts known to be below min_left — last resort,
    ties broken by fewer turns today, then the written order. Nothing is dropped."""
    groups, order = {}, []
    for i, spec in enumerate(specs):
        backend, model, account = _parse(spec)
        g = (backend, model)
        if g not in groups:
            groups[g] = []
            order.append(g)
        groups[g].append((i, spec, f"{backend}@{account or 'default'}"))

    def key(item):
        i, _, label = item
        left = ((usage or {}).get(label) or {}).get("left")
        tier = 1 if left is None else (0 if left >= min_left else 2)
        return (tier, -(left or 0), (runs_today or {}).get(label, 0), i)
    return [spec for g in order for _, spec, _ in sorted(groups[g], key=key)]
