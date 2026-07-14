#!/usr/bin/env python3
"""Stdlib unittest cho task_queue — hàng đợi tuần tự (intake -> clarify -> queue -> next/done).

Phủ phần cơ chế queue (không gọi mạng/agent):
  1. add/dedupe/list — enqueue, chặn trùng qid còn mở, sort theo priority rồi tuổi.
  2. Khoá TUẦN TỰ THEO LUỒNG (owner, mặc định task_resolver) — `next` lấy lock; `next`
     lần 2 cùng owner bị từ chối; owner KHÁC không bị chặn; `done` nhả lock; `release`
     cứu lock kẹt; helper try_claim/release_claim (dùng chung với task_resolver.py).
  3. answer — --accept-proposed gấp `proposed` thành brief, item sang ready;
     source != atask thì KHÔNG sync (không đụng mạng).
  4. requeue/remove — chỉ requeue từ failed/done.
No pip deps. Enrichment (context_pack/scout) KHÔNG test ở đây vì cần API/clone thật.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from argparse import Namespace

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(
    _HERE, "..", ".claude", "skills", "auto-dev", "tools")))
sys.path.insert(0, os.path.abspath(os.path.join(
    _HERE, "..", ".claude", "skills", "dev-automation", "tools")))

import task_queue  # noqa: E402


def ns(**kw):
    return Namespace(**kw)


def add(task, approved=False, **kw):
    base = dict(source="manual", task=task, project=None, type=None,
                title=None, priority=2, ready=True)
    base.update(kw)
    out = task_queue.cmd_add(ns(**base))
    if approved and out.get("ok"):
        # luồng chuẩn bị đã duyệt solution -> đủ điều kiện thực thi
        task_queue.cmd_approve(ns(qid=out["item"]["qid"], plan=None, verify=None, note=None))
    return out


def nxt(owner=None):
    return task_queue.cmd_next(ns(owner=owner))


def done(qid, result="ok", note=None, owner=None):
    return task_queue.cmd_done(ns(qid=qid, result=result, note=note, owner=owner))


def answer(qid, answers_file=None, accept_proposed=False, no_sync=True):
    return task_queue.cmd_answer(ns(qid=qid, answers_file=answers_file,
                                    accept_proposed=accept_proposed, no_sync=no_sync))


class QueueBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tq_")
        os.environ["WORK_DIR"] = self.tmp

    def tearDown(self):
        os.environ.pop("WORK_DIR", None)
        shutil.rmtree(self.tmp, ignore_errors=True)


class AddListTest(QueueBase):
    def test_add_and_dedupe(self):
        out = add("t1")
        self.assertTrue(out["ok"])
        self.assertEqual(out["item"]["state"], "ready")
        dup = add("t1")
        self.assertTrue(dup["error"])
        self.assertIn("already queued", dup["message"])

    def test_closed_item_can_be_readded(self):
        add("t1", approved=True)
        task_queue.cmd_next(ns())
        task_queue.cmd_done(ns(qid="manual-t1", result="ok", note=None))
        self.assertTrue(add("t1")["ok"])

    def test_list_sorted_by_priority_then_age(self):
        add("low", priority=3)
        add("high", priority=1)
        add("mid", priority=2)
        out = task_queue.cmd_list(ns(state=None))
        self.assertEqual([i["qid"] for i in out["items"]],
                         ["manual-high", "manual-mid", "manual-low"])

    def test_list_filters_state(self):
        add("a", ready=True)
        add("b", ready=False)
        out = task_queue.cmd_list(ns(state="needs_clarification"))
        self.assertEqual([i["qid"] for i in out["items"]], ["manual-b"])


class SerialLockTest(QueueBase):
    def test_next_claims_and_second_next_refused(self):
        add("t1", approved=True)
        add("t2", approved=True)
        first = task_queue.cmd_next(ns())
        self.assertTrue(first["ok"])
        self.assertEqual(first["item"]["qid"], "manual-t1")
        self.assertEqual(first["item"]["state"], "processing")
        second = task_queue.cmd_next(ns())
        self.assertTrue(second["error"])
        self.assertEqual(second["locked_by"]["qid"], "manual-t1")

    def test_done_releases_lock_and_next_proceeds(self):
        add("t1", approved=True)
        add("t2", approved=True)
        task_queue.cmd_next(ns())
        done = task_queue.cmd_done(ns(qid="manual-t1", result="ok", note="merged"))
        self.assertEqual(done["item"]["state"], "done")
        nxt = task_queue.cmd_next(ns())
        self.assertEqual(nxt["item"]["qid"], "manual-t2")

    def test_done_fail_marks_failed(self):
        add("t1", approved=True)
        task_queue.cmd_next(ns())
        out = task_queue.cmd_done(ns(qid="manual-t1", result="fail", note="tests red"))
        self.assertEqual(out["item"]["state"], "failed")

    def test_release_returns_item_to_ready(self):
        add("t1", approved=True)
        task_queue.cmd_next(ns())
        rel = task_queue.cmd_release(ns())
        self.assertTrue(rel["ok"])
        item = task_queue._load("manual-t1")
        self.assertEqual(item["state"], "ready")
        self.assertIsNone(task_queue._read_lock())

    def test_next_on_empty_queue(self):
        out = task_queue.cmd_next(ns())
        self.assertTrue(out["ok"])
        self.assertIsNone(out["item"])

    def test_only_approved_is_claimable(self):
        add("t1", ready=False)          # needs_clarification
        add("t2")                        # ready (đủ info, CHƯA duyệt solution)
        out = task_queue.cmd_next(ns())
        self.assertIsNone(out["item"])   # cả hai đều chưa được thực thi
        task_queue.cmd_approve(ns(qid="manual-t2", plan=None, verify=None, note=None))
        self.assertEqual(task_queue.cmd_next(ns())["item"]["qid"], "manual-t2")


class AnswerTest(QueueBase):
    def test_accept_proposed_builds_brief_and_readies(self):
        add("t1", ready=False, title="Fix export permission")
        item = task_queue._load("manual-t1")
        item["questions"] = [
            {"ask": "Scope?", "blocking": True,
             "proposed": "chỉ endpoint /export", "assumption": "toàn module"},
        ]
        task_queue._save(item)
        out = task_queue.cmd_answer(ns(qid="manual-t1", answers_file=None,
                                       accept_proposed=True))
        self.assertTrue(out["ok"])
        self.assertEqual(out["item"]["state"], "ready")
        with open(out["brief_path"], encoding="utf-8") as f:
            brief = f.read()
        self.assertIn("chỉ endpoint /export", brief)

    def test_answers_file_resolves(self):
        add("t1", ready=False, title="T")
        ans = os.path.join(self.tmp, "ans.json")
        with open(ans, "w", encoding="utf-8") as f:
            json.dump({"Scope?": "chỉ /export"}, f)
        out = task_queue.cmd_answer(ns(qid="manual-t1", answers_file=ans,
                                       accept_proposed=False))
        self.assertEqual(out["item"]["state"], "ready")

    def test_answer_requires_input(self):
        add("t1", ready=False)
        out = task_queue.cmd_answer(ns(qid="manual-t1", answers_file=None,
                                       accept_proposed=False))
        self.assertTrue(out["error"])


class FlowOwnerLockTest(QueueBase):
    def test_default_owner_is_task_resolver(self):
        add("t1", approved=True)
        out = nxt()
        self.assertTrue(out["ok"])
        self.assertEqual(task_queue._read_lock("task_resolver")["qid"], "manual-t1")

    def test_other_owner_not_blocked(self):
        add("t1", approved=True)
        add("t2", approved=True)
        first = nxt(owner="task_resolver")
        self.assertTrue(first["ok"])
        # Luồng khác (vd người làm tay) vẫn lấy được task kế — không phải lock toàn cục.
        second = nxt(owner="manual")
        self.assertTrue(second["ok"])
        self.assertEqual(second["item"]["qid"], "manual-t2")
        # Nhưng cùng owner thì vẫn tuần tự.
        third = nxt(owner="manual")
        self.assertTrue(third["error"])

    def test_done_releases_the_items_own_owner(self):
        add("t1", approved=True)
        nxt(owner="manual")
        out = done("manual-t1")           # không truyền owner -> lấy owner ghi trên item
        self.assertEqual(out["item"]["state"], "done")
        self.assertIsNone(task_queue._read_lock("manual"))

    def test_try_claim_release_helpers(self):
        # API mà task_resolver.py dùng trực tiếp.
        self.assertTrue(task_queue.try_claim("task_resolver", "tX"))
        self.assertFalse(task_queue.try_claim("task_resolver", "tY"))   # đang bận
        self.assertTrue(task_queue.try_claim("manual", "tZ"))           # owner khác OK
        self.assertFalse(task_queue.release_claim("task_resolver", qid="tY"))  # sai qid
        self.assertTrue(task_queue.release_claim("task_resolver", qid="tX"))
        self.assertTrue(task_queue.try_claim("task_resolver", "tY"))    # đã nhả -> claim lại được


class AnswerSyncTest(QueueBase):
    def test_manual_source_never_syncs(self):
        add("t1", ready=False, title="T")
        item = task_queue._load("manual-t1")
        item["questions"] = [{"ask": "Scope?", "blocking": True, "proposed": "x"}]
        task_queue._save(item)
        out = answer("manual-t1", accept_proposed=True, no_sync=False)
        self.assertTrue(out["ok"])
        self.assertIsNone(out.get("brief_synced"))  # source=manual -> không đụng aTask


class MarkStatusTest(QueueBase):
    """mark: tra status-ID theo-list rồi update — mock _atask_tool, không mạng."""

    def setUp(self):
        super().setUp()
        add("t1", source="atask")
        # add() dùng qid = source-task
        self.qid = "atask-t1"
        self.calls = []
        self._orig = task_queue._atask_tool

        def fake(script, cli_args, timeout=60):
            self.calls.append((script, cli_args))
            if script == "tasks.py" and cli_args[0] == "get":
                return {"success": True, "content": {
                    "id": "t1", "listTaskId": "L1", "statusType": "todo"}}
            if script == "search.py":
                return {"success": True, "content": {"data": [
                    {"listTaskId": "L9", "statusType": "processing", "status": "WRONG-LIST"},
                    {"listTaskId": "L1", "statusType": "processing", "status": "S-PROC"},
                ]}}
            if script == "tasks.py" and cli_args[0] == "update":
                return {"success": True}
            return {"success": True}
        task_queue._atask_tool = fake

    def tearDown(self):
        task_queue._atask_tool = self._orig
        super().tearDown()

    def test_marks_with_status_id_of_correct_list(self):
        out = task_queue.cmd_mark(ns(qid=self.qid, to="processing", comment=None))
        self.assertTrue(out["ok"])
        self.assertEqual(out["status_id"], "S-PROC")   # KHÔNG mượn ID của list khác
        upd = [c for c in self.calls if c[0] == "tasks.py" and c[1][0] == "update"][0]
        self.assertIn("S-PROC", upd[1])
        item = task_queue._load(self.qid)
        self.assertEqual(item["atask_status"], "processing")

    def test_unchanged_when_already_there(self):
        out = task_queue.cmd_mark(ns(qid=self.qid, to="todo", comment=None))
        self.assertTrue(out.get("unchanged"))

    def test_missing_column_is_error(self):
        out = task_queue.cmd_mark(ns(qid=self.qid, to="closed", comment=None))
        self.assertTrue(out["error"])

    def test_comment_posted_when_given(self):
        task_queue.cmd_mark(ns(qid=self.qid, to="processing", comment="MR: http://x"))
        self.assertTrue(any(c[0] == "checklists.py" and c[1][0] == "add-comment"
                            for c in self.calls))

    def test_non_atask_source_refused(self):
        add("m1")   # source=manual
        out = task_queue.cmd_mark(ns(qid="manual-m1", to="processing", comment=None))
        self.assertTrue(out["error"])


class TelegramClarifyTest(QueueBase):
    """ask-tg gửi mục đánh số; reply parse comment TỰ DO ('1: ok; 2: ...') -> answers."""

    QUESTIONS = [
        {"ask": "Chặn trạng thái nào?", "blocking": True, "proposed": "chỉ Hoàn thành"},
        {"ask": "HTTP code khi chặn?", "blocking": True, "proposed": "400"},
        {"ask": "Áp cho subtask?", "blocking": False, "proposed": "có"},
    ]

    def setUp(self):
        super().setUp()
        add("t1", ready=False, title="Chặn xoá task Hoàn thành")
        item = task_queue._load("manual-t1")
        item["questions"] = list(self.QUESTIONS)
        task_queue._save(item)
        self.sent = []
        self._orig = task_queue._send_tg
        task_queue._send_tg = lambda text: (self.sent.append(text) or (True, ""))

    def tearDown(self):
        task_queue._send_tg = self._orig
        super().tearDown()

    def test_parse_reply_free_text(self):
        answers, matched = task_queue._parse_reply(
            "1: ok; 2: dùng 409 CONFLICT thay vì 400\n3) chỉ parent thôi", self.QUESTIONS)
        self.assertEqual(matched, [1, 2, 3])
        self.assertEqual(answers[0]["answer"], "")                    # 'ok' = nhận đề xuất
        self.assertIn("409", answers[1]["answer"])                    # sửa tự do
        self.assertEqual(answers[2]["answer"], "chỉ parent thôi")
        # mục bỏ qua hoàn toàn -> nhận đề xuất
        answers2, matched2 = task_queue._parse_reply("2: 409", self.QUESTIONS)
        self.assertEqual(matched2, [2])
        self.assertEqual(answers2[0]["answer"], "")
        self.assertEqual(answers2[0]["proposed"], "chỉ Hoàn thành")

    def test_parse_reply_comma_and_dot_separators(self):
        # người dùng thật viết: "1: chỉ hoàn thành, 2: completed... xóa. 3: subtask..."
        answers, matched = task_queue._parse_reply(
            "1: chỉ hoàn thành, 2: completed. 3: subtask hoàn thành thì k được xóa",
            self.QUESTIONS)
        self.assertEqual(matched, [1, 2, 3])
        self.assertEqual(answers[0]["answer"], "chỉ hoàn thành")
        self.assertEqual(answers[1]["answer"], "completed")
        self.assertIn("subtask", answers[2]["answer"])

    def test_parse_reply_strips_qid_prefix(self):
        answers, matched = task_queue._parse_reply(
            "manual-t1 1: ok; 2: 409", self.QUESTIONS)
        self.assertEqual(matched, [1, 2])

    def test_ask_tg_sends_numbered_form(self):
        out = task_queue.cmd_ask_tg(ns(qid="manual-t1"))
        self.assertTrue(out["ok"])
        self.assertEqual(len(self.sent), 1)
        msg = self.sent[0]
        self.assertIn("1. ❗", msg)
        self.assertIn("3. ▫️", msg)
        self.assertIn("Đề xuất:", msg)
        self.assertIn("1: ok; 2:", msg)          # hướng dẫn trả lời tự do
        self.assertNotIn("callback", msg)        # KHÔNG phải nút bấm

    def test_reply_folds_answers_and_readies(self):
        out = task_queue.cmd_reply(ns(qid="manual-t1",
                                      text="1: ok; 2: dùng 409; 3: chỉ parent",
                                      no_sync=True))
        self.assertTrue(out["ok"])
        self.assertEqual(out["item"]["state"], "ready")
        self.assertEqual(out["items_answered"], [1, 2, 3])
        with open(out["brief_path"], encoding="utf-8") as f:
            brief = f.read()
        self.assertIn("409", brief)              # câu sửa nằm trong 'Đã chốt'
        self.assertIn("chỉ Hoàn thành", brief)   # câu 'ok' -> giả định từ đề xuất


class TgGateTest(QueueBase):
    """tg_gate: gửi mốc duyệt dạng mục tự do + parse trả lời -> approved/comment."""

    def setUp(self):
        super().setUp()
        import tg_gate
        import run_log
        self.tg_gate, self.run_log = tg_gate, run_log
        self._runs_orig = run_log._runs_dir
        run_log._runs_dir = lambda: self.tmp
        self.sent = []
        self._send_orig = task_queue._send_tg
        task_queue._send_tg = lambda text: (self.sent.append(text) or (True, ""))

    def tearDown(self):
        task_queue._send_tg = self._send_orig
        self.run_log._runs_dir = self._runs_orig
        super().tearDown()

    def test_send_then_parse_roundtrip(self):
        out = self.tg_gate.cmd_send(ns(run="r1", gate="after_plan", title="Chặn xoá task",
                                       item=["Plan: atomic DELETE || đề xuất: duyệt",
                                             "Verify: 10 step || đề xuất: duyệt",
                                             "Sửa engine mysql || đề xuất: tôi sửa"],
                                       items_file=None))
        self.assertTrue(out["ok"])
        self.assertIn("1. Plan: atomic DELETE", self.sent[0])
        self.assertIn("Duyệt after_plan", self.sent[0])
        parsed = self.tg_gate.cmd_parse(ns(run="r1", gate="after_plan",
                                           text="1: ok; 2: thêm step kiểm tra subtask; 3: ok"))
        self.assertFalse(parsed["approved_all"])
        self.assertTrue(parsed["items"][0]["approved"])
        self.assertEqual(parsed["items"][1]["comment"], "thêm step kiểm tra subtask")
        self.assertTrue(parsed["items"][2]["approved"])

    def test_parse_all_ok(self):
        self.tg_gate.cmd_send(ns(run="r1", gate="before_mr", title="T",
                                 item=["A || đề xuất: duyệt"], items_file=None))
        parsed = self.tg_gate.cmd_parse(ns(run="r1", gate="before_mr", text="1: ok"))
        self.assertTrue(parsed["approved_all"])

    def test_parse_without_send_errors(self):
        out = self.tg_gate.cmd_parse(ns(run="rX", gate="nope", text="1: ok"))
        self.assertTrue(out["error"])

    def test_reply_then_wait_returns_parsed(self):
        self.tg_gate.cmd_send(ns(run="r1", gate="after_plan", title="T",
                                 item=["A || đề xuất: duyệt", "B || đề xuất: duyệt"],
                                 items_file=None))
        # bridge/người ghi nhận tin trả lời:
        self.tg_gate.cmd_reply(ns(run="r1", gate="after_plan", text="1: ok; 2: đổi B"))
        # wait thấy reply-file ngay -> trả kết quả parse, không chờ hết timeout
        out = self.tg_gate.cmd_wait(ns(run="r1", gate="after_plan",
                                       timeout=3, interval=1, poll_updates=False))
        self.assertFalse(out["approved_all"])
        self.assertEqual(out["items"][1]["comment"], "đổi B")
        self.assertIn("reply_text", out)

    def test_wait_times_out_cleanly(self):
        self.tg_gate.cmd_send(ns(run="r2", gate="before_mr", title="T",
                                 item=["A"], items_file=None))
        out = self.tg_gate.cmd_wait(ns(run="r2", gate="before_mr",
                                       timeout=1, interval=1, poll_updates=False))
        self.assertEqual(out["status"], "timeout")


class WorkerTest(QueueBase):
    """queue_worker: chạy tuần tự theo priority, park không kẹt hàng, gỡ agent chết."""

    def setUp(self):
        super().setUp()
        import queue_worker
        self.qw = queue_worker
        self._send = task_queue._send_tg
        task_queue._send_tg = lambda text: (True, "")

    def tearDown(self):
        task_queue._send_tg = self._send
        super().tearDown()

    def test_priority_map(self):
        self.assertEqual(task_queue.atask_priority_to_queue("1"), 1)   # Khẩn cấp
        self.assertEqual(task_queue.atask_priority_to_queue("4"), 3)   # Thấp
        self.assertEqual(task_queue.atask_priority_to_queue(None), 2)  # mặc định

    def test_dry_run_drains_queue_in_priority_order(self):
        add("low", priority=3, approved=True)
        add("urgent", priority=1, approved=True)
        out = self.qw.cmd_run(ns(project=None, env="dev", max_tasks=0,
                                 interval=0, task_timeout=10, dry_run=True))
        self.assertEqual(out["done"], ["manual-urgent", "manual-low"])
        self.assertIsNone(task_queue._read_lock("task_resolver"))      # không kẹt lock

    def test_agent_done_vs_parked_vs_stuck(self):
        add("a", approved=True)
        add("b", approved=True)
        add("c", approved=True)
        outcomes = {"manual-a": "done", "manual-b": "failed", "manual-c": "processing"}

        def fake_spawn(task_id, timeout):
            qid = f"manual-{task_id}"
            it = task_queue._load(qid)
            it["state"] = outcomes[qid]
            task_queue._save(it)
            if outcomes[qid] != "processing":   # agent tự nhả lock khi kết thúc tử tế
                task_queue.release_claim("task_resolver", qid)
            return 0, ""
        orig_spawn = self.qw._spawn_agent
        self.qw._spawn_agent = fake_spawn
        try:
            out = self.qw.cmd_run(ns(project=None, env="dev", max_tasks=0,
                                     interval=0, task_timeout=10, dry_run=False))
        finally:
            self.qw._spawn_agent = orig_spawn
        self.assertEqual(out["done"], ["manual-a"])
        self.assertEqual(sorted(out["parked"]), ["manual-b", "manual-c"])
        # agent chết giữa chừng (c): worker phải đánh fail + nhả lock
        self.assertEqual(task_queue._load("manual-c")["state"], "failed")
        self.assertIsNone(task_queue._read_lock("task_resolver"))

    def test_max_tasks_respected(self):
        for t in ("a", "b", "c"):
            add(t, approved=True)
        out = self.qw.cmd_run(ns(project=None, env="dev", max_tasks=2,
                                 interval=0, task_timeout=10, dry_run=True))
        self.assertEqual(len(out["done"]), 2)


class AutopilotTest(QueueBase):
    """autopilot: nối review->prep->execute; chờ-người không chặn pha thực thi."""

    def setUp(self):
        super().setUp()
        import autopilot
        self.ap = autopilot
        self._send = task_queue._send_tg
        task_queue._send_tg = lambda text: (True, "")
        self._review = autopilot._phase_review
        self._spawn = autopilot._spawn_prep
        self._worker = autopilot.queue_worker.cmd_run
        autopilot._phase_review = lambda a: {"rc": 0, "tail": ""}

    def tearDown(self):
        self.ap._phase_review = self._review
        self.ap._spawn_prep = self._spawn
        self.ap.queue_worker.cmd_run = self._worker
        task_queue._send_tg = self._send
        super().tearDown()

    def _args(self, **kw):
        base = dict(resolve_existing=False, skip_resolver=False, skip_execute=False,
                    project=None, env="dev", max_tasks=0, review_timeout=10,
                    prep_timeout=10, task_timeout=10)
        base.update(kw)
        return ns(**base)

    def test_prep_approves_then_worker_runs(self):
        add("t1")                      # ready -> prep phải xử lý
        add("t2", ready=False)         # needs_clarification -> prep xử lý, người chưa trả lời

        def fake_prep(task_id, timeout):
            if task_id == "t1":        # người duyệt xong -> approved
                task_queue.cmd_approve(ns(qid="manual-t1", plan=None, verify=None, note=None))
            return 0                    # t2: giữ nguyên (chờ người)
        self.ap._spawn_prep = fake_prep
        ran = {}
        self.ap.queue_worker.cmd_run = lambda a: ran.update(done=["manual-t1"], parked=[]) or \
            {"done": ["manual-t1"], "parked": []}
        out = self.ap.cmd_run(self._args())
        self.assertEqual(out["approved_in_prep"], ["manual-t1"])
        self.assertEqual(len(out["waiting_human"]), 1)
        self.assertIn("manual-t2", out["waiting_human"][0])
        self.assertEqual(out["done"], ["manual-t1"])   # chờ-người KHÔNG chặn thực thi

    def test_skip_flags(self):
        called = {"review": 0, "worker": 0}
        self.ap._phase_review = lambda a: called.__setitem__("review", 1) or {"rc": 0, "tail": ""}
        self.ap.queue_worker.cmd_run = lambda a: called.__setitem__("worker", 1) or \
            {"done": [], "parked": []}
        self.ap.cmd_run(self._args(skip_resolver=True, skip_execute=True))
        self.assertEqual(called, {"review": 0, "worker": 0})

    def test_quiet_idle_suppresses_telegram_when_nothing_happened(self):
        sent = []
        task_queue._send_tg = lambda text: (sent.append(text) or (True, ""))
        self.ap.queue_worker.cmd_run = lambda a: {"done": [], "parked": []}
        # lượt RẢNH + quiet -> im lặng tuyệt đối
        self.ap.cmd_run(self._args(skip_resolver=True, quiet_idle=True))
        self.assertEqual(sent, [])
        # có việc (waiting_human) -> vẫn phải nhắn dù quiet
        add("t1", ready=False)
        self.ap._spawn_prep = lambda tid, to: 0
        self.ap.cmd_run(self._args(skip_resolver=True, quiet_idle=True))
        self.assertEqual(len(sent), 1)
        self.assertIn("chờ bạn", sent[0])


class ResolverEnqueueNoLockTest(QueueBase):
    """--enqueue là review-only: KHÔNG claim khoá thực thi — lock bận vẫn review đủ loạt
    (bug thật: --once + claim-per-task làm mỗi lần chạy chỉ xử lý đúng 1 task)."""

    def test_enqueue_mode_ignores_busy_lock(self):
        sys.path.insert(0, os.path.abspath(os.path.join(
            _HERE, "..", ".claude", "skills", "atask-automation", "tools")))
        import task_resolver as tr
        calls = []
        orig = (tr._ENQUEUE, tr._analyze, tr._notify, tr._my_login, tr.STATE,
                task_queue.cmd_intake)
        tr._ENQUEUE = True
        tr._analyze = lambda task, chat: ("summary [[VERDICT]] x",
                                          {"status": "not_fixed", "project": "p1",
                                           "reason": "", "assignee_name": None,
                                           "assignee_atask_id": None, "estimate_days": 1})
        tr._notify = lambda chat, text: None
        tr._my_login = lambda: "me"
        tr.STATE = os.path.join(self.tmp, "resolved.json")
        task_queue.cmd_intake = lambda a: calls.append(a) or {
            "ok": True, "item": {"state": "ready"}}
        task_queue.try_claim("task_resolver", "OTHER-TASK")   # lock đang BẬN
        try:
            tr._handle_task({"id": "tX", "name": "T", "statusType": "todo",
                             "priority": "2"}, "chat", {})
        finally:
            (tr._ENQUEUE, tr._analyze, tr._notify, tr._my_login, tr.STATE,
             task_queue.cmd_intake) = orig
            task_queue.release_claim("task_resolver")
        self.assertEqual(len(calls), 1)          # vẫn review + enqueue dù lock bận
        self.assertEqual(calls[0].priority, 1)   # aTask '2'=Cao -> hàng ưu tiên 1


class ThinGuardTest(unittest.TestCase):
    def _item(self, state="ready", **signals):
        return {"state": state, "signals": signals, "notes": []}

    def test_thin_and_bare_forces_clarification(self):
        it = self._item(thin_description=True, has_extra_context=False)
        task_queue._apply_thin_guard(it)
        self.assertEqual(it["state"], "needs_clarification")

    def test_thin_but_enriched_stays_ready(self):
        it = self._item(thin_description=True, has_extra_context=True)
        task_queue._apply_thin_guard(it)
        self.assertEqual(it["state"], "ready")

    def test_rich_description_untouched(self):
        it = self._item(thin_description=False, has_extra_context=False)
        task_queue._apply_thin_guard(it)
        self.assertEqual(it["state"], "ready")


class RequeueRemoveTest(QueueBase):
    def test_requeue_only_from_terminal(self):
        add("t1", approved=True)
        blocked = task_queue.cmd_requeue(ns(qid="manual-t1"))
        self.assertTrue(blocked["error"])
        task_queue.cmd_next(ns())
        task_queue.cmd_done(ns(qid="manual-t1", result="fail", note=None))
        out = task_queue.cmd_requeue(ns(qid="manual-t1"))
        self.assertTrue(out["ok"])
        # item đã duyệt solution -> quay thẳng lại hàng thực thi.
        self.assertEqual(out["item"]["state"], "approved")

    def test_remove(self):
        add("t1")
        out = task_queue.cmd_remove(ns(qid="manual-t1"))
        self.assertTrue(out["ok"])
        self.assertIsNone(task_queue._load("manual-t1"))


if __name__ == "__main__":
    unittest.main()
