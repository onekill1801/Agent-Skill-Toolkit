#!/usr/bin/env python3
"""Shared supervisor for long-running poll daemons (mr_watch, atask_watch, ...).

The watchers used to loop with a fixed `time.sleep(interval)` and a broad
`except Exception: print(...)`. That survives a crash but behaves badly on real
outages:
  - token expiry (HTTP 401/403) → the loop spins forever, silently useless;
  - network/5xx/429 → it retries at full rate (no backoff), hammering the server.

`supervise()` fixes both. It classifies failures:
  - FATAL (auth/token: 401/403) → log, notify a human ONCE, and STOP. A token
    can only be replaced by hand; spinning is pointless.
  - TRANSIENT (network status 0, 408/409/425/429, 5xx, or any unexpected crash)
    → exponential backoff (1s → ×1.5 → cap 60s), reset on the next success. After
    N consecutive failures it notifies once ("degraded") but keeps retrying.

Every state change is appended to `temp/daemon_health.jsonl` so you can see what
happened (`python daemon_common.py status`).

The API tools in this repo return error envelopes `{"error": true, "status": ...}`
rather than raising, so `guard(payload)` turns one of those into the right
exception for the supervisor to classify.

Stdlib only. Import from any skill's tools dir via sys.path.
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

# HTTP statuses that mean "your credential is bad" — no amount of retrying helps.
FATAL_STATUSES = {401, 403}
# Statuses worth retrying with backoff (0 = urllib URLError / network down).
TRANSIENT_STATUSES = {0, 408, 409, 425, 429, 500, 502, 503, 504}

BACKOFF_BASE = 1.0
BACKOFF_FACTOR = 1.5
BACKOFF_CAP = 60.0
DEGRADED_AFTER = 5  # consecutive transient failures before a one-time "degraded" alert


class DaemonError(Exception):
    """Base for supervisor-classified errors."""


class DaemonFatal(DaemonError):
    """Unrecoverable without human action (e.g. expired token) → stop the daemon."""


class DaemonTransient(DaemonError):
    """Temporary (network/5xx/429) → back off and retry."""


def _repo_root() -> str:
    search = os.path.dirname(os.path.abspath(__file__))
    for _ in range(6):
        if os.path.isdir(os.path.join(search, ".git")) or \
           os.path.isfile(os.path.join(search, "CLAUDE.md")):
            return search
        search = os.path.dirname(search)
    return os.path.dirname(os.path.abspath(__file__))


def _health_path() -> str:
    d = os.path.join(_repo_root(), "temp")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "daemon_health.jsonl")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def health_log(label: str, event: str, detail: str = "") -> None:
    """Append one line to temp/daemon_health.jsonl. Never raises."""
    rec = {"ts": _now(), "label": label, "event": event, "detail": (detail or "")[:500]}
    try:
        with open(_health_path(), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def classify_status(status) -> str:
    """Map an HTTP-ish status to 'fatal' | 'transient'. Unknown non-auth → transient."""
    try:
        s = int(status)
    except (TypeError, ValueError):
        return "transient"
    if s in FATAL_STATUSES:
        return "fatal"
    return "transient"  # network(0), 429, 5xx, and any other 4xx → retry+log, never die


def guard(payload, where: str = "") -> None:
    """Raise the right DaemonError if `payload` is an API error envelope.

    Tools here return {"error": True, "status": <code>, "message": ...} on failure
    (status 0 = network). A plain success payload passes through untouched.
    """
    if not isinstance(payload, dict) or not payload.get("error"):
        return
    status = payload.get("status")
    msg = payload.get("message") or payload.get("errorMessage") or str(payload)
    label = f"{where}: " if where else ""
    if classify_status(status) == "fatal":
        raise DaemonFatal(f"{label}auth/token failed (HTTP {status}): {msg}")
    raise DaemonTransient(f"{label}transient error (status {status}): {msg}")


def _is_networkish(exc: BaseException) -> bool:
    name = type(exc).__name__.lower()
    return any(k in name for k in ("url", "timeout", "connection", "socket", "ssl", "os"))


class Backoff:
    """Exponential backoff with reset-on-success."""

    def __init__(self, base=BACKOFF_BASE, factor=BACKOFF_FACTOR, cap=BACKOFF_CAP):
        self.base, self.factor, self.cap = base, factor, cap
        self._cur = base

    def reset(self) -> None:
        self._cur = self.base

    def next(self) -> float:
        v = self._cur
        self._cur = min(self._cur * self.factor, self.cap)
        return v


def supervise(poll_fn, *, interval: float, label: str, notify=None,
              degraded_after: int = DEGRADED_AFTER) -> None:
    """Run poll_fn() forever with backoff + health logging + fatal-stop.

    poll_fn: a zero-arg callable doing one poll cycle. It may raise DaemonFatal /
      DaemonTransient (e.g. via guard()); any other exception is treated as
      transient so a bug never kills the daemon.
    interval: seconds between successful polls.
    notify(msg): optional callback to alert a human (Telegram, etc.). Called once
      on fatal, and once when the daemon first becomes "degraded".
    """
    health_log(label, "started", f"interval={interval}s")
    backoff = Backoff()
    consecutive = 0
    degraded_sent = False
    while True:
        try:
            poll_fn()
        except KeyboardInterrupt:
            health_log(label, "stopped", "KeyboardInterrupt")
            print(f"[{_now()}] {label}: stopped.", file=sys.stderr)
            return
        except DaemonFatal as e:
            health_log(label, "fatal", str(e))
            print(f"[{_now()}] {label}: FATAL — {e}\n"
                  f"    Daemon stopped; fix the credential then restart.", file=sys.stderr)
            if notify:
                _safe_notify(notify, f"🛑 {label}: dừng do lỗi xác thực — {e}. "
                                     f"Cần thay token rồi khởi động lại.")
            return
        except Exception as e:  # noqa: BLE001 — never let the loop die (DaemonFatal handled above)
            consecutive += 1
            kind = "transient" if isinstance(e, DaemonTransient) or _is_networkish(e) else "error"
            delay = backoff.next()
            health_log(label, kind, f"#{consecutive} retry in {delay:.0f}s — {e}")
            print(f"[{_now()}] {label}: {kind} (#{consecutive}) — {e}; "
                  f"retry in {delay:.0f}s", file=sys.stderr)
            if not degraded_sent and consecutive >= degraded_after:
                degraded_sent = True
                if notify:
                    _safe_notify(notify, f"⚠️ {label}: {consecutive} lần lỗi liên tiếp "
                                         f"(mạng/dịch vụ?). Vẫn đang thử lại, sẽ báo khi hồi.")
            time.sleep(delay)
            continue
        # success
        if consecutive:
            health_log(label, "recovered", f"after {consecutive} failure(s)")
            if degraded_sent and notify:
                _safe_notify(notify, f"✅ {label}: đã hồi phục sau {consecutive} lần lỗi.")
            consecutive = 0
            degraded_sent = False
            backoff.reset()
        time.sleep(interval)


def _safe_notify(notify, msg: str) -> None:
    try:
        notify(msg)
    except Exception as e:  # noqa: BLE001 — notification must never crash the supervisor
        print(f"[{_now()}] notify failed: {e}", file=sys.stderr)


def _cmd_status() -> int:
    """Summarize temp/daemon_health.jsonl: last event per daemon + recent errors."""
    path = _health_path()
    if not os.path.isfile(path):
        print(json.dumps({"ok": True, "daemons": {}, "note": "no health log yet"},
                         ensure_ascii=False, indent=2))
        return 0
    last = {}
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
    print(json.dumps({"ok": True, "daemons": last}, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Daemon supervisor helpers + health status.")
    sub = ap.add_subparsers(dest="action", required=True)
    sub.add_parser("status", help="show last health event per daemon")
    args = ap.parse_args()
    if args.action == "status":
        return _cmd_status()
    return 2


if __name__ == "__main__":
    sys.exit(main())
