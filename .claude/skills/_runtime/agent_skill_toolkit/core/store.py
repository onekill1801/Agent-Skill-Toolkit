"""Durable task store (SQLite, WAL) — the state that survives a quota stop, a crash or a reboot.

Design §5.1 / §7 / §B.4-M5. Workflows own what a task MEANS (kind, steps, data); this module
only guarantees:
  * one owner at a time (lease, claimed atomically with BEGIN IMMEDIATE)
  * a crashed owner's lease expires and the task becomes claimable again
  * progress = checkpoints (step + data + optional SHA); resume reads the last one
  * a quota stop is a PAUSE with a resume time, not a failure (attempts unchanged)
  * every transition is an event (audit, "minutes of human involvement", status board)

Statuses:  queued -> running -> done
                       |-> paused_quota (resume_at) -> running ...
                       |-> blocked (needs a human)     -> queued (released)
                       |-> queued (retry, attempts+1)  -> ... -> failed (attempts exhausted)
           any open -> cancelled
"""

import contextlib
import json
import os
import sqlite3
import time
import uuid

from . import paths

OPEN = ("queued", "running", "paused_quota", "blocked")
CLOSED = ("done", "failed", "cancelled")
DEFAULT_LEASE_S = 900

SCHEMA = """
create table if not exists tasks (
  id text primary key, kind text not null, ref text not null, title text default '',
  status text not null, step text default '', attempts integer default 0, max_attempts integer default 3,
  priority integer default 5, lease_owner text, lease_until real, resume_at real, last_error text,
  backend text, data text default '{}', created real not null, updated real not null);
create index if not exists tasks_pick on tasks(status, priority, created);
create index if not exists tasks_ref on tasks(kind, ref);
create table if not exists checkpoints (
  id integer primary key autoincrement, task_id text not null, step text not null,
  data text default '{}', sha text, ts real not null);
create index if not exists cp_task on checkpoints(task_id, id);
create table if not exists events (
  id integer primary key autoincrement, task_id text, ts real not null, kind text not null, data text);
create index if not exists ev_task on events(task_id, id);
create table if not exists usage (
  id integer primary key autoincrement, task_id text, step text, backend text, model text,
  tokens_in integer, tokens_out integer, seconds real, ts real not null);
"""


class LeaseLost(Exception):
    """The caller no longer owns the task (lease expired and someone else claimed it)."""


def default_path() -> str:
    return os.environ.get("TOOLKIT_STORE") or paths.state_dir("toolkit.db")


def _row(r) -> dict:
    if r is None:
        return {}
    d = dict(r)
    d["data"] = json.loads(d.get("data") or "{}")
    return d


