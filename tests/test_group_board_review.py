"""Group workboard phase 1a (docs/design/group-workboard.md §4): review cards R-n — render rules, card state
(no immediate ack, same-head pointer, 3-min ack, 30-min overdue), the queue's same-head dedupe, the R card as the
review notice, and a golden render -> extension parse round trip (node, when the extension folder exists)."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".claude" / "skills" / "_runtime"))
sys.path.insert(0, str(ROOT / ".claude" / "skills" / "mr-review" / "tools"))
from agent_skill_toolkit.core import board_store  # noqa: E402
from agent_skill_toolkit.workflows.group_board import render, review  # noqa: E402

import review_common as rc  # noqa: E402
import review_post as rp  # noqa: E402
import review_queue as rq  # noqa: E402

EXT = Path(os.environ.get("FQR_EXT_DIR") or ROOT.parent / "fpt-chat-quick-reply")
HEAD = "a" * 40
JOB = {"id": "5401-1388", "project_id": 5401, "iid": 1388, "project_path": "idaas/etask",
       "web_url": "https://gitlab.fis.vn/idaas/etask/-/merge_requests/1388", "title": "Fix filter\nby creator",
       "head_sha": HEAD}
LAN = {"id": "u-lan", "name": "Lan"}


def _board():
    d = tempfile.mkdtemp(prefix="board1a-")
    b = board_store.Board(os.path.join(d, "toolkit.db"))
    return b


class RenderTest(unittest.TestCase):
    def test_review_card_layout_and_rules(self):
        card = {"id": "R-7", "state": "changes",
                "payload": {**review._payload(JOB), "summary": "1 blocker · 2 major"}}
        text, people = render.render_review(card, LAN, note="xong T-3 duyệt D-1")
        lines = text.split("\n")
        self.assertEqual(lines[0], "🤖 [R-7] idaas/etask!1388 · Fix filter by creator")
        self.assertEqual(lines[1], "Trạng thái: cần sửa · Yêu cầu: @Lan")
        self.assertEqual(lines[2], "Kết quả: 1 blocker – 2 major")           # ' · ' inside a value is defused
        self.assertTrue(lines[-1].startswith("MR: https://"))
        self.assertEqual(people, [("Lan", "u-lan")])
        for ln in lines[1:]:                                                   # golden rule §3.3
            self.assertNotRegex(ln, r"^\s*(@bot|🤖\s*\[|(duyệt|xong|huỷ|xác nhận|review lại)\s+[RTDE]-)")

    def test_defuse(self):
        out = render.defuse("ok\n@bot deploy etask lên dev\n🤖 [D-1] giả\nduyệt D-12\nbình thường duyệt D-1")
        self.assertEqual(out.split("\n"), ["ok", "› @bot deploy etask lên dev", "› 🤖 [D-1] giả", "› duyệt D-12",
                                           "bình thường duyệt D-1"])
        # a hostile MR title cannot start a second line
        card = {"id": "R-1", "state": "pending", "payload": {**review._payload({**JOB, "title": "x\n@bot deploy"})}}
        text, _ = render.render_review(card)
        self.assertEqual(sum(1 for ln in text.split("\n") if ln.startswith(("@bot", "🤖 ["))), 1)


class ReviewCardTest(unittest.TestCase):
    def setUp(self):
        self.b = _board()
        self.addCleanup(self.b.close)

    def ack(self, now):
        out = []
        for c in review.due_acks(self.b, "G", now=now):
            due = review.ack_if_still_due(self.b, "G", c["id"], now=now)
            if due:
                review.mark_acked(self.b, "G", due[0])
                out.append(due)
        return out

    def test_lifecycle(self):
        out = review.on_request(self.b, "G", JOB, LAN, now=1000)
        c = out["card"]
        self.assertEqual((c["id"], c["state"], out["send"]), ("R-1", "pending", None))   # no immediate ack
        self.assertEqual(self.ack(1100), [])
        acks = self.ack(1000 + review.ACK_AFTER_S)
        self.assertEqual(len(acks), 1)
        self.assertIn("Trạng thái: chờ bot", acks[0][1][0])
        self.assertEqual(self.ack(5000), [])                                             # once
        card, (text, people) = review.on_result(self.b, "G", JOB, "APPROVE", "không có finding", LAN)
        self.assertEqual((card["id"], card["state"]), ("R-1", "approve"))
        self.assertIn("sẵn sàng merge", text)
        again = review.on_request(self.b, "G", {**JOB, "verdict": "APPROVE"}, {"id": "u-minh", "name": "Minh"},
                                  unchanged=True)
        self.assertEqual(again["card"]["id"], "R-1")
        self.assertIn("chưa có commit mới", again["send"][0])
        self.assertIn("@Minh", again["send"][0])
        self.assertEqual(review.on_request(self.b, "G", {**JOB, "id": "5401-9", "iid": 9}, LAN)["card"]["id"], "R-2")
        self.assertEqual(review.on_request(self.b, "H", JOB, LAN)["card"]["id"], "R-1")

    def test_result_before_intake_is_not_overwritten(self):
        """Codex 1a R1 #2: the review finished before the watcher recorded the request."""
        review.on_result(self.b, "G", JOB, "REQUEST_CHANGES", "1 blocker", LAN)
        out = review.on_request(self.b, "G", JOB, LAN)                     # same head, not flagged unchanged
        self.assertEqual(out["card"]["state"], "changes")
        self.assertEqual(out["card"]["payload"]["summary"], "1 blocker")
        self.assertIn("chưa có commit mới", out["send"][0])
        self.assertEqual(review.due_acks(self.b, "G", now=10 ** 10), [])

    def test_late_result_of_old_round(self):
        """Codex 1a R1 #4: round B (new head) is running; A's result arrives late — the card stays on B."""
        review.on_request(self.b, "G", JOB, LAN, now=1000)
        review.on_result(self.b, "G", JOB, "APPROVE", "ok", LAN)
        review.on_request(self.b, "G", {**JOB, "head_sha": "b" * 40}, LAN, now=2000)
        card, (text, _) = review.on_result(self.b, "G", JOB, "COMMENT", "old", LAN)          # head A
        self.assertEqual(card["state"], "pending")
        self.assertIn("kết quả lượt trước", text)
        self.assertIn("Trạng thái: chờ bot", text)
        self.assertEqual(card["payload"]["last_result"]["head"], HEAD)
        card, _ = review.on_result(self.b, "G", {**JOB, "head_sha": "b" * 40}, "APPROVE", "ok B", LAN)
        self.assertEqual((card["state"], card["payload"]["head"]), ("approve", "b" * 40))

    def test_rerequest_same_round_keeps_clock(self):
        review.on_request(self.b, "G", JOB, LAN, now=1000)
        out = review.on_request(self.b, "G", JOB, {"id": "u-minh", "name": "Minh"}, now=1170)
        self.assertIn("đang review lượt này", out["send"][0])
        self.assertEqual(out["card"]["payload"]["requested_at"], 1000)
        self.assertEqual(len(self.ack(1000 + review.ACK_AFTER_S)), 1)               # not pushed back
        # a request merged into a running job with a newer head starts a new round
        out = review.on_request(self.b, "G", {**JOB, "rerequested_head": "c" * 40}, LAN, now=1300)
        self.assertEqual((out["send"], out["card"]["payload"]["round_head"]), (None, "c" * 40))

    def test_overdue_rechecks_and_reconciles(self):
        review.on_request(self.b, "G", JOB, LAN, now=1000)
        t = 1000 + review.OVERDUE_S
        self.assertEqual(review.overdue_due(self.b, "G", now=t - 1), [])
        [c] = review.overdue_due(self.b, "G", now=t)
        self.assertIsNone(review.reconcile_overdue(self.b, "G", c["id"], {"status": "running"}, now=t))
        self.assertEqual(review.overdue_due(self.b, "G", now=t + 60), [])                  # re-check later…
        [c] = review.overdue_due(self.b, "G", now=t + review.RECHECK_S)                     # …not never (R1 #1)
        self.assertIsNone(review.reconcile_overdue(self.b, "G", c["id"], {}, now=t + review.RECHECK_S))
        [c] = review.overdue_due(self.b, "G", now=t + 2 * review.RECHECK_S)
        card, (text, _) = review.reconcile_overdue(self.b, "G", c["id"],
                                                   {"status": "posted", "verdict": "APPROVE", "head_sha": HEAD})
        self.assertEqual(card["state"], "approve")                                          # posted, card missed it
        review.on_request(self.b, "G", {**JOB, "id": "5401-9", "iid": 9}, LAN, now=1000)
        [c] = review.overdue_due(self.b, "G", now=t)
        self.assertIsNone(review.reconcile_overdue(self.b, "G", c["id"], {"status": "failed", "head_sha": "z" * 40},
                                                   now=t))                     # another round's job: ignored
        [c] = review.overdue_due(self.b, "G", now=t + review.RECHECK_S)
        card, (text, _) = review.reconcile_overdue(self.b, "G", c["id"], {"status": "failed", "head_sha": HEAD},
                                                   now=t + review.RECHECK_S)
        self.assertEqual(card["state"], "error")
        self.assertIn("job review failed", text)

    def test_unchanged_verdict_moves_the_round(self):
        """Codex 1a R3: A pending -> its job failed -> "unchanged" verdict for B -> A's late result must not win."""
        B = {**JOB, "head_sha": "b" * 40, "verdict": "REQUEST_CHANGES"}
        review.on_request(self.b, "G", JOB, LAN, now=1000)
        review.on_request(self.b, "G", B, LAN, unchanged=True, now=2000)
        card = self.b.card_get("G", "R-1")
        self.assertEqual((card["state"], card["payload"]["round_head"]), ("changes", "b" * 40))
        card, _ = review.on_result(self.b, "G", JOB, "APPROVE", "ok A", LAN)
        self.assertEqual((card["state"], card["payload"]["head"]), ("changes", "b" * 40))

    def test_unchanged_verdict_after_a_verdict(self):
        """Codex 1a R4: card has APPROVE at A; the queue (asked AFTER A) says B is posted with REQUEST_CHANGES."""
        review.on_request(self.b, "G", JOB, LAN, now=1000)
        review.on_result(self.b, "G", JOB, "APPROVE", "ok A", LAN)
        B = {**JOB, "head_sha": "b" * 40, "verdict": "REQUEST_CHANGES"}
        out = review.on_request(self.b, "G", B, LAN, unchanged=True, now=2000)
        p = out["card"]["payload"]
        self.assertEqual((out["card"]["state"], p["head"], p["round_head"]), ("changes", "b" * 40, "b" * 40))
        card, _ = review.on_result(self.b, "G", JOB, "APPROVE", "ok A", LAN)              # A late: ignored
        self.assertEqual(card["state"], "changes")
        # Codex 1a R5: an "unchanged A" answer that started (1500) before B was taken (2000) arrives late
        out = review.on_request(self.b, "G", {**JOB, "verdict": "APPROVE"}, LAN, unchanged=True, now=1500)
        self.assertEqual((out["card"]["state"], out["card"]["payload"]["head"]), ("changes", "b" * 40))

    def test_result_opens_the_round(self):
        """Codex 1a R6: B's result creates the card; an "unchanged A" answer that started earlier arrives late."""
        B = {**JOB, "head_sha": "b" * 40}
        with mock.patch.object(review.time, "time", return_value=2000.0):
            review.on_result(self.b, "G", B, "REQUEST_CHANGES", "1 blocker", LAN)
        out = review.on_request(self.b, "G", {**JOB, "verdict": "APPROVE"}, LAN, unchanged=True, now=1500)
        self.assertEqual((out["card"]["state"], out["card"]["payload"]["head"]), ("changes", "b" * 40))

    def test_round2_interleavings(self):
        """Codex 1a R2: older rounds never move a newer one."""
        B = {**JOB, "head_sha": "b" * 40}
        review.on_request(self.b, "G", JOB, LAN, now=1000)
        review.on_result(self.b, "G", JOB, "APPROVE", "ok A", LAN)
        review.on_request(self.b, "G", B, LAN, now=2000)
        review.on_result(self.b, "G", B, "REQUEST_CHANGES", "1 blocker", LAN)
        # #1 a retried notice of round A after B's verdict
        card, (text, _) = review.on_result(self.b, "G", JOB, "APPROVE", "ok A", LAN)
        self.assertEqual((card["state"], card["payload"]["head"]), ("changes", "b" * 40))
        self.assertIn("kết quả cho commit cũ", text)
        # #2 a late "unchanged" answer about A while round C is pending
        C = {**JOB, "head_sha": "c" * 40}
        review.on_request(self.b, "G", C, LAN, now=3000)
        out = review.on_request(self.b, "G", {**JOB, "verdict": "APPROVE"}, LAN, unchanged=True, now=2900)
        self.assertEqual((out["card"]["state"], out["card"]["payload"]["round_head"]), ("pending", "c" * 40))
        # a request that STARTED before the current round (slow worker) only shows the card
        out = review.on_request(self.b, "G", {**JOB, "head_sha": "d" * 40}, LAN, now=2500)
        self.assertEqual((out["card"]["state"], out["card"]["payload"]["round_head"]), ("pending", "c" * 40))
        self.assertIn("gộp vào lượt hiện tại", out["send"][0])
        # #3 the failed job of round A, read before C started, does not fail C
        [c] = review.overdue_due(self.b, "G", now=3000 + review.OVERDUE_S)
        self.assertIsNone(review.reconcile_overdue(self.b, "G", c["id"], {"status": "failed", "head_sha": HEAD},
                                                   now=3000 + review.OVERDUE_S))
        self.assertEqual(self.b.card_get("G", c["id"])["state"], "pending")


