"""Group workboard phase 0b (docs/design/group-workboard.md §3.2–§3.3): durable inbox, outbox written
BEFORE sending, inline mentions, heartbeat + supervise watchdog."""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".claude" / "skills" / "_runtime"))
from agent_skill_toolkit.core import board_store  # noqa: E402

TCHAT = ROOT / ".claude" / "skills" / "tchat-automation" / "tools"
DEV = ROOT / ".claude" / "skills" / "dev-automation" / "tools"


def _tmpdb():
    d = tempfile.mkdtemp(prefix="board-")
    return os.path.join(d, "toolkit.db")


class BoardStoreTest(unittest.TestCase):
    def setUp(self):
        self.path = _tmpdb()
        self.b = board_store.Board(self.path)
        self.addCleanup(self.b.close)

    def test_inbox_once(self):
        self.assertIsNone(self.b.watermark("G"))
        self.assertTrue(self.b.inbox_record("G", "m1", message_inc=5, sender_id="U"))
        self.assertFalse(self.b.inbox_record("G", "m1", message_inc=5, sender_id="U"))
        self.assertTrue(self.b.inbox_record("G2", "m1", message_inc=1))         # unique per group
        self.assertEqual(self.b.watermark("G"), 5)
        self.b.inbox_record("G", "m0", message_inc=3)
        self.assertEqual(self.b.watermark("G"), 5)                               # never lowered
        self.b.inbox_set("G", "m1", "done", "ok")
        self.assertEqual(self.b.inbox_get("G", "m1")["status"], "done")
        # durable: a new handle (restart) sees the same rows
        b2 = board_store.Board(self.path)
        self.addCleanup(b2.close)
        self.assertFalse(b2.inbox_record("G", "m1"))

    def test_outbox_match(self):
        now = time.time()
        self.b.outbox_begin("G", "🤖 [R-1] etask!12", "req-1")
        self.assertEqual(self.b.outbox_get("req-1")["state"], "sending")
        self.assertEqual(self.b.is_bot_message("G", "req-1"), "request_id")
        self.assertEqual(self.b.is_bot_message("G", "", "🤖 [R-1] etask!12", now), "hash")
        self.assertEqual(self.b.is_bot_message("G", "", "🤖 [R-1] etask!12", now + 600), "")   # outside ±2 min
        self.assertEqual(self.b.is_bot_message("G2", "", "🤖 [R-1] etask!12", now), "")       # other group
        self.assertEqual(self.b.is_bot_message("G", "", "@bot review etask!12", now), "")
        self.b.outbox_done("req-1", "sent", message_inc=77)
        self.assertEqual(self.b.is_bot_message("G", "", "", 0, 77), "inc")
        self.b.outbox_begin("G", "never left", "req-2")
        self.b.outbox_done("req-2", "failed", error="connect")
        self.assertEqual(self.b.is_bot_message("G", "req-2", "never left", now), "")   # failed = not sent
        with self.assertRaises(ValueError):
            self.b.outbox_done("req-1", "bogus")

    def test_newer_schema_refused(self):
        with self.b.s._tx():
            self.b._set_meta("schema_version", "99")
        with self.assertRaises(RuntimeError):
            board_store.Board(self.path)

    def test_parse_ts(self):
        self.assertAlmostEqual(board_store.parse_ts("2026-10-30T01:02:03.000Z"), 1793322123.0, places=0)
        self.assertEqual(board_store.parse_ts(1793322123000), 1793322123.0)
        self.assertEqual(board_store.parse_ts("garbage"), 0.0)

    def test_migrates_duplicate_inc_rows(self):
        """An earlier build keyed the inbox by message id: the same inc twice must merge, not crash."""
        import sqlite3
        path = _tmpdb()
        db = sqlite3.connect(path)
        db.executescript("create table inbox (group_id text not null, message_id text not null, message_inc integer, "
                         "sender_id text, created_at real, received real not null, status text not null, card_id text, "
                         "outcome text, primary key (group_id, message_id));"
                         "insert into inbox values ('G','inc:5',5,'U',0,1,'queued',null,null);"
                         "insert into inbox values ('G','real-id',5,'U',0,2,'done',null,null);"
                         "insert into inbox values ('G','inc:6',6,'U',0,3,'history',null,null);")
        db.commit()
        db.close()
        b = board_store.Board(path)
        self.addCleanup(b.close)
        rows = b.inbox_by_status("G", ("queued", "done", "history"))
        self.assertEqual(sorted((r["message_inc"], r["status"]) for r in rows), [(5, "done"), (6, "history")])
        self.assertFalse(b.inbox_record("G", "inc:5", message_inc=5))          # unique by inc from now on
        self.assertTrue(b.inbox_record("G", "x", message_inc=7, sender_name="Lan"))


