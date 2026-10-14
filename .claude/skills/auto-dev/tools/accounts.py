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


def _start(key, envd, timeout, asked_at) -> concurrent.futures.Future:
    """One daemon thread per fetch: nothing ever joins it, so a stuck request can not block the
    caller (the dashboard's single worker) past API_WAIT_S (R1 #3); its result still lands in the cache."""
    fut = concurrent.futures.Future()

    def run():
        try:
            res = CU.usage_for(envd, timeout)
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
    out, waiting = {}, {}
    with _api_lock:
        for name, a in accts.items():
            envd = (a or {}).get("env") or {}
            key = _key(name, envd)
            hit = _api_cache.get(key)
            if hit and now - hit[0] < API_CACHE_S:
                out[f"claude@{name}"] = hit[1]
                continue
            entry = _inflight.get(key)
            if entry is None or time.monotonic() - entry[1] > STUCK_S:
                entry = _inflight[key] = (_start(key, envd, timeout, now), time.monotonic())
            waiting[f"claude@{name}"] = entry[0]
    deadline = time.monotonic() + wait_s
    for label, fut in waiting.items():
        try:
            out[label] = fut.result(timeout=max(0.0, deadline - time.monotonic()))
        except concurrent.futures.TimeoutError:
            out[label] = {"error": "network"}              # still running: the next call picks it up
    return out


def gather(orca_timeout: int = orca_adapter.DEFAULT_TIMEOUT, api: bool = True) -> dict:
    try:
        orca, err = orca_adapter.account_list(timeout=orca_timeout), None
    except (orca_adapter.OrcaError, OSError) as e:
        orca, err = None, getattr(e, "message", str(e))
    cfg = agent_runner._agents_config()
    out = A.report(cfg, orca, read_slots(agent_runner.slots_dir()), read_turns(),
                   stale_s=agent_runner.SLOT_STALE_S, api=read_api_usage(cfg) if api else None)
    if err:
        out["orca_error"] = err
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
    if rep.get("unattributed"):
        o = rep["unattributed"]
        lines.append(f"{o['account']:<20} {'':<22} {'':>6}  {'':>8}  {'':<10}  {'':<15}  {o['today']['runs']} lượt"
                     f"{'':<3}  {len(o['running'])} đang chạy")
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
    a = ap.parse_args(argv)
    rep = gather(api=not a.orca_only)
    if a.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        print(render(rep))
    return 0


if __name__ == "__main__":
    sys.exit(main())