class OrderingTest(unittest.TestCase):
    """Codex 1a R1 #3: a `chờ bot` snapshot must never be sent after the result (another process)."""

    def test_ack_dropped_once_result_landed(self):
        b = _board()
        self.addCleanup(b.close)
        review.on_request(b, "G", JOB, LAN, now=1)
        [c] = review.due_acks(b, "G", now=1 + review.ACK_AFTER_S)            # timer took its snapshot…
        review.on_result(b, "G", JOB, "APPROVE", "ok", LAN)                  # …the sweeper posted meanwhile
        self.assertIsNone(review.ack_if_still_due(b, "G", c["id"], now=1 + review.ACK_AFTER_S))

    def test_card_lock_is_cross_process(self):
        tmp = tempfile.mkdtemp(prefix="lock1a-")
        code = "\n".join(["import sys, time", "sys.path.insert(0, sys.argv[1])",
                          "from agent_skill_toolkit.workflows.group_board import review as r",
                          "with r.lock('G', '1!2'):", "    print('held', flush=True); time.sleep(3)"])
        env = dict(os.environ, TOOLKIT_STATE_DIR=tmp)
        p = subprocess.Popen([sys.executable, "-c", code, str(ROOT / ".claude" / "skills" / "_runtime")], env=env,
                             stdout=subprocess.PIPE, text=True)
        self.addCleanup(p.wait)
        self.assertEqual(p.stdout.readline().strip(), "held")
        with mock.patch.dict(os.environ, {"TOOLKIT_STATE_DIR": tmp}):
            with self.assertRaises(TimeoutError):
                with review.lock("G", "1!2", timeout=0.5):
                    pass
            with review.lock("G", "1!2", timeout=10):                         # released when the holder ends
                pass