def _run_tchat(code: str, db: str, timeout=60) -> dict:
    env = dict(os.environ, TOOLKIT_STORE=db, TOOLKIT_STATE_DIR=os.path.dirname(db),
               WORK_DIR=os.path.dirname(db), PYTHONIOENCODING="utf-8")
    p = subprocess.run([sys.executable, "-c", code], cwd=str(TCHAT), env=env, capture_output=True,
                       text=True, encoding="utf-8", timeout=timeout)
    if p.returncode != 0:
        raise AssertionError(p.stderr[-2000:])
    return json.loads(p.stdout.strip().splitlines()[-1])


SEND_CODE = r'''
import json, sys
import send, ws_client, tokens, config
from agent_skill_toolkit.core import board_store
seen = {}
class WS:
    def __init__(self, *a, **k):
        if MODE == "connect_fail":
            raise OSError("refused")
    def send_text(self, s):
        f = json.loads(s)["data"]
        b = board_store.Board()
        seen["state_at_send"] = b.outbox_get(f["requestId"]).get("state")
        b.close()
        self.req = f["requestId"]
        self.n = 0
    def recv(self):
        self.n += 1
        if MODE == "echo" and self.n == 2:
            return json.dumps({"type": "message", "data": {"requestId": self.req, "messageIdInc": 4242}})
        return None if self.n > 3 else "pong"
    def close(self): pass
ws_client.WebSocket = WS
tokens.ensure_fresh = lambda: "t"
config.ws_url = lambda: "wss://x"; config.web_origin = lambda: "o"; config.verify_ssl = lambda: True
out = {}
for MODE in ("echo", "noecho", "connect_fail"):
    try:
        r = send.send_text("G", "hello " + MODE, "SUPER_PRIVATE", sender_id="ME", request_id="rq-" + MODE)
    except OSError as e:
        r = {"raised": str(e)}
    b = board_store.Board()
    out[MODE] = {"res": {k: r.get(k) for k in ("confirmed", "raised", "error")},
                 "row": b.outbox_get("rq-" + MODE), "at_send": seen.pop("state_at_send", None)}
    b.close()
print(json.dumps(out))
'''


class SendOutboxTest(unittest.TestCase):
    def test_outbox_before_send_subprocess(self):
        db = _tmpdb()
        code = ("import runpy; from pathlib import Path; runpy.run_path(str(Path('send.py').resolve().parents[2] / "
                "'_runtime' / 'bootstrap.py'))['activate']()\n" + SEND_CODE)
        out = _run_tchat(code, db)
        self.assertEqual(out["echo"]["at_send"], "sending")                # written BEFORE transmitting
        self.assertEqual(out["echo"]["row"]["state"], "sent")
        self.assertEqual(out["echo"]["row"]["message_inc"], 4242)
        self.assertEqual(out["noecho"]["row"]["state"], "unconfirmed")
        self.assertEqual(out["connect_fail"]["row"]["state"], "failed")
        self.assertEqual(out["connect_fail"]["res"]["raised"], "refused")
        # the watcher (another process / handle) sees what the sender subprocess wrote
        b = board_store.Board(db)
        self.addCleanup(b.close)
        self.assertEqual(b.is_bot_message("G", "", "hello echo", time.time()), "hash")
        self.assertEqual(b.is_bot_message("G", "", "hello connect_fail", time.time()), "")

    def test_no_outbox_no_send_and_mentions(self):
        out = _run_tchat(PURE_CODE, _tmpdb())
        self.assertTrue(out["no_outbox"]["error"])
        self.assertIn("not sent", out["no_outbox"]["message"])
        self.assertEqual(out["ws_opened"], 0)
        text, ms = out["text"], out["mentions"]
        self.assertEqual([m["userId"] for m in ms], ["U2", "U1"])
        for m in ms:
            self.assertEqual(text[m["offset"]:m["offset"] + m["length"]], "@" + m["target"])
        self.assertEqual(out["none"], ["không ai", {}])