class Store:
    def __init__(self, path: str = ""):
        self.path = path or default_path()
        self.db = sqlite3.connect(self.path, timeout=30, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("pragma journal_mode=wal")
        self.db.execute("pragma busy_timeout=30000")
        self.db.executescript(SCHEMA)

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    @contextlib.contextmanager
    def _tx(self):
        self.db.execute("begin immediate")       # take the write lock now: claims are atomic
        try:
            yield
            self.db.execute("commit")
        except BaseException:
            self.db.execute("rollback")
            raise

    def _event(self, task_id, event_kind, **data):
        self.db.execute("insert into events(task_id, ts, kind, data) values (?,?,?,?)",
                        (task_id, time.time(), event_kind, json.dumps(data, ensure_ascii=False)))

    # ------------------------------------------------------------------ tasks
    def add(self, kind: str, ref: str, *, title: str = "", data: dict = None, priority: int = 5,
            max_attempts: int = 3) -> dict:
        """Create a task, or return the open one with the same (kind, ref)."""
        now = time.time()
        with self._tx():
            cur = self.db.execute(
                f"select * from tasks where kind=? and ref=? and status in ({','.join('?' * len(OPEN))})",
                (kind, ref, *OPEN)).fetchone()
            if cur:
                return {**_row(cur), "duplicate": True}
            tid = uuid.uuid4().hex[:12]
            self.db.execute("insert into tasks(id, kind, ref, title, status, priority, max_attempts, data, "
                            "created, updated) values (?,?,?,?,?,?,?,?,?,?)",
                            (tid, kind, ref, title, "queued", priority, max_attempts,
                             json.dumps(data or {}, ensure_ascii=False), now, now))
            self._event(tid, "added", task_kind=kind, ref=ref)
        return {**self.get(tid), "duplicate": False}

    def get(self, task_id: str) -> dict:
        return _row(self.db.execute("select * from tasks where id=?", (task_id,)).fetchone())

    def list(self, status: tuple = (), kind: str = "", limit: int = 100) -> list:
        q, args = "select * from tasks where 1=1", []
        if status:
            q += f" and status in ({','.join('?' * len(status))})"
            args += list(status)
        if kind:
            q += " and kind=?"
            args.append(kind)
        q += " order by updated desc limit ?"
        return [_row(r) for r in self.db.execute(q, (*args, limit))]

    def claim(self, owner: str, kinds: tuple = (), lease_s: int = DEFAULT_LEASE_S) -> dict:
        """Atomically take the next runnable task: queued, paused_quota whose time has come,
        or running with an expired lease (crashed owner). Highest priority (lowest number) first."""
        now = time.time()
        kind_sql = f" and kind in ({','.join('?' * len(kinds))})" if kinds else ""
        with self._tx():
            r = self.db.execute(
                "select * from tasks where ("
                " status='queued'"
                " or (status='paused_quota' and coalesce(resume_at,0)<=?)"
                " or (status='running' and coalesce(lease_until,0)<?))" + kind_sql +
                " order by priority, created limit 1", (now, now, *kinds)).fetchone()
            if not r:
                return {}
            prev = r["status"]
            self.db.execute("update tasks set status='running', lease_owner=?, lease_until=?, resume_at=null, "
                            "updated=? where id=?", (owner, now + lease_s, now, r["id"]))
            self._event(r["id"], "claimed", owner=owner, previous=prev,
                        reclaimed_from=r["lease_owner"] if prev == "running" else None)
        return self.get(r["id"])

    def _owned(self, task_id: str, owner: str):
        r = self.db.execute("select status, lease_owner, lease_until from tasks where id=?", (task_id,)).fetchone()
        if not r or r["status"] != "running" or r["lease_owner"] != owner:
            raise LeaseLost(f"task {task_id} is not held by {owner}")

    def heartbeat(self, task_id: str, owner: str, lease_s: int = DEFAULT_LEASE_S):
        with self._tx():
            self._owned(task_id, owner)
            self.db.execute("update tasks set lease_until=?, updated=? where id=?",
                            (time.time() + lease_s, time.time(), task_id))

    def checkpoint(self, task_id: str, owner: str, step: str, data: dict = None, sha: str = ""):
        with self._tx():
            self._owned(task_id, owner)
            now = time.time()
            self.db.execute("insert into checkpoints(task_id, step, data, sha, ts) values (?,?,?,?,?)",
                            (task_id, step, json.dumps(data or {}, ensure_ascii=False), sha or None, now))
            self.db.execute("update tasks set step=?, updated=? where id=?", (step, now, task_id))
            self._event(task_id, "checkpoint", step=step, sha=sha or None)

    def checkpoints(self, task_id: str) -> list:
        return [{**dict(r), "data": json.loads(r["data"] or "{}")}
                for r in self.db.execute("select * from checkpoints where task_id=? order by id", (task_id,))]

    def last_checkpoint(self, task_id: str) -> dict:
        cps = self.checkpoints(task_id)
        return cps[-1] if cps else {}

    def update_data(self, task_id: str, owner: str, **fields):
        with self._tx():
            self._owned(task_id, owner)
            data = self.get(task_id)["data"]
            data.update(fields)
            self.db.execute("update tasks set data=?, updated=? where id=?",
                            (json.dumps(data, ensure_ascii=False), time.time(), task_id))

    def _finish(self, task_id, owner, status, event, error=None, **extra):
        with self._tx():
            self._owned(task_id, owner)
            sets = ["status=?", "lease_owner=null", "lease_until=null", "updated=?"]
            args = [status, time.time()]
            if error is not None:
                sets.append("last_error=?")
                args.append(str(error)[:2000])
            for k, v in extra.items():
                sets.append(f"{k}=?")
                args.append(v)
            self.db.execute(f"update tasks set {', '.join(sets)} where id=?", (*args, task_id))
            self._event(task_id, event, error=str(error)[:300] if error else None,
                        **{k: v for k, v in extra.items() if k != "attempts"})

    def pause_quota(self, task_id: str, owner: str, resume_at: float, backend: str = "", reason: str = ""):
        """Provider limit hit: stop now, resume later from the last checkpoint. Not a failure."""
        self._finish(task_id, owner, "paused_quota", "paused_quota", error=reason or None,
                     resume_at=float(resume_at), backend=backend or None)

    def block(self, task_id: str, owner: str, reason: str):
        """Needs a human (decision, missing info, budget exceeded)."""
        self._finish(task_id, owner, "blocked", "blocked", error=reason)

    def release(self, task_id: str, owner: str):
        self._finish(task_id, owner, "queued", "released")

    def complete(self, task_id: str, owner: str, **result):
        if result:
            self.update_data(task_id, owner, result=result)
        self._finish(task_id, owner, "done", "done")

    def fail(self, task_id: str, owner: str, error: str) -> str:
        """Count an attempt; requeue while attempts remain, else failed. Returns the new status."""
        t = self.get(task_id)
        attempts = int(t.get("attempts") or 0) + 1
        status = "queued" if attempts < int(t.get("max_attempts") or 3) else "failed"
        self._finish(task_id, owner, status, "failed_attempt" if status == "queued" else "failed",
                     error=error, attempts=attempts)
        return status

    def unblock(self, task_id: str, by: str = ""):
        with self._tx():
            n = self.db.execute("update tasks set status='queued', updated=? where id=? and status='blocked'",
                                (time.time(), task_id)).rowcount
            if n:
                self._event(task_id, "unblocked", by=by)
        return bool(n)

    def cancel(self, task_id: str, by: str = ""):
        with self._tx():
            n = self.db.execute(
                f"update tasks set status='cancelled', lease_owner=null, lease_until=null, updated=? "
                f"where id=? and status in ({','.join('?' * len(OPEN))})", (time.time(), task_id, *OPEN)).rowcount
            if n:
                self._event(task_id, "cancelled", by=by)
        return bool(n)

    # ------------------------------------------------------------------ usage / events
    def record_usage(self, task_id: str, step: str, backend: str, model: str = "",
                     tokens_in: int = None, tokens_out: int = None, seconds: float = None):
        self.db.execute("insert into usage(task_id, step, backend, model, tokens_in, tokens_out, seconds, ts) "
                        "values (?,?,?,?,?,?,?,?)",
                        (task_id, step, backend, model, tokens_in, tokens_out, seconds, time.time()))

    def events(self, task_id: str = "", limit: int = 200) -> list:
        q = "select * from events" + (" where task_id=?" if task_id else "") + " order by id desc limit ?"
        rows = self.db.execute(q, (task_id, limit) if task_id else (limit,))
        return [{**dict(r), "data": json.loads(r["data"] or "{}")} for r in rows][::-1]

    def summary(self) -> dict:
        counts = {r["status"]: r["n"] for r in self.db.execute("select status, count(*) n from tasks group by status")}
        nxt = self.db.execute("select min(resume_at) t from tasks where status='paused_quota'").fetchone()["t"]
        return {"counts": counts, "next_resume_at": nxt}