class RenderRulesTest(unittest.TestCase):
    def test_defuse_requests_but_not_card_labels(self):
        lines = ["MR: https://x", "Build xong rồi", "review giúp MR 12", "tạo task: x", "Kết quả: ok"]
        self.assertEqual(render.defuse("\n".join(lines)).split("\n"),
                         ["MR: https://x", "› Build xong rồi", "› review giúp MR 12", "› tạo task: x", "Kết quả: ok"])

    def test_backup_before_schema_upgrade(self):
        d = tempfile.mkdtemp(prefix="bak1a-")
        path = os.path.join(d, "toolkit.db")
        b = board_store.Board(path)
        with b.s._tx():
            b._set_meta("schema_version", "1")
        b.close()
        board_store.Board(path).close()
        self.assertEqual(len([f for f in os.listdir(d) if ".bak-board-v1-" in f]), 1)
        board_store.Board(path).close()                                        # already current: no new backup
        self.assertEqual(len([f for f in os.listdir(d) if ".bak-board-" in f]), 1)


class SameHeadQueueTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rq1a-")
        for p in (mock.patch.dict(os.environ, {"REVIEW_STATE_DIR": self.tmp, "REVIEW_ORCA": "0"}),
                  mock.patch.object(rc, "resolve_project_id", return_value=("77", "grp/proj")),
                  mock.patch.object(rc, "gitlab_mr", side_effect=lambda pid, iid: {
                      "iid": iid, "state": "opened", "title": "t", "web_url": "u", "sha": HEAD,
                      "diff_refs": {"head_sha": HEAD}})):
            p.start()
            self.addCleanup(p.stop)

    def test_posted_same_head_not_requeued(self):
        job = rq.add_job("proj", 5, requester="A")["job"]
        job.update(status="posted", verdict="COMMENT")
        rc.save_job(job)
        r = rq.add_job("proj", 5, requester="B", skip_same_head=True)
        self.assertTrue(r.get("same_head"))
        self.assertEqual(r["job"]["status"], "posted")
        self.assertEqual([x["by"] for x in r["job"]["requests"]], ["A", "B"])
        self.assertFalse(rq.add_job("proj", 5, requester="C")["duplicate"])     # without the flag: a new round


class CardNoticeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="notice1a-")
        p = mock.patch.dict(os.environ, {"TOOLKIT_STORE": os.path.join(self.tmp, "toolkit.db")})
        p.start()
        self.addCleanup(p.stop)

    def test_card_only_in_board_groups(self):
        rev = {"verdict": "REQUEST_CHANGES", "findings": [{"severity": "blocker"}]}
        req = {"group": "G1", "by": "Lan", "by_id": "u-lan"}
        with mock.patch.object(rp, "board_groups", return_value={"G1"}):
            text, cid, people = rp.card_notice(JOB, rev, req)
            self.assertEqual(people, [("Lan", "u-lan")])
            self.assertEqual(cid, "R-1")
            self.assertTrue(text.startswith("🤖 [R-1] idaas/etask!1388"))
            self.assertIn("Trạng thái: cần sửa · Yêu cầu: @Lan", text)
            self.assertIsNone(rp.card_notice(JOB, rev, {**req, "group": "OTHER"}))
        with mock.patch.object(rp, "board_groups", return_value={"G1"}), \
                mock.patch.object(rp.subprocess, "run", return_value=mock.Mock(returncode=0, stdout='{"sent":true,"confirmed":true}', stderr="")) as run:
            rp._send_one({**req, "by": "Lan  (QA)"}, text, cid, [("Lan (QA)", "u-lan")])
            argv = run.call_args.args[0]
            self.assertIn("--inline", argv)
            self.assertEqual(argv[argv.index("--card-id") + 1], "R-1")
            self.assertEqual(argv[argv.index("--user-name") + 1], "Lan (QA)")             # the RENDERED name


