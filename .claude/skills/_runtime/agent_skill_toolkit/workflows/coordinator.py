"""Coordinator (design §5.2, checklist F.7): one deterministic loop — not an LLM — that runs every
kind of queued work with shared limits, and a progress board over the store.

    c = Coordinator(registry, parallel=2, hooks=[review_flow.reconcile])
    c.run(once=True)            # N worker threads drain the queue, then return
    board(st)                   # rows for the progress board (grouped by parent)

What the store guarantees (core/store.py) and this loop relies on:
  * a task's dependencies must be DONE before it is claimed; failed / cancelled dependency ->
    the dependent is BLOCKED (block_orphans), never left waiting forever
  * `resource` = exclusive key: two tasks on the same repo/branch never run at once
  * `max_running` = global cap across all kinds and processes (other `work` loops included)
  * quota pause / resume, leases, checkpoints: unchanged (engine.run)
"""

import threading
import time

from agent_skill_toolkit.core import store as store_mod
from agent_skill_toolkit.workflows import engine as E

ICON = {"queued": "⏳", "running": "🔄", "paused_quota": "⏸", "blocked": "✋", "done": "✅",
        "failed": "❌", "cancelled": "🚫"}


class Coordinator:
    def __init__(self, registry: dict, parallel: int = 2, owner: str = "coordinator", hooks=(),
                 lease_s: int = store_mod.DEFAULT_LEASE_S, store_path: str = ""):
        self.registry, self.parallel, self.owner = registry, max(1, int(parallel)), owner
        self.hooks, self.lease_s, self.store_path = list(hooks), lease_s, store_path
        self.results, self._lock = [], threading.Lock()

    def tick(self, st) -> dict:
        """Housekeeping before claiming: orphaned dependents, then each workflow's reconcile hook
        (e.g. review tasks whose human decision arrived). A failing hook never stops the loop."""
        out = {"blocked_orphans": st.block_orphans(), "hooks": []}
        for h in self.hooks:
            try:
                out["hooks"].append(h())
            except Exception as e:  # noqa: BLE001
                out["hooks"].append({"error": f"{getattr(h, '__name__', 'hook')}: {type(e).__name__}: {e}"})
        return out

    def _worker(self, i: int, stop: threading.Event, once: bool, interval: float):
        with store_mod.Store(self.store_path) as st:
            while not stop.is_set():
                res = E.work_once(st, f"{self.owner}#{i}", self.registry, lease_s=self.lease_s,
                                  max_running=self.parallel)
                if res.get("status") != "idle":
                    with self._lock:
                        self.results.append(res)
                    continue
                if once:
                    return
                stop.wait(interval)

    def _pass(self, stop, once, interval):
        threads = [threading.Thread(target=self._worker, args=(i, stop, once, interval), daemon=True)
                   for i in range(self.parallel)]
        for t in threads:
            t.start()
        return threads

    def run(self, once: bool = False, interval: float = 30, on_tick=None) -> dict:
        stop = threading.Event()
        try:
            if once:
                # passes until one runs nothing new: a pass can finish a dependency and unlock the next
                while True:
                    before = len(self.results)
                    with store_mod.Store(self.store_path) as st:
                        r = self.tick(st)
                    if on_tick:
                        on_tick(r)
                    for t in self._pass(stop, True, interval):
                        t.join()
                    if len(self.results) == before:
                        return {"ran": self.results}
            with store_mod.Store(self.store_path) as st:
                r = self.tick(st)
            if on_tick:
                on_tick(r)
            self._pass(stop, False, interval)
            while True:
                time.sleep(interval)
                with store_mod.Store(self.store_path) as st:
                    r = self.tick(st)
                if on_tick:
                    on_tick(r)
        finally:
            stop.set()


# ----------------------------------------------------------------------------- progress board

def _row(st, t: dict) -> dict:
    cps = st.checkpoints(t["id"], valid_only=True)
    steps = list(dict.fromkeys(c["step"] for c in cps))
    note = ""
    if t["status"] == "paused_quota" and t.get("resume_at"):
        note = "tiếp lúc " + time.strftime("%H:%M %d/%m", time.localtime(t["resume_at"]))
    elif t["status"] in ("blocked", "failed") and t.get("last_error"):
        note = str(t["last_error"]).splitlines()[0][:120]
    deps = st.deps_of(t["id"])
    waiting = [d for d in deps if (st.get(d) or {}).get("status") != "done"]
    if waiting and t["status"] == "queued":
        note = "chờ " + ", ".join(waiting)
    return {"id": t["id"], "kind": t["kind"], "title": t.get("title") or t["ref"], "status": t["status"],
            "icon": ICON.get(t["status"], "?"), "step": t.get("step") or "", "done_steps": steps,
            "attempts": t.get("attempts"), "resource": t.get("resource") or "", "deps": deps,
            "parent": t.get("parent") or "", "note": note,
            "source": ((t.get("data") or {}).get("intake") or {}).get("source", "")}


def board(st, include_closed_hours: float = 24, limit: int = 200) -> dict:
    """Open tasks + those closed in the last N hours, grouped under their parent."""
    since = time.time() - include_closed_hours * 3600
    rows = [_row(st, t) for t in st.list(limit=limit)
            if t["status"] in store_mod.OPEN or (t.get("updated") or 0) >= since]
    by_id = {r["id"]: r for r in rows}
    roots = [r for r in rows if not r["parent"] or r["parent"] not in by_id]
    kids = {}
    for r in rows:
        if r["parent"] and r["parent"] in by_id:
            kids.setdefault(r["parent"], []).append(r)
    lines = []
    for r in sorted(roots, key=lambda x: (x["status"] in store_mod.CLOSED, x["id"])):
        ch = kids.get(r["id"], [])
        pct = f"  {round(100 * sum(c['status'] == 'done' for c in ch) / len(ch))}%" if ch else ""
        lines.append(_line(r) + pct)
        lines += ["  " + _line(c) for c in ch]
    summary = st.summary()
    head = " · ".join(f"{ICON.get(k, k)} {k} {v}" for k, v in sorted(summary["counts"].items()))
    return {"summary": summary, "rows": rows, "text": "\n".join([head or "(trống)", ""] + lines)}


def _line(r: dict) -> str:
    step = f" [{r['step']}]" if r["step"] and r["status"] in ("running", "paused_quota", "blocked") else ""
    done = f" {len(r['done_steps'])} bước xong" if r["done_steps"] else ""
    note = f" — {r['note']}" if r["note"] else ""
    return f"{r['icon']} {r['id']} {r['kind']}: {r['title'][:70]}{step}{done}{note}"
