#!/usr/bin/env python3
"""Toolkit web dashboard — ONE page to watch everything the repo runs, plus the settings form.

    python dashboard.py serve [--port 8765]     # prints the URL to open (with its edit key), e.g. in Orca's browser
    python dashboard.py snapshot                # the same data once, as JSON (scripts / debugging)

Sections: overview (usage tier, capacity, daily cap) · tasks of the durable store (review_mr / verify /
proposal / refactor / atask, as `coordinator.py board`) · agent accounts (`accounts.py`: who runs what,
usage left from Orca) · MR review jobs · pending approvals · daemons · pipeline runs · intake queue ·
port-forwards · feedback · settings (workflows.settings: non-secret switches only) · a command help per section.

Safety: listens on 127.0.0.1 only, refuses a foreign Host header (DNS rebinding). Monitoring is read-only
(store opened with Store.reader = sqlite mode=ro; no reader creates a directory); approvals and every
other action stay terminal commands (shown on the page). The ONE write is POST /api/settings, which needs
the per-run edit key printed by `serve` (only in that terminal — the page receives it through the URL
fragment, never from the server), a same-origin JSON request, and passes the catalog's validation.
Secrets are never read out: the page only learns whether a secret key is set. Free text from state files
is passed through the secret redactor before it leaves the process; no traceback is sent.

Snapshots are built by ONE background worker (never per request): a request gets the latest snapshot
at once; `?fresh=1` asks the worker for a new one and waits a few seconds at most.
"""

import argparse
import hmac
import json
import os
import runpy
import secrets
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_HERE = os.path.dirname(os.path.abspath(__file__))
_SKILLS = os.path.dirname(os.path.dirname(_HERE))
runpy.run_path(os.path.join(_SKILLS, "_runtime", "bootstrap.py"))["activate"]()
from agent_skill_toolkit.core import store as store_mod  # noqa: E402
from agent_skill_toolkit.workflows import coordinator as coord  # noqa: E402
from agent_skill_toolkit.workflows import settings as settings_mod  # noqa: E402
from agent_skill_toolkit.workflows.execution import usage as usage_mod  # noqa: E402
from agent_skill_toolkit.workflows.verify.safety import redact  # noqa: E402

sys.path.insert(0, _HERE)
import status as status_mod  # noqa: E402  (daemons / runs / queue / feedback readers)

HTML_FILE = os.path.join(_HERE, "dashboard.html")
CACHE_S = 5                 # the worker rebuilds the snapshot at most this often (while someone watches)
IDLE_S = 60                 # no request for this long: the worker stops polling
ACCOUNTS_CACHE_S = 30       # `orca account list` is a subprocess: at most twice a minute
ORCA_TIMEOUT_S = 10
FRESH_WAIT_S = 6            # `?fresh=1` waits at most this long for the new snapshot
FAILED_WINDOW_S = 7 * 86400  # failures are terminal (no "resolve" action): shown for this long, KPI = same window
_cache = {"accounts_at": 0.0, "accounts": None}


def _clean(obj):
    """EVERY string leaving the process goes through the secret redactor — no key is trusted, since
    ids / statuses of state files are as untrusted as titles (Codex dashboard #1, R2 #1, R3 #1)."""
    if isinstance(obj, dict):
        return {(redact(k) if isinstance(k, str) else k): _clean(v) for k, v in obj.items()}   # keys too (R4)
    if isinstance(obj, list):
        return [_clean(v) for v in obj]
    if isinstance(obj, str):
        return redact(obj)
    return obj


def _section(fn):
    """Run one reader; a failing section becomes {"ok": False, "error": <type: redacted message>}
    and never breaks the page. No traceback leaves the process."""
    t0 = time.time()
    try:
        return {"ok": True, "data": _clean(fn()), "ms": round((time.time() - t0) * 1000)}
    except Exception as e:  # noqa: BLE001 — a dashboard shows the error instead of dying
        return {"ok": False, "error": redact(f"{type(e).__name__}: {e}")[:300],
                "ms": round((time.time() - t0) * 1000)}


def _state_base() -> str:
    """Base of the toolkit's state files, as core.paths / rc_config resolve it — never created here."""
    return os.environ.get("TOOLKIT_STATE_DIR") or os.path.join(status_mod._repo_root(), "temp")