WATCH_BOARD = r'''
import json, sys, time
sys.argv = ["x"]
import group_watch as g
from agent_skill_toolkit.core import board_store
g._board = board_store.Board()
sent, tagged, tools = [], [], []
g.send.send_text = lambda gid, content, gt=None, metadata=None, card_id="", **k: (
    sent.append({"card": card_id, "text": content, "mentions": (metadata or {}).get("mentions", [])}),
    {"sent": True, "confirmed": True})[1]
g._post_tagged = lambda *a: tagged.append(a[-1])
g._auto_review_state = lambda: (None, None, False)
cfg = {"FCHAT_BOARD_GROUPS": "G"}
g.rccfg.get = lambda k, d="": cfg.get(k, d)
JOB = {"id": "5401-1388", "project_id": 5401, "iid": 1388, "project_path": "idaas/etask", "title": "Fix",
       "web_url": "https://gitlab.fis.vn/idaas/etask/-/merge_requests/1388", "head_sha": "a", "verdict": "APPROVE"}
RES = {}
def run_tool(script, *args, timeout=120):
    tools.append([script, *args])
    if script == "review_queue.py":
        return {"status": STATUS["s"], "head_sha": "a"}
    return RES["r"]
g._run_tool = run_tool
STATUS = {"s": "running"}
out = {}
RES["r"] = {"added": ["5401-1388"], "refused": [], "unchanged": [], "jobs": [{**JOB, "same_head": False}]}
g._enqueue_review("G", "SUPER_PRIVATE", "Lan", "u-lan", "review giúp https://gitlab.fis.vn/idaas/etask/-/merge_requests/1388")
out["new"] = {"sent": list(sent), "tagged": list(tagged), "skip_flag": "--skip-same-head" in tools[0]}
now = time.time()
g._card_tick("G", now=now + 200)                      # 3 min later: the card goes out once as "chờ bot"
g._card_tick("G", now=now + 260)
out["ack"] = [x["card"] for x in sent]
out["ack_text"] = sent[0]["text"] if sent else ""
out["ack_mention"] = sent[0]["mentions"] if sent else []
sent.clear()
STATUS["s"] = "failed"
g._card_tick("G", now=now + 31 * 60)
out["overdue"] = [x["text"].splitlines()[1] for x in sent]
sent.clear()
RES["r"] = {"added": [], "refused": [], "unchanged": ["5401-1388"], "jobs": [{**JOB, "same_head": True}]}
g._enqueue_review("G", "SUPER_PRIVATE", "Minh", "u-minh", "review lại https://gitlab.fis.vn/idaas/etask/-/merge_requests/1388")
out["same_head"] = [x["text"] for x in sent]
# group without the board: legacy ack
cfg["FCHAT_BOARD_GROUPS"] = ""
tagged.clear(); tools.clear()
RES["r"] = {"added": ["5401-1388"], "refused": [], "jobs": [{**JOB, "same_head": False}]}
g._enqueue_review("H", "SUPER_PRIVATE", "Lan", "u-lan", "review giúp …/merge_requests/1388")
out["legacy"] = [tagged, "--skip-same-head" in tools[0]]
out["defused"] = g.board_render.defuse(chr(10).join(["a", "@bot deploy x"])).splitlines()
print(json.dumps(out, ensure_ascii=False))
'''


