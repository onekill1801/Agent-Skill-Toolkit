#!/usr/bin/env python3
"""Toolkit web dashboard — ONE page to watch everything the repo runs (read-only).

    python dashboard.py serve [--port 8765]     # then open http://127.0.0.1:8765 (e.g. in Orca's browser)
    python dashboard.py snapshot                # the same data once, as JSON (scripts / debugging)

Sections: overview (usage tier, capacity, daily cap) · tasks of the durable store (review_mr / verify /
proposal / refactor / atask, as `coordinator.py board`) · agent accounts (`accounts.py`: who runs what,
usage left from Orca) · MR review jobs · pending approvals · daemons · pipeline runs · intake queue ·
port-forwards · feedback.

Safety: listens on 127.0.0.1 only, answers GET only, refuses a foreign Host header (DNS rebinding),
never writes (the store is opened with Store.reader = sqlite mode=ro). It has NO action buttons —
every action stays a terminal command (approvals need a human at a TTY); the page shows the command.
No token is ever read into the page: accounts come from `orca account list` (emails / usage only).
"""

import argparse
import json
import os
import runpy
import socket
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_SKILLS = os.path.dirname(os.path.dirname(_HERE))
runpy.run_path(os.path.join(_SKILLS, "_runtime", "bootstrap.py"))["activate"]()
from agent_skill_toolkit.core import store as store_mod  # noqa: E402
from agent_skill_toolkit.workflows import coordinator as coord  # noqa: E402
from agent_skill_toolkit.workflows.execution import usage as usage_mod  # noqa: E402

sys.path.insert(0, _HERE)
import status as status_mod  # noqa: E402  (daemons / runs / queue / approvals / feedback readers)

HTML_FILE = os.path.join(_HERE, "dashboard.html")
CACHE_S = 5                 # whole snapshot: many tabs / fast refreshes never hammer the readers
ACCOUNTS_CACHE_S = 30       # `orca account list` is a subprocess: at most twice a minute
_lock = threading.Lock()
_cache = {"at": 0.0, "data": None, "accounts_at": 0.0, "accounts": None}


def _section(fn):
    """Run one reader; a failing section becomes {"error": ...} and never breaks the page."""
    t0 = time.time()
    try:
        return {"ok": True, "data": fn(), "ms": round((time.time() - t0) * 1000)}
    except Exception as e:  # noqa: BLE001 — a dashboard shows the error instead of dying
        return {"ok": False, "error": f"{type(e).__name__}: {e}"[:300],
                "trace": traceback.format_exc(limit=2)[-600:], "ms": round((time.time() - t0) * 1000)}


def read_store() -> dict:
    """Board + settings through a READ-ONLY connection (no schema setup / write lock while agents run)."""
    try:
        st = store_mod.Store.reader()
    except FileNotFoundError:
        return {"exists": False, "rows": [], "summary": {"counts": {}}, "settings": {}}
    try:
        b = coord.board(st)
        today = usage_mod.agent_usage(st, since=usage_mod.local_midnight())
        settings = {"capacity": st.capacity(),
                    "daily_agent_runs": int(st.get_setting("daily_agent_runs") or 0),
                    "agent_runs_today": today["runs"], "agent_seconds_today": round(today["seconds"])}
        return {"exists": True, "rows": b["rows"], "summary": b["summary"], "settings": settings}
    finally:
        st.close()


def read_accounts() -> dict:
    now = time.time()
    if _cache["accounts"] is not None and now - _cache["accounts_at"] < ACCOUNTS_CACHE_S:
        return _cache["accounts"]
    sys.path.insert(0, os.path.join(_SKILLS, "auto-dev", "tools"))
    import accounts as accounts_tool                      # auto-dev/tools/accounts.py (facade)
    data = accounts_tool.gather()
    _cache.update(accounts=data, accounts_at=now)
    return data


