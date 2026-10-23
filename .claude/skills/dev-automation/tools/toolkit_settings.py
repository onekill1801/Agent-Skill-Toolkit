#!/usr/bin/env python3
"""Toolkit settings — the same catalog the dashboard edits (agent_skill_toolkit.workflows.settings).

    python toolkit_settings.py list [--json]                 # every editable setting: value, default, meaning
    python toolkit_settings.py set KEY VALUE [KEY VALUE ...]  # [WRITE] .env / store policy (hook asks)

Only non-secret switches are editable; tokens/passwords are shown as "đã đặt / chưa đặt", never read out.
After a .env change the output names the daemon tabs to restart (they keep what they loaded at start).
"""

import argparse
import json
import os
import runpy
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
runpy.run_path(os.path.join(os.path.dirname(os.path.dirname(_HERE)), "_runtime", "bootstrap.py"))["activate"]()
from agent_skill_toolkit.core import store as store_mod  # noqa: E402
from agent_skill_toolkit.workflows import settings as S  # noqa: E402

sys.path.insert(0, _HERE)
import probe_common as pc  # noqa: E402  (UTF-8 stdout on Windows)


def _snapshot() -> dict:
    try:
        st = store_mod.Store.reader()
    except FileNotFoundError:
        st = None
    try:
        return S.snapshot(st)
    finally:
        if st is not None:
            st.close()


def render(snap: dict) -> str:
    lines, group = [f".env: {snap['env_file']}" + ("" if snap["env_exists"] else "  (CHƯA CÓ)")], None
    for it in snap["items"]:
        if it["group"] != group:
            group = it["group"]
            lines += ["", f"== {group}"]
        val = it["value"] if it["set"] and it["value"] != "" else f"(mặc định: {it['default'] or 'trống'})"
        lines.append(f"  {it['key']:<28} = {val}")
        lines.append(f"  {'':<28}   {it['label']}" + (f" — {it['help']}" if it["help"] else ""))
    missing = [s["key"] for s in snap["secrets"] if not s["set"]]
    lines += ["", f"Khoá bí mật: {sum(s['set'] for s in snap['secrets'])}/{len(snap['secrets'])} đã đặt"
              + (f" · chưa đặt: {', '.join(missing)}" if missing else "")]
    lines += [f"! {w}" for w in snap["warnings"]]
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("list")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("set")
    p.add_argument("pairs", nargs="+", metavar="KEY VALUE")
    a = ap.parse_args(argv)
    if a.cmd == "list":
        snap = _snapshot()
        print(json.dumps(snap, ensure_ascii=False, indent=2) if a.json else render(snap))
        return 0
    if len(a.pairs) % 2:
        return pc.emit({"error": True, "message": "cần cặp KEY VALUE"})
    changes = dict(zip(a.pairs[::2], a.pairs[1::2]))
    try:
        out = S.apply(changes, by="cli")
    except (ValueError, RuntimeError, FileNotFoundError, TimeoutError) as e:
        return pc.emit({"error": True, "message": str(e)})
    if out["restart"]:
        out["note"] = "Restart các tab: " + ", ".join(out["restart"])
    return pc.emit(out)


if __name__ == "__main__":
    sys.exit(main())
