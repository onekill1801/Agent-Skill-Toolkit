"""Durable task store (SQLite, WAL) — the state that survives a quota stop, a crash or a reboot.

Design §5.1 / §7 / §B.4-M5. Workflows own what a task MEANS (kind, steps, data); this module
only guarantees:
  * one holder at a time: every claim mints a fresh LEASE TOKEN; every mutation must present the
    current token of an unexpired lease (a stalled worker — even with the same name — whose lease
    was reclaimed gets LeaseLost, never overwrites the new holder)
  * a crashed holder's lease expires and the task becomes claimable again
  * progress = checkpoints (step + data + input key); resume reads the valid ones; they can be
    invalidated when their inputs change (new MR revision, code repaired after a green build)
  * a quota stop is a PAUSE with a resume time, not a failure (attempts unchanged)
  * every transition is an event (audit, "minutes of human involvement", status board)

Statuses:  queued -> running -> done
                       |-> paused_quota (resume_at) -> running ...
                       |-> blocked (needs a human)     -> queued (unblock)
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


def max_running_default() -> int:
    """Initial value of the SHARED capacity (stored in the database on first open, then that stored
    value is the only policy — see Store.capacity / set_capacity). 0 = no cap. Default 2."""
    try:
        return max(0, int(os.environ.get("TOOLKIT_MAX_PARALLEL", "2")))
    except ValueError:
        return 2

SCHEMA = """
create table if not exists tasks (
  id text primary key, kind text not null, ref text not null, title text default '',
  status text not null, step text default '', attempts integer default 0, max_attempts integer default 3,
  priority integer default 5, lease_owner text, lease_token text, lease_until real, resume_at real,
  last_error text, backend text, data text default '{}', created real not null, updated real not null);
create index if not exists tasks_pick on tasks(status, priority, created);
create index if not exists tasks_ref on tasks(kind, ref);
create table if not exists checkpoints (
  id integer primary key autoincrement, task_id text not null, step text not null,
  data text default '{}', sha text, input_key text, valid integer default 1, ts real not null);
create index if not exists cp_task on checkpoints(task_id, id);
create table if not exists events (
  id integer primary key autoincrement, task_id text, ts real not null, kind text not null, data text);
create index if not exists ev_task on events(task_id, id);
create table if not exists deps (
  task_id text not null, depends_on text not null, primary key (task_id, depends_on));
create index if not exists deps_on on deps(depends_on);
create table if not exists settings (key text primary key, value text);
create table if not exists usage (
  id integer primary key autoincrement, task_id text, step text, backend text, model text,
  tokens_in integer, tokens_out integer, seconds real, ts real not null);