def read_reviews() -> dict:
    # same place as review_common.temp_dir("jobs"), computed WITHOUT its makedirs
    jobs_dir = os.path.join(os.environ.get("REVIEW_STATE_DIR")
                            or os.path.join(status_mod._repo_root(), "temp", "review_queue"), "jobs")
    out = []
    if os.path.isdir(jobs_dir):
        for name in sorted(os.listdir(jobs_dir)):
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(jobs_dir, name), encoding="utf-8") as f:
                    j = json.load(f)
            except (OSError, ValueError):
                continue
            out.append({"id": j.get("id"), "status": j.get("status"),
                        "mr": f"{j.get('project_path') or j.get('project')}!{j.get('iid')}",
                        "title": (j.get("title") or "")[:90], "verdict": j.get("verdict"),
                        "branches": f"{j.get('source_branch')} → {j.get('target_branch')}",
                        "updated": j.get("updated") or j.get("created"),
                        "by": ", ".join(sorted({r.get("by") or r.get("source", "") for r in j.get("requests", [])}))})
    out.sort(key=lambda j: str(j.get("updated") or ""), reverse=True)
    return {"jobs": out}


def read_forwards() -> dict:
    d = os.path.join(status_mod._repo_root(), "temp", "port_forwards")
    if not os.path.isdir(d):
        return {"forwards": []}
    import podfwd                                         # only when forwards exist (it imports k8s helpers)
    return podfwd.list_all()


def collect(fresh: bool = False) -> dict:
    with _lock:
        now = time.time()
        if not fresh and _cache["data"] is not None and now - _cache["at"] < CACHE_S:
            return _cache["data"]
        data = {"generated_at": now, "host": socket.gethostname(),
                "sections": {
                    "store": _section(read_store),
                    "accounts": _section(read_accounts),
                    "reviews": _section(read_reviews),
                    "approvals": _section(status_mod.sect_approvals),
                    "daemons": _section(status_mod.sect_daemons),
                    "runs": _section(status_mod.sect_runs),
                    "queue": _section(status_mod.sect_queue),
                    "forwards": _section(read_forwards),
                    "feedback": _section(status_mod.sect_feedback),
                }}
        _cache.update(data=data, at=now)
        return data


class Handler(BaseHTTPRequestHandler):
    server_version = "toolkit-dashboard"

    def log_message(self, fmt, *args):                    # quiet: one line per error only
        if args and str(args[1] if len(args) > 1 else "").startswith(("4", "5")):
            sys.stderr.write("dashboard: " + (fmt % args) + "\n")

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
        return host in ("127.0.0.1", "localhost", "::1")

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if not self._host_ok():
            return self._send(403, b"forbidden host", "text/plain; charset=utf-8")
        path, _, query = self.path.partition("?")
        if path in ("/", "/index.html"):
            try:
                with open(HTML_FILE, "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            except OSError as e:
                return self._send(500, str(e).encode(), "text/plain; charset=utf-8")
        if path == "/api/state":
            data = collect(fresh="fresh=1" in query)
            return self._send(200, json.dumps(data, ensure_ascii=False, default=str).encode("utf-8"),
                              "application/json; charset=utf-8")
        return self._send(404, b"not found", "text/plain; charset=utf-8")

    def do_POST(self):  # noqa: N802 — read-only dashboard
        self._send(405, b"read-only", "text/plain; charset=utf-8")

    do_PUT = do_DELETE = do_PATCH = do_POST


def serve(port: int) -> None:
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.daemon_threads = True
    print(f"Dashboard: http://127.0.0.1:{port}   (Ctrl+C để dừng)", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("serve")
    p.add_argument("--port", type=int, default=int(os.environ.get("TOOLKIT_DASHBOARD_PORT") or 8765))
    sub.add_parser("snapshot")
    a = ap.parse_args(argv)
    if a.cmd == "serve":
        serve(a.port)
    else:
        print(json.dumps(collect(fresh=True), ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
