#!/usr/bin/env python3
"""Toolkit status dashboard — one consolidated read-only view of all moving parts.

State is scattered across temp/ and work/ (daemon health, pipeline runs, the intake
queue, pending Telegram approvals, the feedback ledger). This aggregates them into a
single glance so you can see what is running, stuck, or waiting for you — without
running five different commands.

Read-only. Never writes. Stdlib only. Works even when a section has no data yet.

    python status.py            # human-readable report
    python status.py --json     # machine-readable (for scripts / the Telegram /status)
    python status.py --section daemons,runs   # only some sections

Sections: daemons, runs, queue, approvals, feedback.
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

_HERE = os.path.dirname(os.path.abspath(__file__))


def _repo_root():
    search = _HERE
    for _ in range(6):
        if os.path.isdir(os.path.join(search, ".git")) or \
           os.path.isfile(os.path.join(search, "CLAUDE.md")):
            return search
        search = os.path.dirname(search)
    return _HERE


def _work_dir():
    return os.environ.get("WORK_DIR") or os.path.join(_repo_root(), "work")


def _temp(*parts):
    return os.path.join(_repo_root(), "temp", *parts)


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _iter_json_dir(path):
    if not os.path.isdir(path):
        return
    for fn in sorted(os.listdir(path)):
        if fn.endswith(".json"):
            data = _read_json(os.path.join(path, fn))
            if data is not None:
                yield fn, data


def _age(ts_iso):
    """Human 'age' from an ISO timestamp, or '' if unparseable."""
    try:
        t = datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return ""
    secs = (datetime.now(timezone.utc) - t).total_seconds()
    if secs < 0:
        return "just now"
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= size:
            return f"{int(secs // size)}{unit} ago"
    return f"{int(secs)}s ago"


# --- sections --------------------------------------------------------------------

def sect_daemons():
    """Last health event per daemon from temp/daemon_health.jsonl."""
    path = _temp("daemon_health.jsonl")
    last = {}
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                last[r.get("label", "?")] = r
    out = []
    for label, r in sorted(last.items()):
        out.append({"daemon": label, "event": r.get("event"),
                    "age": _age(r.get("ts", "")), "detail": r.get("detail", "")})
    return out


def sect_runs():
    """Open pipeline runs (deliver stage not done) from temp/runs/<id>.json."""
    out = []
    for _, st in _iter_json_dir(_temp("runs")):
        if not isinstance(st, dict):
            continue  # temp/runs also holds gate-result arrays / probe output, not just runs
        stages = st.get("stages")
        if not isinstance(stages, dict):
            continue  # not a run-log file (plan xml/grounding md live here too, but as non-json)
        order = ["plan", "implement", "test", "deliver"]
        current = next((s for s in order if stages.get(s) not in ("done", "skipped")), None)
        done = sum(1 for s in order if stages.get(s) == "done")
        entry = {
            "run_id": st.get("run_id"), "task_id": st.get("task_id"),
            "project": st.get("project"), "type": st.get("type"),
            "tier": st.get("tier"), "mode": st.get("mode"),
            "current_stage": current or "delivered",
            "stages_done": f"{done}/{len(order)}",
            "open": stages.get("deliver") != "done",
        }
        if any(v == "failed" for v in stages.values()):
            entry["failed"] = True
        out.append(entry)
    # open + failed first
    out.sort(key=lambda e: (not e["open"], not e.get("failed", False)))
    return out


def sect_queue():
    """Intake queue items (non-terminal first) + held flow locks."""
    qdir = os.path.join(_work_dir(), "queue")
    items = []
    for _, it in _iter_json_dir(os.path.join(qdir, "items")):
        items.append({
            "qid": it.get("qid"), "task_id": it.get("task_id") or it.get("id"),
            "name": (it.get("name") or "")[:50], "state": it.get("state"),
            "age": _age(it.get("updated") or it.get("created") or ""),
        })
    order = {"processing": 0, "ready": 1, "needs_clarification": 2, "failed": 3, "done": 4}
    items.sort(key=lambda i: order.get(i.get("state"), 9))
    locks = []
    if os.path.isdir(qdir):
        for fn, data in _iter_json_dir(qdir):
            if fn.startswith("lock_"):
                locks.append({"owner": fn[len("lock_"):-len(".json")],
                              "qid": (data or {}).get("qid"),
                              "age": _age((data or {}).get("ts") or (data or {}).get("since") or "")})
    return {"items": items, "locks": locks}


def sect_approvals():
    """Pending Telegram approval requests (temp/tg_approvals/<id>.json)."""
    out = []
    for _, r in _iter_json_dir(_temp("tg_approvals")):
        # Only well-formed pending requests (id + tool); skip legacy/other-shaped files.
        if r.get("status") == "pending" and r.get("id") and r.get("tool"):
            out.append({
                "id": r.get("id"), "tool": r.get("tool"), "risk": r.get("risk"),
                "summary": (r.get("summary") or "")[:60],
                "age": _age_epoch(r.get("created")),
                "_created": r.get("created") or 0,
            })
    out.sort(key=lambda a: a["_created"], reverse=True)  # newest first
    return out


def _age_epoch(epoch):
    if not epoch:
        return ""
    try:
        secs = time.time() - float(epoch)
    except (TypeError, ValueError):
        return ""
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= size:
            return f"{int(secs // size)}{unit} ago"
    return f"{int(secs)}s ago"


def sect_feedback():
    """Per-project feedback ledger: record count + straight-through rate by stage."""
    fdir = os.path.join(_work_dir(), "feedback")
    out = []
    if not os.path.isdir(fdir):
        return out
    for fn in sorted(os.listdir(fdir)):
        if not fn.endswith(".jsonl"):
            continue
        project = fn[:-len(".jsonl")]
        total, clean, corrections = 0, 0, 0
        with open(os.path.join(fdir, fn), encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                total += 1
                if r.get("human_action") == "approved" and not (r.get("correction") or "").strip():
                    clean += 1
                if r.get("human_action") in ("edited", "rejected", "overridden"):
                    corrections += 1
        out.append({"project": project, "records": total, "corrections": corrections,
                    "straight_through_rate": round(clean / total, 2) if total else None})
    return out


_ALL = {"daemons": sect_daemons, "runs": sect_runs, "queue": sect_queue,
        "approvals": sect_approvals, "feedback": sect_feedback}


# --- rendering -------------------------------------------------------------------

CAP = 12  # max rows per section in the human view (--json always has everything)


def _errline(section):
    return isinstance(section, dict) and "error" in section


def _render(data):
    lines = ["=" * 60, "TOOLKIT STATUS", "=" * 60]

    if "daemons" in data:
        lines += ["", "## Daemons"]
        ds = data["daemons"]
        if _errline(ds):
            lines.append(f"  (error: {ds['error']})")
            ds = []
        if not ds:
            lines.append("  (no daemon has logged health yet)")
        for d in ds:
            flag = {"fatal": "🛑", "transient": "⚠️", "stopped": "⏹",
                    "recovered": "✅", "connected": "✅", "started": "▶"}.get(d["event"], "·")
            lines.append(f"  {flag} {d['daemon']:<16} {d['event']:<11} {d['age']:<10} {d['detail'][:60]}")

    if "runs" in data:
        lines += ["", "## Pipeline runs"]
        rs = data["runs"]
        if _errline(rs):
            lines.append(f"  (error: {rs['error']})")
            rs = []
        open_runs = [r for r in rs if r["open"]]
        if not rs:
            lines.append("  (no runs)")
        elif not open_runs:
            lines.append(f"  all {len(rs)} run(s) delivered ✅")
        else:
            lines.append(f"  {len(open_runs)} open of {len(rs)} run(s):")
        for r in open_runs[:CAP]:
            fail = " FAILED" if r.get("failed") else ""
            lines.append(f"  • {r['run_id']:<22} [{r.get('tier')}/{r.get('mode')}] "
                         f"@ {r['current_stage']} ({r['stages_done']}){fail}")
        if len(open_runs) > CAP:
            lines.append(f"  … and {len(open_runs) - CAP} more (--json for all)")

    if "queue" in data:
        lines += ["", "## Intake queue"]
        q = data["queue"]
        if not isinstance(q, dict) or "items" not in q:
            q = {"items": [], "locks": []}
        active = [i for i in q["items"] if i.get("state") not in ("done", "failed")]
        if not q["items"]:
            lines.append("  (queue empty)")
        for i in active:
            lines.append(f"  • {str(i['qid'])[:10]:<10} {i.get('state'):<20} "
                         f"{i.get('task_id') or '':<12} {i['name']}")
        for lk in q["locks"]:
            lines.append(f"  🔒 lock held by '{lk['owner']}' (qid={lk.get('qid')}) {lk['age']}")

    if "approvals" in data:
        lines += ["", "## Pending approvals"]
        ap = data["approvals"]
        if _errline(ap):
            lines.append(f"  (error: {ap['error']})")
            ap = []
        if not ap:
            lines.append("  (none waiting)")
        else:
            lines.append(f"  {len(ap)} waiting:")
        for a in ap[:CAP]:
            lines.append(f"  ⏳ {a['id']} [{a.get('risk')}] {a.get('tool')}: {a['summary']} ({a['age']})")
        if len(ap) > CAP:
            lines.append(f"  … and {len(ap) - CAP} more (--json for all)")

    if "feedback" in data:
        lines += ["", "## Feedback ledger"]
        fb = data["feedback"]
        if _errline(fb):
            lines.append(f"  (error: {fb['error']})")
            fb = []
        if not fb:
            lines.append("  (no feedback recorded yet)")
        for p in fb:
            rate = p["straight_through_rate"]
            rate_s = f"{int(rate * 100)}% straight-through" if rate is not None else "—"
            lines.append(f"  {p['project']:<16} {p['records']} rec · "
                         f"{p['corrections']} corrections · {rate_s}")

    lines.append("")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="Consolidated toolkit status (read-only).")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--section", default=None,
                    help="comma list: daemons,runs,queue,approvals,feedback (default all)")
    args = ap.parse_args()

    want = ([s.strip() for s in args.section.split(",") if s.strip()]
            if args.section else list(_ALL))
    data = {}
    for name in want:
        fn = _ALL.get(name)
        if fn:
            try:
                data[name] = fn()
            except Exception as e:  # noqa: BLE001 — a broken section must not blank the dashboard
                data[name] = {"error": str(e)}

    if args.json:
        print(json.dumps({"ok": True, "sections": data}, ensure_ascii=False, indent=2))
    else:
        print(_render(data))
    return 0


if __name__ == "__main__":
    sys.exit(main())
