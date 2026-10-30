"""Group workboard state in the toolkit database (docs/design/group-workboard.md §3.2–§3.3).

The bot posts from the OWNER's FPT Chat account, so "a message from the owner's id" is either the owner
typing or the toolkit sending. Two tables tell them apart and keep commands from running twice:

  outbox  every message the toolkit sends is written BEFORE the socket opens (request id + content hash):
          a message from the owner's id that matches it is the bot's own — never a command.
  inbox   every group message seen, unique (group, message id): a restart / reconnect never runs a
          command twice; messages read back from history are recorded as `history` and never executed.

Same SQLite file as the task store (store.default_path()), so a subprocess sender (notify_group from
review_post) and the watcher see one outbox.
"""

import datetime
import hashlib
import json
import time

from . import store as _store

SCHEMA_VERSION = 1
MATCH_WINDOW_S = 120          # hash fallback: an echo within ±2 min of the outbox row (requestId missing)

SCHEMA = """
create table if not exists board_meta (key text primary key, value text);
create table if not exists outbox (
  request_id text primary key, group_id text not null, content_sha text not null, card_id text,
  state text not null, message_inc integer, error text, created real not null, updated real not null);
create index if not exists outbox_sha on outbox(group_id, content_sha, created);
create table if not exists inbox (
  group_id text not null, message_id text not null, message_inc integer, sender_id text, sender_name text,
  created_at real, received real not null, status text not null, card_id text, outcome text,
  primary key (group_id, message_id));
create index if not exists inbox_status on inbox(group_id, status);
"""
# A message seen once as `inc:N` and once by its id must be ONE row. Rows duplicated by an earlier build
# (keyed by id) are merged first — keep the most advanced status — or the unique index cannot be created.
_STATUS_RANK = ("received", "history", "ignored", "owner_chat", "bot", "blocked", "queued", "missed",
                "missed_shadow", "missed_reported", "expired", "error", "done")
INBOX_INC_UNIQUE = ("create unique index if not exists inbox_inc_u on inbox(group_id, message_inc) "
                    "where message_inc is not null")
# inbox statuses: received -> queued -> done | error            (live message acted on)
#                 bot | owner_chat | ignored | history | blocked  (never acted on)
#                 missed -> missed_reported | missed_shadow | expired  (a command the bot could not run:
#                 sent while it was down, stale, or interrupted by a crash — the sender is told to resend)
UNFINISHED = ("received", "queued")

# outbox states: sending -> sent | unconfirmed (no echo) | uncertain (died after transmitting) | failed
# (nothing left the machine). Only `failed` rows can never appear in the group.
OUTBOX_STATES = ("sending", "sent", "unconfirmed", "uncertain", "failed")


def content_sha(text: str) -> str:
    return hashlib.sha256((text or "").strip().encode("utf-8")).hexdigest()


def parse_ts(value) -> float:
    """createdAt of a chat message (ISO 8601, 'Z' or offset; or epoch s/ms) -> epoch seconds; 0 if unknown."""
    if value in (None, ""):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value) / (1000.0 if value > 1e11 else 1.0)
    try:
        s = str(value).strip().replace("Z", "+00:00")
        d = datetime.datetime.fromisoformat(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=datetime.timezone.utc)
        return d.timestamp()
    except ValueError:
        return 0.0