PURE_CODE = r'''
import json
import send
opened = []
send._outbox = lambda: (_ for _ in ()).throw(OSError("disk"))
send.ws_client.WebSocket = lambda *a, **k: opened.append(1)
r = send.send_text("G", "x", "SUPER_PRIVATE", sender_id="ME")
text = "🤖 [R-7] etask!12 · Người review: @Anh Tuan · Người tạo: @Anh"
_, meta = send.with_mentions(text, [("Anh", "U1"), ("Anh Tuan", "U2"), ("Ghost", "U3"), ("", "U4")])
print(json.dumps({"no_outbox": r, "ws_opened": len(opened), "text": text, "mentions": meta["mentions"],
                  "none": list(send.with_mentions("không ai", [("A", "U")]))}, ensure_ascii=False))
'''


WATCH_CODE = r'''
import json, sys, time
sys.argv = ["x"]
import group_watch as g
from agent_skill_toolkit.core import board_store
g._board = board_store.Board()
queued, said = [], []
g._work_q.put = lambda item: queued.append(item[0][4])
FAIL = {"send": False}
def fake_send(gid, text, gt=None, **k):
    if FAIL["send"]:
        return {"error": True, "message": "boom"}
    said.append(text)
    return {"sent": True, "confirmed": True}
g.send.send_text = fake_send
cfg = {"FCHAT_CATCHUP_REPLY": "1"}
g.rccfg.get = lambda k, d="": cfg.get(k, d)
kws = g._keywords()
now = time.time()
iso = lambda t: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))
def raw(mid, inc, sender, content, at=None, req="", name=None):
    d = {"type": "TEXT", "groupId": "G", "messageIdInc": inc, "senderId": sender, "content": content,
         "createdAt": iso(at or now), "requestId": req, "user": {"displayName": name or sender}}
    if mid:
        d["id"] = mid
    return d
msg = lambda *a, **k: {"type": "message", "data": raw(*a, **k)}
st = lambda inc, gid="G": g._board.inbox_get(gid, "inc:%d" % inc).get("status")
out = {}
g._board.outbox_begin("G", "🤖 Đã nhận yêu cầu review etask!1", "rq1")
g._handle(msg("a", 1, "ME", "🤖 Đã nhận yêu cầu review etask!1"), "G", "ME", kws)   # the bot's own echo
g._handle(msg("b", 2, "ME", "review MR etask!2 giúp"), "G", "ME", kws)            # owner chat, no prefix
g._handle(msg("c", 3, "ME", "@bot review etask!3"), "G", "ME", kws)               # owner command
g._handle(msg("c", 3, "ME", "@bot review etask!3"), "G", "ME", kws)               # duplicate delivery
g._handle(msg(None, 3, "ME", "@bot review etask!3"), "G", "ME", kws)              # same inc, no id
g._handle(msg("d", 4, "U1", "review giúp etask!4"), "G", "ME", kws)               # member
g._handle(msg("e", 5, "U1", "trưa nay ăn gì"), "G", "ME", kws)
g._handle(msg("f", 6, "U1", "review giúp etask!6", at=now - 3600, name="Hà"), "G", "ME", kws)   # stale
out["status"] = {i: st(i) for i in range(1, 7)}
out["queued"] = list(queued)
out["said_stale"] = list(said)
# outbox unreadable -> owner commands blocked (fail closed)
real = g._board.is_bot_message
g._board.is_bot_message = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db"))
queued.clear()
g._handle(msg("h", 8, "ME", "@bot review etask!8"), "G", "ME", kws)
out["blocked"] = [st(8), list(queued)]
g._board.is_bot_message = real
# catch-up after a restart: history is recorded, NEVER executed; missed commands reported once.
# The API pages around messageIdInc (X-L+1 .. X+L): the command at inc 20 sits two pages back.
said.clear(); queued.clear()
g.CATCHUP_PAGE = 3
hist = [raw("c", 3, "ME", "@bot review etask!3")] + \
       [raw("x%d" % i, i, "U2", "chuyện phiếm %d" % i) for i in range(9, 30) if i != 20] + \
       [raw("x20", 20, "U2", "review giúp etask!20", at=now - 600, name="Lan"),
        raw("x22", 30, "U3", "review etask!22", at=now - 13 * 3600)]
calls = []
def api(path, params=None):
    calls.append(dict(params or {}))
    L, X = params["limit"], params.get("messageIdInc")
    rows = sorted(hist, key=lambda m: m["messageIdInc"])
    sel = rows[-L:] if X is None else [m for m in rows if X - L + 1 <= m["messageIdInc"] <= X + L]
    return {"data": {"regulars": sel}}
g.client.api_get = api
FAIL["send"] = True
g._catch_up("G", "ME", kws)
out["fail"] = {"said": list(said), "x20": st(20)}
FAIL["send"] = False
g._catch_up("G", "ME", kws)                                      # retried on the next pass
out["catchup"] = {"said": list(said), "queued": list(queued), "x20": st(20), "x21": st(21), "x30": st(30),
                  "pages": len(calls)}
said.clear()
g._catch_up("G", "ME", kws)
out["catchup2"] = len(said)
# crash between recording and running: the next process reports it, never re-runs it
g._board.inbox_record("G", "inc:40", message_inc=40, sender_id="U1", sender_name="Minh", created_at=now)
g._board.inbox_set("G", "inc:40", "queued")
b2 = board_store.Board()
out["unfinished"] = b2.inbox_mark_unfinished("G")
said.clear()
g._flush_missed("G")
out["crash"] = [st(40), list(said)]
# a failed report is retried on a timer (not only on reconnect), with backoff
g._board.inbox_record("G", "inc:50", message_inc=50, sender_id="U1", sender_name="Khoa", created_at=now, status="missed")
said.clear()
FAIL["send"] = True
r = g._MissedRetry()
r.tick("G", now=1000.0)
d1 = r.delay
r.tick("G", now=1001.0)                                          # not due yet: no attempt
FAIL["send"] = False
r.tick("G", now=1000.0 + d1)
out["retry"] = [d1, r.delay, st(50), len(said)]
# first run ever (no watermark): record only, no report
said.clear()
hist[:] = [raw("y", 9, "U2", "review etask!9")]
g._catch_up("G-new", "ME", kws)
out["first_run"] = [len(said), st(9, "G-new")]
print(json.dumps(out, ensure_ascii=False))
'''


