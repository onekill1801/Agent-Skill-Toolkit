#!/usr/bin/env python3
"""Agent accounts: which account is doing what, and how much usage is left
(agent_skill_toolkit.workflows.execution.accounts). Read-only.

    python accounts.py            # table
    python accounts.py --json

Accounts = work/agents.json (fallback agents.sample.json). Usage % comes from Orca
(`orca account list`): the active account is live, the others are Orca's last cached reading —
the "cập nhật" column says how old. "Đang làm" = live agent_runner slots; "Hôm nay" = agent turns
recorded since local midnight (only turns run through a role carry an account).
"""

import argparse
import concurrent.futures
import json
import os
import re
import runpy
import sqlite3
import sys
import threading
import time
from pathlib import Path

runpy.run_path(str(Path(__file__).resolve().parents[2] / "_runtime" / "bootstrap.py"))["activate"]()
from agent_skill_toolkit.core import store as store_mod  # noqa: E402
from agent_skill_toolkit.integrations import claude_usage as CU  # noqa: E402
from agent_skill_toolkit.workflows.execution import accounts as A  # noqa: E402
from agent_skill_toolkit.workflows.execution import usage as usage_mod  # noqa: E402

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(1, os.path.join(_HERE, "..", "..", "dev-automation", "tools"))
import agent_runner  # noqa: E402
import orca_adapter  # noqa: E402
import probe_common as pc  # noqa: E402,F401  (UTF-8 stdout on Windows)


def read_slots(d: str) -> list:
    out = []
    try:
        names = os.listdir(d)
    except OSError:
        return out
    for n in names:
        if not n.endswith(".lock"):
            continue
        p = os.path.join(d, n)
        try:
            with open(p, encoding="utf-8") as f:        # closed at once: Windows must be able to delete it
                raw = f.read()
            mtime = os.path.getmtime(p)
        except OSError:
            continue                                    # slot released while listing
        try:
            info = json.loads(raw)
            info = info if isinstance(info, dict) else {"pid": info}
        except ValueError:
            info = {}                                   # old format: just a pid
        out.append({"key": n[:-len(".lock")].rsplit(".", 1)[0], "info": info, "mtime": mtime})
    return out