def read_store() -> dict:
    """Board + settings through a READ-ONLY connection (no schema setup / write lock while agents run)."""
    try:
        st = store_mod.Store.reader()
    except FileNotFoundError:
        return {"exists": False, "rows": [], "summary": {"counts": {}}, "settings": {}, "done_24h": 0}
    try:
        b = coord.board(st)                                  # open tasks + those closed in the last 24 h
        today = usage_mod.agent_usage(st, since=usage_mod.local_midnight())
        settings = {"capacity": st.capacity(),
                    "daily_agent_runs": int(st.get_setting("daily_agent_runs") or 0),
                    "agent_runs_today": today["runs"], "agent_seconds_today": round(today["seconds"])}
        # counted in SQL, independent of the board's display limit (Codex dashboard R2 #3)
        now = time.time()
        done_24h = st.db.execute("select count(*) n from tasks where status='done' and updated>=?",
                                 (now - 86400,)).fetchone()["n"]
        # failed tasks for "needs you", independent of the board's closed-row limit (Codex dashboard R3 #2)
        failed = [{"id": t["id"], "kind": t["kind"], "title": t.get("title") or t["ref"], "status": "failed",
                   "note": str(t.get("last_error") or "").splitlines()[0][:160] if t.get("last_error") else "",
                   "updated": t.get("updated")}
                  for t in st.list(status=("failed",), limit=500) if (t.get("updated") or 0) >= now - FAILED_WINDOW_S]
        return {"exists": True, "rows": b["rows"], "summary": b["summary"], "settings": settings,
                "done_24h": done_24h, "failed_recent": failed, "failed_window_days": FAILED_WINDOW_S // 86400,
                "failed_window_count": st.db.execute("select count(*) n from tasks where status='failed' and updated>=?",
                                                     (now - FAILED_WINDOW_S,)).fetchone()["n"]}
    finally:
        st.close()


def read_accounts() -> dict:
    now = time.time()
    if _cache["accounts"] is not None and now - _cache["accounts_at"] < ACCOUNTS_CACHE_S:
        return _cache["accounts"]
    sys.path.insert(0, os.path.join(_SKILLS, "auto-dev", "tools"))
    import accounts as accounts_tool                      # auto-dev/tools/accounts.py (facade)
    data = accounts_tool.gather(orca_timeout=ORCA_TIMEOUT_S)
    _cache.update(accounts=data, accounts_at=now)
    return data


def _json_files(d: str):
    """(name, data) of every *.json in d, plus how many could NOT be read: a skipped file must
    never turn into "nothing to do" on the page (Codex dashboard R2 #2)."""
    ok, bad = [], 0
    if not os.path.isdir(d):
        return ok, bad
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return ok, 1
    for name in names:
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(d, name), encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            continue                                          # removed while listing: not an error
        except (OSError, ValueError):
            bad += 1
            continue
        if isinstance(data, dict):
            ok.append((name, data))
        else:
            bad += 1
    return ok, bad


def read_approvals() -> dict:
    """Pending approval requests — the SAME directory rc_config/approvals write (TOOLKIT_STATE_DIR aware)."""
    files, bad = _json_files(os.path.join(_state_base(), "tg_approvals"))
    items = [{"id": r.get("id"), "tool": r.get("tool"), "risk": r.get("risk"),
              "summary": (r.get("summary") or "")[:60], "created": r.get("created") or 0}
             for _, r in files if r.get("status") == "pending" and r.get("id") and r.get("tool")]
    items.sort(key=lambda a: a["created"], reverse=True)
    return {"items": items, "unreadable": bad}


def read_reviews() -> dict:
    # same place as review_common.temp_dir("jobs"), computed WITHOUT its makedirs
    jobs_dir = os.path.join(os.environ.get("REVIEW_STATE_DIR")
                            or os.path.join(status_mod._repo_root(), "temp", "review_queue"), "jobs")
    out = []
    files, bad = _json_files(jobs_dir)
    for _, j in files:
        out.append({"id": j.get("id"), "status": j.get("status"),
                    "mr": f"{j.get('project_path') or j.get('project')}!{j.get('iid')}",
                    "title": (j.get("title") or "")[:90], "verdict": j.get("verdict"),
                    "branches": f"{j.get('source_branch')} → {j.get('target_branch')}",
                    "updated": j.get("updated") or j.get("created"),
                    "by": ", ".join(sorted({str(r.get("by") or r.get("source", ""))
                                            for r in j.get("requests") or [] if isinstance(r, dict)}))})
    out.sort(key=lambda j: str(j.get("updated") or ""), reverse=True)
    return {"jobs": out, "unreadable": bad}