"""
# columns added after the first release of the schema (ALTER for existing databases)
_MIGRATIONS = {"tasks": {"lease_token": "text", "resource": "text", "parent": "text"},
               "checkpoints": {"input_key": "text", "valid": "integer default 1"}}


DEP_BLOCK = "dependency "     # last_error prefix of a task blocked because a dependency cannot finish


class LeaseLost(Exception):
    """The caller no longer holds the task (lease expired, or the task was claimed again)."""


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
        self.db.execute("pragma busy_timeout=30000")
        # switching to WAL needs an exclusive lock and can fail fast with "locked" when several
        # workers open the file at once (the busy handler is not always consulted): retry briefly
        for attempt in range(100):
            try:
                self.db.execute("pragma journal_mode=wal")
                break
            except sqlite3.OperationalError as e:
                if "locked" not in str(e).lower() or attempt == 99:
                    raise
                time.sleep(0.1)
        with self._tx():                 # serialize schema setup: two workers may open an old DB at once
            for stmt in (s.strip() for s in SCHEMA.split(";")):
                if stmt:
                    self.db.execute(stmt)
            for table, cols in _MIGRATIONS.items():
                have = {r["name"] for r in self.db.execute(f"pragma table_info({table})")}
                for col, decl in cols.items():
                    if col not in have:
                        self.db.execute(f"alter table {table} add column {col} {decl}")
            # ONE capacity policy per database, whatever the environment of the process (Codex F.7 R2 #4)
            self.db.execute("insert or ignore into settings(key, value) values ('max_running', ?)",
                            (str(max_running_default()),))

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
            max_attempts: int = 3, resource: str = "", parent: str = "", depends_on=()) -> dict:
        """Create a task, or return the open one with the same (kind, ref).
        resource:   exclusive key (e.g. "repo:<path>@<branch>") - two tasks holding the same key never
                    run at the same time (design 5.2 owner_conflict)
        parent:     id of the request / program this task belongs to (progress board grouping)
        depends_on: task ids that must be DONE before this one can be claimed"""
        now = time.time()
        with self._tx():
            cur = self.db.execute(
                f"select * from tasks where kind=? and ref=? and status in ({','.join('?' * len(OPEN))})",
                (kind, ref, *OPEN)).fetchone()
            if cur:
                return {**_row(cur), "duplicate": True}
            deps = [d for d in dict.fromkeys(depends_on or ()) if d]
            for d in deps:
                if not self.db.execute("select 1 from tasks where id=?", (d,)).fetchone():
                    raise ValueError(f"depends_on: no task {d}")
            if parent and not self.db.execute("select 1 from tasks where id=?", (parent,)).fetchone():
                raise ValueError(f"parent: no task {parent}")
            tid = uuid.uuid4().hex[:12]
            self.db.execute("insert into tasks(id, kind, ref, title, status, priority, max_attempts, data, "
                            "created, updated, resource, parent) values (?,?,?,?,?,?,?,?,?,?,?,?)",
                            (tid, kind, ref, title, "queued", priority, max_attempts,
                             json.dumps(data or {}, ensure_ascii=False), now, now, resource or None,
                             parent or None))
            for d in deps:
                self.db.execute("insert into deps(task_id, depends_on) values (?,?)", (tid, d))
            self._event(tid, "added", task_kind=kind, ref=ref, depends_on=deps or None)
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

    def capacity(self) -> int:
        r = self.db.execute("select value from settings where key='max_running'").fetchone()
        try:
            return max(0, int(r["value"])) if r else 0
        except ValueError:
            return 1                                       # unreadable policy: the safest cap

    def set_capacity(self, n: int, by: str = "") -> int:
        n = max(0, int(n))
        with self._tx():
            self.db.execute("insert into settings(key, value) values ('max_running', ?) "
                            "on conflict(key) do update set value=excluded.value", (str(n),))
            self._event(None, "capacity", max_running=n, by=by or None)
        return n

    def backfill_resource(self, kind: str, resource_of) -> dict:
        """Give every OPEN, not-running task of `kind` without a resource its key, in ONE transaction
        (tasks created before `resource` existed — Codex F.7 R2 #3). Running ones are left alone: the
        claim barrier keeps keyed tasks of that kind off the floor until they end."""
        out = {"set": [], "running_without": []}
        with self._tx():
            rows = self.db.execute(
                f"select id, status, data from tasks where kind=? and coalesce(resource,'')='' "
                f"and status in ({','.join('?' * len(OPEN))})", (kind, *OPEN)).fetchall()
            for r in rows:
                if r["status"] == "running":
                    out["running_without"].append(r["id"])
                    continue
                key = resource_of(json.loads(r["data"] or "{}"))
                if key:
                    self.db.execute("update tasks set resource=?, updated=? where id=?", (key, time.time(), r["id"]))
                    self._event(r["id"], "resource_set", resource=key)
                    out["set"].append(r["id"])
        return out

    def running_count(self) -> int:
        return self.db.execute("select count(*) n from tasks where status='running' and lease_until>=?",
                               (time.time(),)).fetchone()["n"]

    def claim(self, owner: str, kinds: tuple = (), lease_s: int = DEFAULT_LEASE_S) -> dict:
        """Atomically take the next runnable task: queued, paused_quota whose time has come,
        or running with an expired lease (crashed holder). Highest priority (lowest number) first.
        Skipped: tasks with a dependency not DONE yet, and tasks whose `resource` is held by another
        live task, or of a kind that has a LIVE task without any resource (a legacy holder whose
        checkout is unknown). Capacity: the database's shared `capacity()` (all kinds, processes).
        The returned task carries `lease_token` — pass it to every later call."""
        now = time.time()
        kind_sql = f" and t.kind in ({','.join('?' * len(kinds))})" if kinds else ""
        with self._tx():
            cap = self.capacity()
            if cap and self.running_count() >= cap:
                return {}
            r = self.db.execute(
                "select * from tasks t where ("
                " t.status='queued'"
                " or (t.status='paused_quota' and coalesce(t.resume_at,0)<=?)"
                " or (t.status='running' and coalesce(t.lease_until,0)<?))" + kind_sql +
                " and not exists (select 1 from deps d join tasks x on x.id=d.depends_on"
                "                 where d.task_id=t.id and x.status!='done')"
                " and (coalesce(t.resource,'')='' or not exists (select 1 from tasks y where y.resource=t.resource"
                "      and y.id!=t.id and y.status='running' and coalesce(y.lease_until,0)>=?))"
                " and (coalesce(t.resource,'')='' or not exists (select 1 from tasks z where z.kind=t.kind"
                "      and coalesce(z.resource,'')='' and z.status='running' and coalesce(z.lease_until,0)>=?))"
                " order by t.priority, t.created limit 1", (now, now, *kinds, now, now)).fetchone()
            if not r:
                return {}
            prev = r["status"]
            token = uuid.uuid4().hex
            self.db.execute("update tasks set status='running', lease_owner=?, lease_token=?, lease_until=?, "
                            "resume_at=null, updated=? where id=?", (owner, token, now + lease_s, now, r["id"]))
            self._event(r["id"], "claimed", owner=owner, previous=prev,
                        reclaimed_from=r["lease_owner"] if prev == "running" else None)
            # snapshot INSIDE the transaction: reading after commit could return the token of a
            # worker that re-claimed while we stalled (Codex review round 2)
            claimed = _row(self.db.execute("select * from tasks where id=?", (r["id"],)).fetchone())
        return claimed

    def _held(self, task_id: str, lease: str, allow_expired: bool = False):
        r = self.db.execute("select status, lease_token, lease_until from tasks where id=?", (task_id,)).fetchone()
        if not r or r["status"] != "running" or not lease or r["lease_token"] != lease:
            raise LeaseLost(f"task {task_id}: lease is no longer held")
        if not allow_expired and (r["lease_until"] or 0) < time.time():
            raise LeaseLost(f"task {task_id}: lease expired")

    def holds(self, task_id: str, lease: str) -> bool:
        try:
            self._held(task_id, lease)
            return True
        except LeaseLost:
            return False

    def heartbeat(self, task_id: str, lease: str, lease_s: int = DEFAULT_LEASE_S):
        """Extend a LIVE lease. An expired lease is never revived, even if nobody re-claimed yet:
        meanwhile another task may have taken its resource / capacity slot (Codex F.7 #2) — the
        holder gets LeaseLost, stops (ctx.run kills its subprocess) and the task is claimed again
        through the normal checks."""
        with self._tx():
            self._held(task_id, lease)
            self.db.execute("update tasks set lease_until=?, updated=? where id=?",
                            (time.time() + lease_s, time.time(), task_id))

    # ------------------------------------------------------------------ checkpoints
    def checkpoint(self, task_id: str, lease: str, step: str, data: dict = None, sha: str = "",
                   input_key: str = ""):
        with self._tx():
            self._held(task_id, lease)
            now = time.time()
            self.db.execute("insert into checkpoints(task_id, step, data, sha, input_key, valid, ts) "
                            "values (?,?,?,?,?,1,?)",
                            (task_id, step, json.dumps(data or {}, ensure_ascii=False), sha or None,
                             input_key or None, now))
            self.db.execute("update tasks set step=?, updated=? where id=?", (step, now, task_id))
            self._event(task_id, "checkpoint", step=step, sha=sha or None, input_key=input_key or None)

    def checkpoints(self, task_id: str, valid_only: bool = False) -> list:
        q = "select * from checkpoints where task_id=?" + (" and valid=1" if valid_only else "") + " order by id"
        return [{**dict(r), "data": json.loads(r["data"] or "{}")} for r in self.db.execute(q, (task_id,))]

    def last_checkpoint(self, task_id: str) -> dict:
        cps = self.checkpoints(task_id, valid_only=True)
        return cps[-1] if cps else {}

    def invalidate(self, task_id: str, steps: list = None, reason: str = "") -> int:
        """Mark checkpoints invalid (all, or only `steps`) — e.g. the MR got a new revision.
        No lease needed: invalidating can only make work re-run, never skip it."""
        with self._tx():
            if steps:
                n = self.db.execute(f"update checkpoints set valid=0 where task_id=? and valid=1 and step in "
                                    f"({','.join('?' * len(steps))})", (task_id, *steps)).rowcount
            else:
                n = self.db.execute("update checkpoints set valid=0 where task_id=? and valid=1",
                                    (task_id,)).rowcount
            if n:
                self._event(task_id, "invalidated", steps=steps or "all", count=n, reason=reason or None)
        return n

    # ------------------------------------------------------------------ data / transitions
    def update_data(self, task_id: str, lease: str, **fields):
        with self._tx():
            self._held(task_id, lease)
            data = self.get(task_id)["data"]
            data.update(fields)
            self.db.execute("update tasks set data=?, updated=? where id=?",
                            (json.dumps(data, ensure_ascii=False), time.time(), task_id))

    def _finish(self, task_id, lease, status, event, error=None, **extra):
        with self._tx():
            self._held(task_id, lease, allow_expired=True)   # token match = nobody else took it
            sets = ["status=?", "lease_owner=null", "lease_token=null", "lease_until=null", "updated=?"]
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

    def pause_quota(self, task_id: str, lease: str, resume_at: float, backend: str = "", reason: str = ""):
        """Provider limit hit: stop now, resume later from the last valid checkpoint. Not a failure."""
        self._finish(task_id, lease, "paused_quota", "paused_quota", error=reason or None,
                     resume_at=float(resume_at), backend=backend or None)

    def block(self, task_id: str, lease: str, reason: str):
        """Needs a human (decision, missing info, budget exceeded)."""
        self._finish(task_id, lease, "blocked", "blocked", error=reason)

    def release(self, task_id: str, lease: str):
        self._finish(task_id, lease, "queued", "released")

    def complete(self, task_id: str, lease: str, **result):
        if result:
            self.update_data(task_id, lease, result=result)
        self._finish(task_id, lease, "done", "done")

    def fail(self, task_id: str, lease: str, error: str) -> str:
        """Count an attempt; requeue while attempts remain, else failed. Returns the new status."""
        with self._tx():
            self._held(task_id, lease, allow_expired=True)
            t = self.get(task_id)
        attempts = int(t.get("attempts") or 0) + 1
        status = "queued" if attempts < int(t.get("max_attempts") or 3) else "failed"
        self._finish(task_id, lease, status, "failed_attempt" if status == "queued" else "failed",
                     error=error, attempts=attempts)
        return status

    def unblock(self, task_id: str, by: str = ""):
        with self._tx():
            n = self.db.execute("update tasks set status='queued', updated=? where id=? and status='blocked'",
                                (time.time(), task_id)).rowcount
            if n:
                self._event(task_id, "unblocked", by=by)
        return bool(n)

    def resolve_blocked(self, task_id: str, status: str = "done", by: str = "", note: str = "",
                        if_updated: float = None, if_error_prefix: str = None) -> bool:
        """Close a BLOCKED task whose outcome happened elsewhere (e.g. a human approved and the
        sweeper posted). Only from blocked, only to done/cancelled — never steals a lease.
        if_updated / if_error_prefix: resolve only the exact revision the caller inspected (a task
        re-run and blocked for another reason meanwhile is left alone — Codex F.7 R2 #5)."""
        if status not in ("done", "cancelled"):
            raise ValueError("status must be done or cancelled")
        q, args = "update tasks set status=?, updated=? where id=? and status='blocked'", [status, time.time(), task_id]
        if if_updated is not None:
            q += " and updated=?"
            args.append(if_updated)
        if if_error_prefix is not None:
            q += " and substr(coalesce(last_error,''), 1, ?)=?"
            args += [len(if_error_prefix), if_error_prefix]
        with self._tx():
            n = self.db.execute(q, args).rowcount
            if n:
                self._event(task_id, "resolved", status=status, by=by, note=note or None)
        return bool(n)

    def set_resource(self, task_id: str, resource: str) -> bool:
        """Give a not-running task its exclusive key (upgrade of a task created before `resource`)."""
        with self._tx():
            n = self.db.execute("update tasks set resource=?, updated=? where id=? and status!='running' "
                                "and coalesce(resource,'')=''", (resource, time.time(), task_id)).rowcount
            if n:
                self._event(task_id, "resource_set", resource=resource)
        return bool(n)

    def deps_of(self, task_id: str) -> list:
        return [r["depends_on"] for r in self.db.execute("select depends_on from deps where task_id=?", (task_id,))]

    def children(self, parent: str) -> list:
        return [_row(r) for r in self.db.execute("select * from tasks where parent=? order by created", (parent,))]

    def block_orphans(self) -> list:
        """Queued / paused tasks that can never run because a dependency FAILED or was CANCELLED ->
        blocked (a human re-plans) instead of waiting forever. Returns the ids blocked."""
        out = []
        with self._tx():
            while True:                  # to a fixpoint: A fails -> B blocked -> C blocked -> ...
                rows = self.db.execute(
                    "select t.id, x.id dep, x.status dst from tasks t join deps d on d.task_id=t.id "
                    "join tasks x on x.id=d.depends_on where t.status in ('queued','paused_quota') "
                    "and (x.status in ('failed','cancelled') or (x.status='blocked' and x.last_error like ?))",
                    (DEP_BLOCK + "%",)).fetchall()
                fresh = [r for r in rows if r["id"] not in out]
                if not fresh:
                    return out
                for r in fresh:
                    if r["id"] in out:
                        continue
                    why = f"{DEP_BLOCK}{r['dep']} is {r['dst']}"
                    self.db.execute("update tasks set status='blocked', last_error=?, updated=? where id=? "
                                    "and status in ('queued','paused_quota')", (why, time.time(), r["id"]))
                    self._event(r["id"], "blocked", error=why)
                    out.append(r["id"])

    def cancel(self, task_id: str, by: str = ""):
        with self._tx():
            n = self.db.execute(
                f"update tasks set status='cancelled', lease_owner=null, lease_token=null, lease_until=null, "
                f"updated=? where id=? and status in ({','.join('?' * len(OPEN))})",
                (time.time(), task_id, *OPEN)).rowcount
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