def read_turns(path: str = None) -> list:
    """Today's agent turns, read through a READ-ONLY connection: no Store() (it migrates the schema,
    takes write locks and would contend with agent reservations — Codex accounts #3)."""
    path = path or store_mod.default_path(create=False)       # never mkdir the state dir (Codex R2)
    if not os.path.exists(path):
        return []
    try:
        db = sqlite3.connect(Path(os.path.abspath(path)).as_uri() + "?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return []
    try:
        db.row_factory = sqlite3.Row
        rows = db.execute("select task_id, step, model, seconds, ts from usage where backend=? and ts>=?",
                          (usage_mod.AGENT_BACKEND, usage_mod.local_midnight())).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.Error:
        return []                                       # no usage table yet / locked: nothing to show
    finally:
        db.close()


API_CACHE_S = 300          # one direct usage call per account per 5 min (dashboard polls more often)
API_WAIT_S = CU.TIMEOUT_S + 5
# this process only, never written. key = (name, credentials dir, endpoint): a renamed / re-pointed
# account is a NEW entry, never the old account's numbers (Codex usage R1 #2)
_api_cache = {}            # key -> (fetched_at, result)
_inflight = {}             # key -> (Future, started): concurrent callers share ONE request (R1 #1)
STUCK_S = 3 * CU.TIMEOUT_S  # an in-flight request older than this is abandoned, a new one may start (R2 #1)
_api_lock = threading.Lock()


def _key(name: str, envd: dict) -> tuple:
    try:
        url = CU.usage_url()
    except ValueError:
        url = "?"
    return (name, os.path.normcase(os.path.abspath(CU.config_dir(envd))), url)


def _start(key, envd, timeout, asked_at, fetch=None) -> concurrent.futures.Future:
    """One daemon thread per fetch: nothing ever joins it, so a stuck request can not block the
    caller (the dashboard's single worker) past API_WAIT_S (R1 #3); its result still lands in the cache."""
    fut = concurrent.futures.Future()

    def run():
        try:
            res = fetch() if fetch else CU.usage_for(envd, timeout)
        except Exception:  # noqa: BLE001 — usage_for never raises; belt and braces
            res = {"error": "network"}
        with _api_lock:
            # an ABANDONED request (replaced after STUCK_S) neither clears nor overwrites the newer
            # request's entry / result (Codex usage R2 #1, R3 #1)
            if (_inflight.get(key) or (None,))[0] is fut:
                _inflight.pop(key, None)
                _api_cache[key] = (asked_at, res)
        fut.set_result(res)
    threading.Thread(target=run, name=f"usage-{key[0]}", daemon=True).start()
    return fut


def read_api_usage(cfg: dict, timeout: float = CU.TIMEOUT_S, now: float = None, wait_s: float = API_WAIT_S) -> dict:
    """Direct usage of every claude account (integrations.claude_usage): cached per account identity,
    asked in parallel, at most ONE request per account in flight across threads."""
    now = now or time.time()
    accts = ((cfg or {}).get("accounts") or {}).get("claude") or {}
    out, waiting, read_at = {}, {}, {}
    with _api_lock:
        for name, a in accts.items():
            envd = (a or {}).get("env") or {}
            fetch, max_ok = None, API_CACHE_S
            if ((a or {}).get("env_files") or {}).get("CLAUDE_CODE_OAUTH_TOKEN"):
                # runs on a long-lived `setup-token` token: the usage endpoint refuses it (403, scope
                # user:profile) and its CLAUDE_CONFIG_DIR login is the dead one it replaced — read the
                # rate-limit headers of one tiny turn instead (CU.probe), at most every TOKEN_PROBE_S
                tok = agent_runner._account_env(cfg, "claude", name).get("CLAUDE_CODE_OAUTH_TOKEN")
                if not tok:
                    out[f"claude@{name}"] = {"error": "token_unreadable"}
                    continue
                import hashlib
                key = (name, "token", hashlib.sha256(tok.encode("utf-8")).hexdigest()[:16])
                fetch, max_ok = (lambda t=tok: CU.probe(t, timeout)), token_probe_s()
            else:
                key = _key(name, envd)
            hit = _api_cache.get(key)
            # a transient miss (network / 429 / 5xx…) is only reused for TRANSIENT_MAX_AGE_S (R2 #1)
            max_age = A.TRANSIENT_MAX_AGE_S if (hit and hit[1].get("error") in A.TRANSIENT_ERRORS) else max_ok
            if hit and now - hit[0] < max_age:
                out[f"claude@{name}"] = hit[1]
                read_at[f"claude@{name}"] = hit[0]          # when it was READ, not now
                continue
            entry = _inflight.get(key)
            if entry is None or time.monotonic() - entry[1] > STUCK_S:
                entry = _inflight[key] = (_start(key, envd, timeout, now, fetch), time.monotonic())
            waiting[f"claude@{name}"] = entry[0]
    deadline = time.monotonic() + wait_s
    for label, fut in waiting.items():
        try:
            out[label] = fut.result(timeout=max(0.0, deadline - time.monotonic()))
        except concurrent.futures.TimeoutError:
            out[label] = {"error": "network"}              # still running: the next call picks it up
    if out:                                                # numbers only: agent_runner balances on them (F.22)
        A.save_usage(usage_cache_path(), out, now, ids=account_ids(cfg), read_at=read_at)
    return out


def token_probe_s() -> int:
    """How often a token account's usage is re-read through one tiny turn (ACCOUNT_TOKEN_PROBE_S, default
    900 s = ~96 one-token haiku turns a day per account, per process that looks)."""
    try:
        return max(120, int(agent_runner.config.get("ACCOUNT_TOKEN_PROBE_S", "900") or 900))
    except ValueError:
        return 900


def account_ids(cfg: dict) -> dict:
    """{"claude@name": non-secret fingerprint of (credentials dir, usage endpoint)} — the same identity
    the in-process cache uses (_key): a re-pointed account never inherits another's numbers."""
    import hashlib
    accts = ((cfg or {}).get("accounts") or {}).get("claude") or {}
    out = {}
    for name, a in accts.items():
        k = _key(name, (a or {}).get("env") or {})
        tok = agent_runner._account_env(cfg, "claude", name).get("CLAUDE_CODE_OAUTH_TOKEN")             if ((a or {}).get("env_files") or {}).get("CLAUDE_CODE_OAUTH_TOKEN") else None
        k = ("token", hashlib.sha256(tok.encode("utf-8")).hexdigest()[:16]) if tok else k[1:]
        out[f"claude@{name}"] = hashlib.sha256("|".join(k).encode("utf-8")).hexdigest()[:16]
    return out


def check_token_owners(cfg: dict, orca: dict, api: dict, rows: list) -> None:
    """A token is someone's: its answers carry `anthropic-organization-id`, which Orca knows per account
    (organizationUuid; systemDefaultOrganizationUuid for the machine's own login). Warn when an account's
    token belongs to ANOTHER organization than its own (24/11: chungtv8 held a copy of dangnd4's token).
    Several accounts can share one organization (dungpd15 + chungtv8 both "FPT IS Premium 2"), so a
    matching organization proves nothing and is never flagged. Silent when it cannot tell."""
    claude = (orca or {}).get("claude") or {}
    org_of_orca_id, emails = {}, {}
    for a in claude.get("accounts") or []:
        if isinstance(a, dict) and a.get("organizationUuid"):
            org_of_orca_id[a.get("id")] = a["organizationUuid"]
            emails.setdefault(a["organizationUuid"], []).append(a.get("email") or a.get("id"))
    if claude.get("systemDefaultOrganizationUuid"):
        org_of_orca_id[A.SYSTEM_DEFAULT] = claude["systemDefaultOrganizationUuid"]
        emails.setdefault(claude["systemDefaultOrganizationUuid"], []).append(claude.get("systemDefaultEmail") or "~/.claude")
    accts = ((cfg.get("accounts") or {}).get("claude") or {})
    names_of_org = {}
    for n, v in accts.items():
        o = org_of_orca_id.get((v or {}).get("orca_id"))
        if o:
            names_of_org.setdefault(o, []).append(n)
    for r in rows:
        org = ((api or {}).get(r["account"]) or {}).get("org")
        if r.get("backend") != "claude" or not org:
            continue
        r["token_org"] = org
        expected = org_of_orca_id.get((accts.get(r["name"]) or {}).get("orca_id"))
        owners = names_of_org.get(org) or []
        if (expected and org != expected) or (not expected and owners and r["name"] not in owners):
            who = " / ".join(owners or emails.get(org) or ["tổ chức " + org[:8]])
            r["warning"] = (r.get("warning") + "; " if r.get("warning") else "") +                 f"token này thuộc {who}, KHÔNG phải {r['name']} — tạo lại: claude_token setup {r['name']}"


TOKEN_LIFETIME_DAYS = 365      # [Unverified] `claude setup-token` = a ~1-year token; the token does not say


def login_state(cfg: dict, backend: str, name: str, now: float = None) -> dict:
    """How long this account's auth lasts. token: saved-file date + TOKEN_LIFETIME_DAYS (an ESTIMATE);
    login: the refresh-token expiry Claude CLI writes in .credentials.json (exact), + ended if blanked."""
    if backend != "claude":
        return {}
    a = (((cfg.get("accounts") or {}).get("claude") or {}).get(name) or {})
    ref = (a.get("env_files") or {}).get("CLAUDE_CODE_OAUTH_TOKEN")
    if ref:
        from agent_skill_toolkit.core import paths
        ref = str(ref)
        path = os.path.join(paths.work_dir(), ref[len("work:"):]) if ref.startswith("work:") else os.path.expanduser(ref)
        try:
            created = os.path.getmtime(path)
        except OSError:
            return {"mode": "token", "error": "không đọc được file token"}
        return {"mode": "token", "created": created, "expires_at": created + TOKEN_LIFETIME_DAYS * 86400,
                "estimated": True}
    info = CU.login_info(CU.config_dir(a.get("env") or {}))
    return {"mode": "login", **info} if info else {}


def usage_cache_path() -> str:
    from agent_skill_toolkit.core import paths
    return paths.state_path(A.USAGE_CACHE)


def runs_today_by_account(turns: list = None) -> dict:
    """{"claude@name": turns since local midnight} from the usage table (model = backend:model@account)."""
    out = {}
    for t in (read_turns() if turns is None else turns):
        m = str(t.get("model") or "")
        if "@" in m:
            backend = m.split(":", 1)[0].split("@", 1)[0]
            label = f"{backend}@{m.rsplit('@', 1)[1]}"
            out[label] = out.get(label, 0) + 1
    return out


def gather(orca_timeout: int = orca_adapter.DEFAULT_TIMEOUT, api: bool = True) -> dict:
    try:
        orca, err = orca_adapter.account_list(timeout=orca_timeout), None
    except (orca_adapter.OrcaError, OSError) as e:
        orca, err = None, getattr(e, "message", str(e))
    cfg = agent_runner._agents_config()
    api_res = read_api_usage(cfg) if api else None
    out = A.report(cfg, orca, read_slots(agent_runner.slots_dir()), read_turns(),
                   stale_s=agent_runner.SLOT_STALE_S, api=api_res)
    check_token_owners(cfg, orca, api_res or {}, out["accounts"])
    ids, seen = account_ids(cfg), {}
    for r in out["accounts"]:
        r["routing"] = routing_state(cfg, r["backend"], r["name"])
        r["login"] = login_state(cfg, r["backend"], r["name"])
        if r["login"].get("mode") == "token" and ids.get(r["account"]):
            seen.setdefault(ids[r["account"]], []).append(r)
    for same in seen.values():          # one token saved under several names = ONE Claude account (seen 24/11)
        if len(same) > 1:
            for r in same:
                others = ", ".join(x["name"] for x in same if x is not r)
                r["warning"] = (r.get("warning") + "; " if r.get("warning") else "") +                     f"dùng CHUNG token với {others} — thực ra là cùng một tài khoản Claude (dung lượng và slot dùng chung)"
    if err:
        out["orca_error"] = err
    par = {}
    for backend, total in (cfg.get("parallel") or {}).items():
        live = agent_runner.active_accounts(cfg, backend)
        par[backend] = {"total": total, "active": len(live), "per_account": agent_runner.account_limit(cfg, backend, "")}
    if par:
        out["parallel"] = par
    for r in out["accounts"]:
        r["max_parallel"] = agent_runner.account_limit(cfg, r["backend"], r["name"])
    hb = _read_json(heartbeat_path())
    if hb.get("at"):
        out["heartbeat"] = {"at": hb["at"], "interval_s": heartbeat_interval_s()}
    return out


# ----------------------------------------------------------------------------- heartbeat
# Without it the accounts were only read when someone LOOKED (dashboard, accounts.py) or an agent was
# about to run — a login that died at night showed up the next morning as "every account expired".
# The Coordinator calls heartbeat() on every tick; it self-throttles (one check per interval across
# processes), costs no model turn (usage endpoint + local files only) and logs every state CHANGE
# to temp/daemon_health.jsonl (label "accounts", see `daemon_common.py status`).

def heartbeat_path() -> str:
    from agent_skill_toolkit.core import paths
    return paths.state_path("account_heartbeat.json")


def heartbeat_interval_s() -> int:
    try:
        return max(60, int(agent_runner.config.get("ACCOUNT_HEARTBEAT_S", "300") or 300))
    except ValueError:
        return 300


def _read_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def account_state(row: dict) -> dict:
    """One account's heartbeat verdict: ok | limited | dead, + the numbers worth comparing."""
    rt = row.get("routing") or {}
    u = row.get("usage") or {}
    lefts = [b.get("left") for b in (u.get("session"), u.get("weekly")) if isinstance(b, dict) and b.get("left") is not None]
    state = "dead" if rt.get("state") == "skipped" else "limited" if rt.get("limited") else "ok"
    return {"state": state, "reason": rt.get("reason") or row.get("api_error") or "",
            "left": min(lefts) if lefts else None}


def heartbeat(force: bool = False, now: float = None) -> dict:
    """Check every account if the last check is older than ACCOUNT_HEARTBEAT_S (default 300 s)."""
    now = now or time.time()
    path = heartbeat_path()
    prev = _read_json(path)
    if not force and now - float(prev.get("at") or 0) < heartbeat_interval_s():
        return {"heartbeat": "skipped", "next_in_s": round(heartbeat_interval_s() - (now - float(prev["at"])))}
    rep = gather()
    states = {r["account"]: account_state(r) for r in rep.get("accounts") or []}
    changes = []
    for label, s in states.items():
        old = (prev.get("accounts") or {}).get(label) or {}
        if old.get("state") != s["state"]:
            changes.append(f"{label}: {old.get('state') or 'mới'} → {s['state']}" + (f" ({s['reason'][:120]})" if s["reason"] else ""))
    try:
        import daemon_common
        for c in changes:
            daemon_common.health_log("accounts", "changed", c)
        daemon_common.heartbeat("accounts", ok=sum(s["state"] == "ok" for s in states.values()), total=len(states))
    except Exception:  # noqa: BLE001 — the log is a convenience, the check itself already happened
        pass
    tmp = path + f".{os.getpid()}.tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"at": now, "accounts": states}, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        pass
    return {"heartbeat": "done", "ok": sum(s["state"] == "ok" for s in states.values()), "total": len(states),
            "changes": changes}


def routing_state(cfg: dict, backend: str, name: str) -> dict:
    """What agent_runner.run_role will do with this account right now: {"state": "ok"} |
    {"state": "skipped", "reason"} (dead login, unchanged since) | plus "limited": {model: until}."""
    env_extra = agent_runner._account_env(cfg, backend, name)
    key = f"{backend}@{name}"
    fp = agent_runner.auth_fingerprint(backend, env_extra)
    bad = agent_runner.auth_bad(key, fp)
    reason = str(bad.get("message") or "") if bad else agent_runner.login_ended(backend, env_extra)
    reason = re.sub(r"^CLI '[^']*' exit \d+:\s*", "", reason).split(" Run `", 1)[0]
    out = {"state": "skipped", "reason": reason[:160]} if reason else {"state": "ok"}
    if (((((cfg.get("accounts") or {}).get(backend) or {}).get(name) or {}).get("env_files") or {})
            .get("CLAUDE_CODE_OAUTH_TOKEN")):
        out["auth"] = "token"
    limited = {}
    for spec in sum((v if isinstance(v, list) else [v] for v in (cfg.get("roles") or {}).values()), []):
        c = agent_runner.parse_candidate(str(spec))
        if c["backend"] == backend and (c["account"] or "default") == name:
            until = agent_runner.limited_until(f"{key}:{c['model'] or ''}")
            if until:
                limited[c["model"] or "default"] = until
    if limited:
        out["limited"] = limited
    return out


def _pct(b):
    if not b:
        return "?"
    return "reset?" if b.get("reset_passed") else f"{b['left']:.0f}%"


def _reset(u):
    ts = [b["resets_at"] for b in ((u or {}).get("session"), (u or {}).get("weekly"))
          if b and b.get("resets_at") and not b.get("reset_passed")]
    return time.strftime("%a %H:%M", time.localtime(min(ts))) if ts else ""


def render(rep: dict) -> str:
    lines = ["Tài khoản            Email                  5h còn  Tuần còn  Reset sớm   Cập nhật         Hôm nay   Đang làm",
             "-" * 124]
    for r in rep["accounts"]:
        u = r.get("usage")
        src = {"live": "Orca trực tiếp", "api": "API " + (f"{u['age_min']}p trước" if (u or {}).get("age_min") else "vừa đọc"),
               "cached": f"Orca {(u or {}).get('age_min')}p trước", "none": "—"}[r["usage_source"]]
        doing = "; ".join(f"{x['role'] or '?'} {x['task_id'] or ''} {x['step'] or ''} ({x['minutes']}p)".replace("  ", " ")
                          for x in r["running"]) or "rảnh"
        today = f"{r['today']['runs']} lượt"
        name = r["account"] + (" *" if r.get("orca_active") else "")
        lines.append(f"{name:<20} {(r.get('email') or '')[:22]:<22} {_pct((u or {}).get('session')):>6}  "
                     f"{_pct((u or {}).get('weekly')):>8}  {_reset(u):<10}  {src:<15}  {today:<8}  {doing}")
        if r.get("warning"):
            lines.append(f"{'':<20} ! {r['warning']}")
        if r.get("api_error") and r["usage_source"] != "api":
            lines.append(f"{'':<20} ! đọc trực tiếp không được: {r['api_error']}")
        lg = r.get("login") or {}
        if lg.get("expires_at"):
            when = time.strftime("%d/%m/%Y", time.localtime(lg["expires_at"]))
            if lg.get("mode") == "token":
                lines.append(f"{'':<20} 🔑 token dài hạn, hạn ~{when} (ước tính: lưu {time.strftime('%d/%m/%Y', time.localtime(lg['created']))} + 1 năm)")
            else:
                gone = lg.get("ended") or lg["expires_at"] < time.time()
                lines.append(f"{'':<20} 🔑 đăng nhập {'ĐÃ HẾT HẠN' if gone else 'hết hạn'} {when}")
        rt = r.get("routing") or {}
        if rt.get("state") == "skipped":
            lines.append(f"{'':<20} ✗ BỎ QUA khi chạy agent: {rt.get('reason')}")
        for model, until in (rt.get("limited") or {}).items():
            lines.append(f"{'':<20} ⏸ {model} hết lượt tới {time.strftime('%a %H:%M', time.localtime(until))}")
    if rep.get("unattributed"):
        o = rep["unattributed"]
        lines.append(f"{o['account']:<20} {'':<22} {'':>6}  {'':>8}  {'':<10}  {'':<15}  {o['today']['runs']} lượt"
                     f"{'':<3}  {len(o['running'])} đang chạy")
    for b, p in (rep.get("parallel") or {}).items():
        lines.append(f"Song song {b}: tổng {p['total']} phiên / {p['active']} tài khoản sống → tối đa {p['per_account']} phiên mỗi tài khoản")
    lines.append("")
    lines.append("* = tài khoản Orca đang chọn (số liệu trực tiếp). Tài khoản khác: số Orca đọc lần cuối.")
    lines.append("reset? = đã qua giờ reset sau lần đọc đó — số mới chưa biết. Chọn tài khoản đó trong Orca để cập nhật.")
    if rep.get("orca") != "ok":
        lines.append(f"Orca không đọc được ({rep.get('orca_error', '')}) — không có số dung lượng.")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--orca-only", action="store_true", help="do not ask Anthropic directly (Orca's numbers only)")
    ap.add_argument("--heartbeat", action="store_true", help="run the account check now (as the Coordinator does) "
                                                           "and log state changes")
    a = ap.parse_args(argv)
    if a.heartbeat:
        print(json.dumps(heartbeat(force=True), ensure_ascii=False, indent=2))
        return 0
    rep = gather(api=not a.orca_only)
    if a.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        print(render(rep))
    return 0


if __name__ == "__main__":
    sys.exit(main())