def _port_open(port) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=0.3):
            return True
    except (OSError, ValueError, TypeError):
        return False


def read_forwards() -> dict:
    """podfwd state files read directly (podfwd.list_all would mkdir and run `tasklist` without a
    deadline — Codex dashboard #3/#4): name, ports, target and whether the local port answers."""
    d = os.path.join(status_mod._repo_root(), "temp", "port_forwards")
    rows = []
    if os.path.isdir(d):
        for f in sorted(os.listdir(d)):
            if not f.endswith(".json"):
                continue
            try:
                with open(os.path.join(d, f), encoding="utf-8") as fh:
                    st = json.load(fh)
            except (OSError, ValueError):
                continue
            if not isinstance(st, dict) or not st.get("local_port"):
                continue
            t = st.get("target_spec") or {}
            rows.append({"name": st.get("name") or f[:-5], "local": f"127.0.0.1:{st['local_port']}",
                         "target": st.get("target"), "context": t.get("context"), "namespace": t.get("namespace"),
                         "listening": _port_open(st["local_port"]), "restarts": st.get("restarts", 0)})
    return {"count": len(rows), "forwards": rows}


def read_settings() -> dict:
    """Editable settings (values of NON-secret keys only) + which secret keys are set — no redaction:
    the catalog holds no secret, and a redacted value (a 24-hex group id looks like a token) would be
    saved back wrong."""
    try:
        st = store_mod.Store.reader()
    except FileNotFoundError:
        st = None
    try:
        return settings_mod.snapshot(st)
    finally:
        if st is not None:
            st.close()


READERS = {"store": "read_store", "accounts": "read_accounts", "reviews": "read_reviews",
           "approvals": "read_approvals", "daemons": None, "runs": None, "queue": None,
           "forwards": "read_forwards", "feedback": None}


def collect() -> dict:
    """Build one snapshot now (the worker's job; also `snapshot` and tests)."""
    mod = sys.modules[__name__]
    sections = {}
    for name, fn in READERS.items():
        reader = getattr(mod, fn) if fn else getattr(status_mod, f"sect_{name}")
        sections[name] = _section(reader)
    return {"generated_at": time.time(), "host": socket.gethostname(), "sections": sections}


class Snapshots:
    """ONE background worker builds snapshots; requests never run readers themselves (Codex #4)."""

    def __init__(self, build=collect):
        self.build = build
        self.data, self.gen = None, 0
        self.cond = threading.Condition()
        self.wanted = threading.Event()
        self.last_request = time.time()
        self.thread = None

    def _loop(self):
        while True:
            self.wanted.wait(timeout=CACHE_S)
            self.wanted.clear()
            if time.time() - self.last_request > IDLE_S and self.data is not None:
                continue                                       # nobody watching: stay idle
            data = self.build()
            with self.cond:
                self.data, self.gen = data, self.gen + 1
                self.cond.notify_all()

    def start(self):
        if not self.thread:
            self.thread = threading.Thread(target=self._loop, name="dashboard-snapshots", daemon=True)
            self.thread.start()
        return self

    def get(self, fresh: bool = False, wait_s: float = FRESH_WAIT_S) -> dict:
        self.last_request = time.time()
        with self.cond:
            gen = self.gen
            if fresh or self.data is None:
                self.wanted.set()                              # coalesced: many fresh=1 -> one rebuild
                self.cond.wait_for(lambda: self.gen > gen, timeout=wait_s if self.data is not None else 30)
            return self.data or {"generated_at": time.time(), "host": socket.gethostname(),
                                 "sections": {}, "pending": True}


SNAPSHOTS = Snapshots()


MAX_BODY = 16 * 1024