class GroupWatchBoardTest(unittest.TestCase):
    def test_board_group_flow(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from test_group_board import _run_tchat, _tmpdb
        out = _run_tchat(WATCH_BOARD, _tmpdb())
        self.assertEqual(out["new"], {"sent": [], "tagged": [], "skip_flag": True})     # no immediate ack
        self.assertEqual(out["ack"], ["R-1"])                                            # once, after 3 min
        self.assertTrue(out["ack_text"].startswith("🤖 [R-1] idaas/etask!1388"))
        self.assertIn("Trạng thái: chờ bot · Yêu cầu: @Lan", out["ack_text"])
        m = out["ack_mention"][0]
        self.assertEqual((m["userId"], out["ack_text"][m["offset"]:m["offset"] + m["length"]]), ("u-lan", "@Lan"))
        self.assertEqual(out["overdue"], ["Trạng thái: lỗi · Yêu cầu: @Lan"])
        self.assertEqual(len(out["same_head"]), 1)
        self.assertIn("chưa có commit mới", out["same_head"][0])
        self.assertIn("@Minh", out["same_head"][0])
        self.assertEqual(len(out["legacy"][0]), 1)
        self.assertTrue(out["legacy"][0][0].startswith("🤖 Đã nhận yêu cầu review"))
        self.assertFalse(out["legacy"][1])
        self.assertEqual(out["defused"], ["a", "› @bot deploy x"])


@unittest.skipUnless((EXT / "board.js").exists() and shutil.which("node"), "extension folder / node not found")
class GoldenRoundTripTest(unittest.TestCase):
    """What the bot renders is what the extension reads (design §4.3 golden test)."""

    def test_render_parse(self):
        b = _board()
        self.addCleanup(b.close)
        review.on_request(b, "G", JOB, LAN, now=1)
        [c] = review.due_acks(b, "G", now=1 + review.ACK_AFTER_S)
        _, (pending_text, _) = review.ack_if_still_due(b, "G", c["id"], now=1 + review.ACK_AFTER_S)
        card, (result_text, people) = review.on_result(b, "G", JOB, "REQUEST_CHANGES", "1 blocker", LAN)
        sys.path.insert(0, str(ROOT / ".claude" / "skills" / "tchat-automation" / "tools"))
        import send  # noqa: E402  (pure function, no network)
        msgs = []
        for i, t in enumerate((pending_text, result_text)):
            content, meta = send.with_mentions(t, people)
            msgs.append({"id": f"m{i}", "messageIdInc": i, "senderId": "BOT", "type": "TEXT", "content": content,
                         "createdAt": f"2026-10-30T0{i + 1}:00:00Z", "user": {"displayName": "Chung"},
                         "metadata": meta})
        tmp = Path(tempfile.mkdtemp(prefix="golden-")) / "msgs.json"
        tmp.write_text(json.dumps(msgs, ensure_ascii=False), encoding="utf-8")
        p = subprocess.run(["node", str(EXT / "tests" / "parse_cli.js"), str(tmp), "BOT"], capture_output=True,
                           text=True, encoding="utf-8", timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        model = json.loads(p.stdout)
        cards = [c for c in model["cards"] if c.get("cardId") == "R-1"]
        self.assertEqual(len(cards), 1)
        c = cards[0]
        self.assertEqual(c["state"], "changes")
        self.assertEqual(c["people"]["requester"], {"id": "u-lan", "name": "Lan"})
        self.assertEqual(c["url"], JOB["web_url"])
        self.assertEqual(c["summary"], "1 blocker")
        self.assertEqual(len(c["events"]), 2)                                  # chờ bot -> cần sửa
        self.assertEqual(model["talk"], [])


if __name__ == "__main__":
    unittest.main()