class GroupWatchInboxTest(unittest.TestCase):
    def test_handle_and_catch_up(self):
        out = _run_tchat(WATCH_CODE, _tmpdb())
        s = out["status"]
        self.assertEqual(s, {"1": "bot", "2": "owner_chat", "3": "queued", "4": "queued", "5": "ignored",
                             "6": "missed_reported"})
        self.assertEqual(out["queued"], ["review etask!3", "review giúp etask!4"])   # once each (id or inc)
        self.assertEqual(len(out["said_stale"]), 1)
        self.assertIn("Hà", out["said_stale"][0])
        self.assertEqual(out["blocked"], ["blocked", []])
        self.assertEqual(out["fail"], {"said": [], "x20": "missed"})                 # kept for retry
        cu = out["catchup"]
        self.assertEqual(cu["queued"], [])
        self.assertEqual(len(cu["said"]), 1)
        self.assertIn("Lan", cu["said"][0])
        self.assertNotIn("U3", cu["said"][0])                    # older than 12 h: not reported
        self.assertEqual([cu["x20"], cu["x21"], cu["x30"]], ["missed_reported", "history", "history"])
        self.assertGreater(cu["pages"], 2)                       # paged back to the watermark
        self.assertEqual(out["catchup2"], 0)
        self.assertEqual(out["unfinished"], 3)                   # inc 3, 4 (no worker in this probe) + 40
        self.assertEqual(out["crash"][0], "missed_reported")
        self.assertIn("Minh", out["crash"][1][0])
        self.assertIn("chạy dở", out["crash"][1][0])
        self.assertEqual(out["first_run"], [0, "history"])
        self.assertEqual(out["retry"], [120.0, 60.0, "missed_reported", 1])


class HeartbeatTest(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(DEV))
        import daemon_common
        import supervise
        self.dc, self.sv = daemon_common, supervise
        d = tempfile.mkdtemp(prefix="hb-")
        p = mock.patch.object(daemon_common, "_repo_root", return_value=d)
        p.start()
        self.addCleanup(p.stop)

    def test_beat_and_stale(self):
        self.assertEqual(self.dc.last_heartbeat("gw"), {})
        self.assertIsNone(self.sv._stale("gw", 180, 10))                 # startup grace
        self.assertIn("chưa từng ghi", self.sv._stale("gw", 180, 200))
        self.dc.heartbeat("gw", group="G")
        hb = self.dc.last_heartbeat("gw")
        self.assertEqual(hb["group"], "G")
        self.assertLess(hb["age"], 5)
        self.assertIsNone(self.sv._stale("gw", 180, 200))
        with mock.patch.object(self.dc.time, "time", return_value=time.time() + 400):
            self.assertIn("heartbeat dừng", self.sv._stale("gw", 180, 600))
        self.assertIsNone(self.sv._stale("", 180, 600))                  # watchdog off


if __name__ == "__main__":
    unittest.main()