class Handler(BaseHTTPRequestHandler):
    server_version = "toolkit-dashboard"
    snapshots = SNAPSHOTS
    edit_key = ""               # set by serve(): random per run, printed only to its terminal

    def log_message(self, fmt, *args):                    # quiet: only 4xx / 5xx
        if len(args) > 1 and str(args[1]).startswith(("4", "5")):
            sys.stderr.write("dashboard: " + (fmt % args) + "\n")

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").strip().lower()
        if host.startswith("["):
            host = host[1:].split("]", 1)[0]
        else:
            host = host.rsplit(":", 1)[0]
        return host in ("127.0.0.1", "localhost", "::1")

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
                         "frame-ancestors 'none'")
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
            except OSError:
                return self._send(500, b"dashboard.html missing", "text/plain; charset=utf-8")
        if path == "/api/state":
            data = self.snapshots.get(fresh="fresh=1" in query.split("&"))
            return self._json(200, data)
        if path == "/api/settings":
            try:                                               # NOT through _clean: see read_settings
                return self._json(200, {"ok": True, "data": read_settings(), "editable": bool(self.edit_key)})
            except Exception as e:  # noqa: BLE001
                return self._json(200, {"ok": False, "error": redact(f"{type(e).__name__}: {e}")[:300]})
        return self._send(404, b"not found", "text/plain; charset=utf-8")

    def _json(self, code: int, obj):
        return self._send(code, json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"),
                          "application/json; charset=utf-8")

    def _write_allowed(self) -> str:
        """'' when this request may change settings, else why not. A browser page of another site can not
        pass: it has no edit key (never served by GET), a JSON body forces a CORS preflight we never
        answer, and Origin / Sec-Fetch-Site must be this dashboard."""
        if not self._host_ok():
            return "forbidden host"
        port = self.server.server_address[1]
        origin = self.headers.get("Origin")
        if origin is not None and origin not in (f"http://127.0.0.1:{port}", f"http://localhost:{port}"):
            return "foreign origin"
        if (self.headers.get("Sec-Fetch-Site") or "same-origin") != "same-origin":
            return "cross-site request"
        if (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/json":
            return "JSON only"
        key = self.headers.get("X-Dashboard-Key") or ""
        if not self.edit_key or not hmac.compare_digest(key.encode(), self.edit_key.encode()):
            return "missing or wrong edit key — open the URL printed by `dashboard.py serve`"
        return ""

    def do_POST(self):  # noqa: N802
        path = self.path.partition("?")[0]
        if path != "/api/settings":
            return self._send(405, b"read-only", "text/plain; charset=utf-8")
        why = self._write_allowed()
        if why:
            return self._json(403, {"ok": False, "error": why})
        try:
            n = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            n = -1
        if not 0 < n <= MAX_BODY:
            return self._json(413, {"ok": False, "error": "body missing or too large"})
        try:
            body = json.loads(self.rfile.read(n).decode("utf-8"))
            if not isinstance(body, dict):                     # null / [] / "x" (Codex settings R1 #5)
                raise ValueError("body must be a JSON object")
            changes, expected = body.get("changes"), body.get("expected") or {}
            if not isinstance(changes, dict) or not isinstance(expected, dict):
                raise ValueError("changes / expected must be objects")
            out = settings_mod.apply(changes, by="dashboard", expected=expected)
        except (ValueError, RuntimeError, FileNotFoundError, TimeoutError, UnicodeDecodeError) as e:
            return self._json(400, {"ok": False, "error": redact(str(e))[:400]})
        except OSError as e:                                   # e.g. .env locked by another program (R1 #2)
            return self._json(503, {"ok": False, "error": redact(f"không ghi được: {type(e).__name__}: {e}")[:400]})
        self.snapshots.wanted.set()                            # capacity / caps show at once
        return self._json(200, {"ok": True, **out})

    do_PUT = do_DELETE = do_PATCH = lambda self: self._send(405, b"read-only", "text/plain; charset=utf-8")


def serve(port: int, read_only: bool = False) -> None:
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.daemon_threads = True
    Handler.snapshots.start()
    Handler.edit_key = "" if read_only else secrets.token_urlsafe(24)
    if read_only:
        print(f"Dashboard (chỉ xem): http://127.0.0.1:{port}   (Ctrl+C để dừng)", flush=True)
    else:
        # the key travels in the URL FRAGMENT: browsers never send it to a server or put it in logs
        print(f"Dashboard: http://127.0.0.1:{port}/#key={Handler.edit_key}\n"
              f"  mở ĐÚNG link này để sửa được cài đặt (key đổi mỗi lần chạy; mở không có key = chỉ xem)\n"
              f"  Ctrl+C để dừng", flush=True)
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
    p.add_argument("--read-only", action="store_true", help="no settings form (no edit key)")
    sub.add_parser("snapshot")
    a = ap.parse_args(argv)
    if a.cmd == "serve":
        serve(a.port, read_only=a.read_only)
    else:
        print(json.dumps(collect(), ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
