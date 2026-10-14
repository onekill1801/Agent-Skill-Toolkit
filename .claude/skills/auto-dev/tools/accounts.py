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
import json
import os
import runpy
import sqlite3
import sys
import time
from pathlib import Path

runpy.run_path(str(Path(__file__).resolve().parents[2] / "_runtime" / "bootstrap.py"))["activate"]()
from agent_skill_toolkit.core import store as store_mod  # noqa: E402
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


def gather(orca_timeout: int = orca_adapter.DEFAULT_TIMEOUT) -> dict:
    try:
        orca, err = orca_adapter.account_list(timeout=orca_timeout), None
    except (orca_adapter.OrcaError, OSError) as e:
        orca, err = None, getattr(e, "message", str(e))
    out = A.report(agent_runner._agents_config(), orca, read_slots(agent_runner.slots_dir()), read_turns(),
                   stale_s=agent_runner.SLOT_STALE_S)
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
        src = {"live": "trực tiếp", "cached": f"{(u or {}).get('age_min')} phút trước", "none": "—"}[r["usage_source"]]
        doing = "; ".join(f"{x['role'] or '?'} {x['task_id'] or ''} {x['step'] or ''} ({x['minutes']}p)".replace("  ", " ")
                          for x in r["running"]) or "rảnh"
        today = f"{r['today']['runs']} lượt"
        name = r["account"] + (" *" if r.get("orca_active") else "")
        lines.append(f"{name:<20} {(r.get('email') or '')[:22]:<22} {_pct((u or {}).get('session')):>6}  "
                     f"{_pct((u or {}).get('weekly')):>8}  {_reset(u):<10}  {src:<15}  {today:<8}  {doing}")
        if r.get("warning"):
            lines.append(f"{'':<20} ! {r['warning']}")
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
    a = ap.parse_args(argv)
    rep = gather()
    if a.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        print(render(rep))
    return 0


if __name__ == "__main__":
    sys.exit(main())