class Board:
    def __init__(self, path: str = ""):
        self.s = _store.Store(path)
        self.db = self.s.db
        with self.s._tx():
            for stmt in (x.strip() for x in SCHEMA.split(";")):
                if stmt:
                    self.db.execute(stmt)
            cur = self._meta("schema_version")
            if cur and int(cur) > SCHEMA_VERSION:
                raise RuntimeError(f"board schema {cur} is newer than this code ({SCHEMA_VERSION})")
            if "sender_name" not in {r["name"] for r in self.db.execute("pragma table_info(inbox)")}:
                self.db.execute("alter table inbox add column sender_name text")
            if not self.db.execute("select 1 from sqlite_master where name='inbox_inc_u'").fetchone():
                self._merge_inc_duplicates()
                self.db.execute(INBOX_INC_UNIQUE)
            self._set_meta("schema_version", str(SCHEMA_VERSION))

    def _merge_inc_duplicates(self) -> None:
        rank = {s: i for i, s in enumerate(_STATUS_RANK)}
        dups = self.db.execute("select group_id, message_inc from inbox where message_inc is not null "
                               "group by group_id, message_inc having count(*) > 1").fetchall()
        for d in dups:
            rows = [dict(r) for r in self.db.execute("select rowid, * from inbox where group_id=? and message_inc=?",
                                                     (d["group_id"], d["message_inc"]))]
            keep = max(rows, key=lambda r: rank.get(r["status"], -1))
            self.db.execute(f"delete from inbox where rowid in ({','.join('?' * (len(rows) - 1))})",
                            [r["rowid"] for r in rows if r["rowid"] != keep["rowid"]])

    def close(self):
        self.s.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ------------------------------------------------------------------ meta
    def _meta(self, key: str):
        r = self.db.execute("select value from board_meta where key=?", (key,)).fetchone()
        return r["value"] if r else None

    def _set_meta(self, key: str, value: str):
        self.db.execute("insert into board_meta(key, value) values (?, ?) "
                        "on conflict(key) do update set value=excluded.value", (key, value))

    def watermark(self, group_id: str):
        """Highest messageIdInc recorded for the group; None = never watched (first run)."""
        v = self._meta(f"watermark:{group_id}")
        return int(v) if v is not None else None

    def raise_watermark(self, group_id: str, inc) -> None:
        if inc is None:
            return
        with self.s._tx():
            cur = self.watermark(group_id)
            if cur is None or int(inc) > cur:
                self._set_meta(f"watermark:{group_id}", str(int(inc)))

    # ------------------------------------------------------------------ outbox
    def outbox_begin(self, group_id: str, content: str, request_id: str, card_id: str = "") -> None:
        now = time.time()
        with self.s._tx():
            self.db.execute("insert into outbox(request_id, group_id, content_sha, card_id, state, created, updated) "
                            "values (?,?,?,?, 'sending', ?, ?)",
                            (request_id, str(group_id), content_sha(content), card_id or None, now, now))

    def outbox_done(self, request_id: str, state: str, message_inc=None, error: str = "") -> None:
        if state not in OUTBOX_STATES:
            raise ValueError(f"outbox state {state!r}")
        with self.s._tx():
            self.db.execute("update outbox set state=?, message_inc=coalesce(?, message_inc), error=?, updated=? "
                            "where request_id=?",
                            (state, message_inc, (error or "")[:500] or None, time.time(), request_id))

    def outbox_get(self, request_id: str) -> dict:
        r = self.db.execute("select * from outbox where request_id=?", (request_id,)).fetchone()
        return dict(r) if r else {}

    def is_bot_message(self, group_id: str, request_id: str = "", content: str = "", created_at: float = 0.0,
                       message_inc=None) -> str:
        """'' when the message is not the toolkit's; else how it matched: 'request_id' | 'inc' | 'hash'.
        Raises on a database error — callers fail CLOSED (no owner command runs without the check)."""
        if request_id and self.db.execute("select 1 from outbox where request_id=? and state!='failed'",
                                          (request_id,)).fetchone():
            return "request_id"
        if message_inc is not None and self.db.execute(
                "select 1 from outbox where group_id=? and message_inc=?", (str(group_id), int(message_inc))).fetchone():
            return "inc"
        if content:
            at = created_at or time.time()
            if self.db.execute("select 1 from outbox where group_id=? and content_sha=? and state!='failed' "
                               "and created between ? and ?",
                               (str(group_id), content_sha(content), at - MATCH_WINDOW_S, at + MATCH_WINDOW_S)
                               ).fetchone():
                return "hash"
        return ""

    # ------------------------------------------------------------------ inbox
    def inbox_record(self, group_id: str, message_id: str, *, message_inc=None, sender_id: str = "",
                     sender_name: str = "", created_at: float = 0.0, status: str = "received") -> bool:
        """True = first time this message is seen (the caller may act on it); False = already recorded —
        by message id OR by (group, messageIdInc), so a message seen once as `inc:N` and once by its id
        is still one message."""
        with self.s._tx():
            n = self.db.execute("insert or ignore into inbox(group_id, message_id, message_inc, sender_id, "
                                "sender_name, created_at, received, status) values (?,?,?,?,?,?,?,?)",
                                (str(group_id), str(message_id), None if message_inc is None else int(message_inc),
                                 sender_id or None, sender_name or None, created_at or None, time.time(),
                                 status)).rowcount
            if n and message_inc is not None:
                cur = self.watermark(group_id)
                if cur is None or int(message_inc) > cur:
                    self._set_meta(f"watermark:{group_id}", str(int(message_inc)))
        return bool(n)

    def inbox_set(self, group_id: str, message_id: str, status: str, outcome: str = "", card_id: str = "") -> None:
        with self.s._tx():
            self.db.execute("update inbox set status=?, outcome=coalesce(?, outcome), card_id=coalesce(?, card_id) "
                            "where group_id=? and message_id=?",
                            (status, outcome[:500] or None, card_id or None, str(group_id), str(message_id)))

    def inbox_get(self, group_id: str, message_id: str) -> dict:
        r = self.db.execute("select * from inbox where group_id=? and message_id=?",
                            (str(group_id), str(message_id))).fetchone()
        return dict(r) if r else {}

    def inbox_by_status(self, group_id: str, statuses) -> list:
        q = (f"select * from inbox where group_id=? and status in ({','.join('?' * len(statuses))}) "
             f"order by coalesce(message_inc, 0), received")
        return [dict(r) for r in self.db.execute(q, (str(group_id), *statuses))]

    def inbox_mark_unfinished(self, group_id: str) -> int:
        """At start-up: messages recorded but never finished by the previous process (crash between
        recording and running) -> `missed` (reported, never re-run: they may have half-run)."""
        with self.s._tx():
            return self.db.execute(
                f"update inbox set status='missed', outcome='interrupted' where group_id=? and status in "
                f"({','.join('?' * len(UNFINISHED))})", (str(group_id), *UNFINISHED)).rowcount

    def inbox_counts(self, group_id: str) -> dict:
        return {r["status"]: r["n"] for r in self.db.execute(
            "select status, count(*) n from inbox where group_id=? group by status", (str(group_id),))}

    def event(self, kind: str, **data) -> None:
        """Audit line in the shared events table (task_id = null) — `taskstore.py events` shows it."""
        self.db.execute("insert into events(task_id, ts, kind, data) values (null, ?, ?, ?)",
                        (time.time(), f"board.{kind}", json.dumps(data, ensure_ascii=False)))
