"""Group workboard phase 3 (docs/design/group-workboard.md §6): aTask task cards T-n — command parsing, list
choice, assignee from the registry (a mention only points), draft -> confirm -> create with an idempotency marker,
lost answers reconciled (never a second task), cancel / done (asked twice), the watcher wiring, and the golden
render -> extension parse round trip."""

import datetime as dt
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
sys.path.insert(0, str(Path(__file__).resolve().parent))
from agent_skill_toolkit.core import board_store, policy  # noqa: E402
from agent_skill_toolkit.integrations import atask  # noqa: E402
from agent_skill_toolkit.workflows.group_board import commands, task  # noqa: E402

EXT = Path(os.environ.get("FQR_EXT_DIR") or ROOT.parent / "fpt-chat-quick-reply")
TODAY = dt.date(2026, 10, 30)
LISTS = {"default": "6", "lists": {
    "6": {"project": "Backend Project Task", "project_id": "P6", "name": "Task BE", "list_id": "L6"},
    "1": {"project": "Dự án eTask triển khai KYTA Team", "project_id": "P1", "name": "Danh sách góp ý/ lỗi", "list_id": "L1"},
    "10": {"project": "Dự án eTask triển khai KYTA Team", "project_id": "P1", "name": "Danh sách bug AI", "list_id": "L10"}}}
TEAM = {"lan": {"name": "Lan Nguyen", "handles": {"fchat_id": "U1", "atask_user_id": 101}},
        "huy": {"name": "Huy Tran", "handles": {"fchat_id": "U2", "atask_user_id": 102}},
        "nolink": {"name": "Minh Bui", "handles": {"fchat_id": "U3"}},
        "dup1": {"name": "Anh Le", "handles": {"fchat_id": "U4", "atask_user_id": 104}},
        "dup2": {"name": "Anh Le", "handles": {"fchat_id": "U5", "atask_user_id": 105}}}
LAN = {"fchat_id": "U1", "name": "Lan Nguyen", "perms": ["review", "task"]}
HUY = {"fchat_id": "U2", "name": "Huy Tran", "perms": ["review", "task"]}


def _board():
    b = board_store.Board(os.path.join(tempfile.mkdtemp(prefix="board3-"), "toolkit.db"))
    return b


class FakeAT:
    """aTask stand-in: scripted answers, records every write."""
    content = staticmethod(atask.content)

    def __init__(self):
        self.created, self.assigned, self.completed = [], [], []
        self.create_result = None            # None = success
        self.found = {}                      # what find_by_marker returns (None = aTask unreachable)
        self.assign_result = {"success": True}
        self.complete_result = {"success": True}

    @staticmethod
    def task_link(code):
        return f"https://atask.example/task/{code}" if code else ""

    def create_task(self, name, list_id, description="", due_date=""):
        self.created.append({"name": name, "list_id": list_id, "description": description, "due": due_date})
        if self.create_result is not None:
            return self.create_result
        return {"success": True, "content": {"id": f"T{len(self.created)}", "taskCode": f"BPT-{40 + len(self.created)}"}}

    def find_by_marker(self, name, list_id, marker):
        return self.found

    def assign_users(self, task_id, ids):
        self.assigned.append((task_id, list(ids)))
        return self.assign_result

    def complete_task(self, task_id):
        self.completed.append(task_id)
        return self.complete_result


def _draft(b, at=None, text="Sửa lọc task | giao: @Huy Tran | hạn: 05/11/2026 | mô tả: theo người tạo", ident=LAN,
           now=1000.0, mentions=()):
    ok, f = task.parse_create(text, TODAY)
    assert ok == "ok", f
    asg, why = task.resolve_assignee(f["assignee"], mentions, TEAM, {"id": ident["fchat_id"], "name": ident["name"]})
    assert not why, why
    return task.draft(b, "G", ident, f, asg, task.pick_list(LISTS, f["list"]), "Chung", now=now)


class ParseTest(unittest.TestCase):
    def test_fields(self):
        ok, f = task.parse_create("Sửa lọc | giao: @Huy Tran | hạn: 5/11 | mô tả: a · b | list: 1", TODAY)
        self.assertEqual(ok, "ok")
        self.assertEqual((f["title"], f["assignee"], f["due"], f["desc"], f["list"]),
                         ("Sửa lọc", "@Huy Tran", dt.date(2026, 11, 5), "a · b", "1"))

    def test_errors(self):
        for text, why in (("", "thiếu tiêu đề"), ("x | hạn: 29/10/2026", "đã qua"), ("x | hạn: 31/02/2027", "có thật"),
                          ("x | hạn: mai", "dd/mm/yyyy"), ("x | ưu tiên: cao", "không hiểu"), ("x" * 201, "dài quá")):
            ok, msg = task.parse_create(text, TODAY)
            self.assertEqual(ok, "error", text)
            self.assertIn(why, msg)

    def test_commands(self):
        p = commands.parse
        self.assertEqual(p("@bot tạo task: A | giao: @B"), ("task_create", ("A | giao: @B",)))
        self.assertIsNone(p("tạo task: A"))                                  # needs the prefix
        self.assertEqual(p("tạo task: A", prefixed=True)[0], "task_create")  # owner path
        self.assertEqual(p("@bot tạo task: dòng 1\nmô tả dài")[1], ("dòng 1\nmô tả dài",))
        self.assertEqual(p("xác nhận T-5"), ("task_confirm", ("T-5",)))
        self.assertEqual(p("xác nhận xong T-5"), ("task_confirm_done", ("T-5",)))
        self.assertEqual(p("huỷ T-5"), ("task_cancel", ("T-5",)))
        self.assertEqual(p("hủy T-5"), ("task_cancel", ("T-5",)))
        self.assertEqual(p("xong T-5"), ("task_done", ("T-5",)))
        self.assertEqual(p("@bot list task"), ("task_lists", ()))
        self.assertIsNone(p("xong T-5 rồi nhé mọi người"))                   # anchored, whole line
        self.assertIsNone(p("xác nhận R-5"))

    def test_policy(self):
        self.assertEqual(policy.check("task", {"perms": ["review"]})[0], False)
        self.assertEqual(policy.check("task", {"perms": ["task"]})[0], True)
        self.assertEqual(policy.check("task_done", {"perms": ["review"]})[0], True)
        self.assertEqual(policy.check("task_done", {"perms": []})[0], False)       # registry-blocked sender


class DueTest(unittest.TestCase):
    def test_due_instant(self):
        self.assertEqual(task.due_instant("2026-08-07"), "2026-08-07T16:59:59Z")       # = BPT-31 on aTask
        self.assertEqual(task.due_instant("2026-12-31"), "2026-12-31T16:59:59Z")
        self.assertEqual(task.due_instant(""), "")


class EnvSnapshotTest(unittest.TestCase):
    """Bảng môi trường của extension: '@bot env' (không repo) ⇒ một tin ảnh chụp mọi repo."""

    def test_command(self):
        self.assertEqual(commands.parse("@bot env")[0], "env_all")
        self.assertEqual(commands.parse("@bot env all")[0], "env_all")
        self.assertEqual(commands.parse("@bot env tất cả")[0], "env_all")
        self.assertEqual(commands.parse("@bot env etask"), ("env", ("etask",)))
        self.assertIsNone(commands.parse("env"))                                      # needs the prefix

    def test_text(self):
        from agent_skill_toolkit.workflows.group_board import envmap
        t = envmap.snapshot_text([("etask", "https://g/idaas/etask", [
            {"env": "sandbox", "level": "verified", "version": "0.198.45", "commit": "a" * 40,
             "rs_created_at": "", "branch": "merge|rfc", "build": {"url": "http://j/282/"}},
            {"env": "prod", "level": "unknown"}])], now=0, scope="chủ nhóm")
        lines = t.splitlines()
        self.assertTrue(lines[0].startswith("🌐 [ENV-SNAPSHOT] Bảng môi trường · "))
        self.assertTrue(lines[0].endswith(" · chủ nhóm"))
        self.assertEqual(lines[1], "repo | etask | https://g/idaas/etask")
        self.assertEqual(lines[2], "etask | sandbox | ✅ | 0.198.45 | " + "a" * 40 + " | — | merge/rfc | http://j/282/")
        self.assertEqual(lines[3], "etask | prod | ❔ | — | — | — | — | —")             # '|' inside a value never splits


class ListTest(unittest.TestCase):
    def test_pick(self):
        self.assertEqual(task.pick_list(LISTS, "")[0], "6")
        self.assertEqual(task.pick_list(LISTS, "1")[0], "1")
        self.assertEqual(task.pick_list(LISTS, "bug ai")[0], "10")
        self.assertIsNone(task.pick_list(LISTS, "eTask triển khai")[0])           # two lists match
        self.assertIn("không có", task.pick_list(LISTS, "99")[1])
        self.assertIn("mặc định", task.pick_list({"default": "", "lists": {}}, "")[1])

    def test_load(self):
        d = tempfile.mkdtemp(prefix="lists-")
        with mock.patch.dict(os.environ, {"WORK_DIR": d}):
            self.assertEqual(task.load_lists(), {"default": "", "lists": {}})
            Path(d, "task_lists.json").write_text(json.dumps({"default": "6", "lists": {"6": {"name": "x"}}}), "utf-8")
            with self.assertRaises(ValueError):                                     # no list_id = broken
                task.load_lists()
            Path(d, "task_lists.json").write_text(json.dumps(LISTS), "utf-8")
            self.assertEqual(task.load_lists()["default"], "6")
            self.assertIn("6: Backend Project Task › Task BE (mặc định)", task.lists_text(task.load_lists()))


class AssigneeTest(unittest.TestCase):
    def test_resolution(self):
        req = {"id": "U1", "name": "Lan Nguyen"}
        r = task.resolve_assignee
        self.assertEqual(r("@Huy Tran", (), TEAM, req)[0]["atask"], "102")             # by registry name
        self.assertEqual(r("@Huy", [{"target": "Huy", "userId": "U2"}], TEAM, req)[0]["key"], "huy")  # mention -> id
        self.assertEqual(r("tôi", (), TEAM, req)[0]["atask"], "101")
        self.assertEqual(r("", (), TEAM, req), (None, ""))
        self.assertIn("chưa liên kết", r("@Minh Bui", (), TEAM, req)[1])
        self.assertIn("nhiều hồ sơ", r("@Anh Le", (), TEAM, req)[1])
        self.assertIn("chưa liên kết", r("@Người Lạ", (), TEAM, req)[1])
        # a mention naming a member with a FAKE id is resolved by id — never trusted for the aTask id
        self.assertIn("chưa liên kết", r("@Huy Tran", [{"target": "Huy Tran", "userId": "X9"}], TEAM, req)[1])

    def test_link_must_be_unique_and_complete(self):                                  # Codex 3 R1 #3
        req = {"id": "U1", "name": "Lan Nguyen"}
        team = {**TEAM, "huy2": {"name": "Huy Khac", "handles": {"fchat_id": "U8", "atask_user_id": 102}},
                "nofchat": {"name": "Tam", "handles": {"atask_user_id": 109}}}
        self.assertIn("nhiều hồ sơ", task.resolve_assignee("@Huy Tran", (), team, req)[1])
        self.assertIn("chưa liên kết đủ", task.resolve_assignee("@Tam", (), team, req)[1])


class FlowTest(unittest.TestCase):
    def setUp(self):
        self.b = _board()
        self.addCleanup(self.b.close)
        self.at = FakeAT()

    def test_draft_card(self):
        out = _draft(self.b)
        text, people = out["send"]
        lines = text.split("\n")
        self.assertEqual(lines[0], "🤖 [T-1] Sửa lọc task")
        self.assertEqual(lines[1], "Trạng thái: chờ xác nhận · Yêu cầu: @Lan Nguyen · Giao: @Huy Tran")
        self.assertEqual(lines[2], "Hạn: 05/11/2026 · List: Backend Project Task › Task BE")
        self.assertIn('gõ "xác nhận T-1" để tạo trên aTask, "huỷ T-1" để bỏ', text)
        self.assertEqual(text.count("@Lan Nguyen"), 1)                                # tagged once per message
        self.assertEqual(people, [("Lan Nguyen", "U1"), ("Huy Tran", "U2")])
        self.assertEqual(out["card"]["state"], "draft")
        self.assertEqual(self.at.created, [])                                       # nothing on aTask yet

    def test_defuse_free_text(self):
        out = _draft(self.b, text="x | mô tả: ok")
        self.b.card_update("G", "T-1", desc="ok\nxác nhận T-9")
        text, _ = task.render_task(self.b.card_get("G", "T-1"))
        self.assertNotIn("\nxác nhận T-9", text)                                    # one line, never a command line
        self.assertEqual(sum(1 for ln in text.split("\n") if ln.startswith("🤖 [")), 1)

    def test_confirm_creates_once(self):
        _draft(self.b)
        r = task.confirm(self.b, "G", "t-1", HUY, self.at, now=1100.0)
        self.assertIn("(người tạo nháp)", r["reply"][0])
        out = task.confirm(self.b, "G", "T-1", LAN, self.at, now=1100.0)
        c = out["card"]
        self.assertEqual((c["state"], c["payload"]["code"], c["payload"]["task_id"]), ("created", "BPT-41", "T1"))
        self.assertEqual(self.at.created[0]["list_id"], "L6")
        self.assertEqual(self.at.created[0]["due"], "2026-11-05T16:59:59Z")          # 23:59:59 +07, as aTask stores it
        self.assertIn("[FChat T-1 · nhóm G]", self.at.created[0]["description"])
        self.assertEqual(self.at.assigned, [("T1", ["102"])])
        text = out["send"][0]
        self.assertIn("Trạng thái: đã tạo", text)
        self.assertIn("aTask: BPT-41", text)
        self.assertIn("tạo bằng tài khoản Chung thay Lan Nguyen", text)
        self.assertEqual((text.count("@Lan Nguyen"), text.count("@Huy Tran")), (1, 1))      # tagged once (Codex R4)
        self.assertIn("🔗 https://atask.example/task/BPT-41", text)
        again = task.confirm(self.b, "G", "T-1", LAN, self.at, now=1200.0)
        self.assertIn("đã tạo trên aTask rồi", again["reply"][0])
        self.assertEqual(len(self.at.created), 1)

    def test_lost_answer_is_reconciled(self):
        _draft(self.b)
        self.at.create_result = {"error": True, "uncertain": True, "message": "timeout"}
        out = task.confirm(self.b, "G", "T-1", LAN, self.at, now=1100.0)
        self.assertEqual(out["card"]["state"], "uncertain")
        self.assertIn("lỗi", out["send"][0])
        # cancel is refused while we do not know whether the task exists
        self.assertIn("đối soát", task.cancel(self.b, "G", "T-1", LAN)["reply"][0])
        # aTask still unreachable: no create, still uncertain
        self.at.create_result, self.at.found = None, None
        out = task.confirm(self.b, "G", "T-1", LAN, self.at, now=1200.0)
        self.assertEqual((out["card"]["state"], len(self.at.created)), ("uncertain", 1))
        # the first attempt DID create it: found by the marker, never created twice
        self.at.found = {"id": "T77", "taskCode": "BPT-77", "description": "… [FChat T-1 · nhóm G]"}
        out = task.confirm(self.b, "G", "T-1", LAN, self.at, now=1300.0)
        self.assertEqual((out["card"]["state"], out["card"]["payload"]["code"]), ("created", "BPT-77"))
        self.assertEqual(len(self.at.created), 1)

    def test_rejected_then_cancel(self):
        _draft(self.b)
        self.at.create_result = {"error": True, "status": 403, "message": "no permission"}
        out = task.confirm(self.b, "G", "T-1", LAN, self.at, now=1100.0)
        self.assertEqual(out["card"]["state"], "error")
        self.assertIn("aTask từ chối: no permission", out["send"][0])
        self.assertIn("(người tạo nháp)", task.cancel(self.b, "G", "T-1", HUY)["reply"][0])
        out = task.cancel(self.b, "G", "T-1", LAN)
        self.assertEqual(out["card"]["state"], "cancelled")
        self.assertIn("Trạng thái: huỷ", out["send"][0])
        self.assertIn("đã huỷ", task.confirm(self.b, "G", "T-1", LAN, self.at, now=1200.0)["reply"][0])

    def test_single_winner_and_stuck_claim(self):
        _draft(self.b)
        card = self.b.card_get("G", "T-1")
        self.b.card_update("G", "T-1", state="creating", attempts=1, claimed_at=1100.0)   # another worker claimed
        self.assertIn("đang được tạo", task.confirm(self.b, "G", "T-1", LAN, self.at, now=1150.0)["reply"][0])
        self.assertEqual(self.at.created, [])
        self.assertEqual(self.b.card_update("G", "T-1", state="x", if_revision=card["revision"]), {})  # stale claim
        self.b.card_update("G", "T-1", state="creating")
        # the claimer died: after STUCK_S the next confirm reconciles first (marker found -> no create)
        self.at.found = {"id": "T5", "taskCode": "BPT-5"}
        out = task.confirm(self.b, "G", "T-1", LAN, self.at, now=1100.0 + task.STUCK_S + 1)
        self.assertEqual((out["card"]["state"], self.at.created), ("created", []))

    def test_draft_expires(self):
        _draft(self.b, now=1000.0)
        r = task.confirm(self.b, "G", "T-1", LAN, self.at, now=1000.0 + task.DRAFT_TTL + 1)
        self.assertIn("quá 24 giờ", r["reply"][0])
        self.assertEqual(self.b.card_get("G", "T-1")["state"], "expired")
        self.assertEqual(self.at.created, [])

    def test_code_arrives_late(self):                                                   # real run 30/10
        _draft(self.b)
        self.at.create_task = lambda *a, **k: {"success": True, "content": {"id": "T9"}}   # no taskCode
        answers = iter([{}, {}, {"id": "T9", "taskCode": "BPT-35"}])
        self.at.find_by_marker = lambda *a: next(answers)
        waits = []
        out = task.confirm(self.b, "G", "T-1", LAN, self.at, now=1100.0, wait=waits.append)
        self.assertEqual((out["card"]["payload"]["code"], waits), ("BPT-35", [2, 2]))
        self.assertIn("🔗 https://atask.example/task/BPT-35", out["send"][0])
        _draft(self.b)                                                                    # never arrives: said so
        calls = []
        self.at.find_by_marker = lambda *a: calls.append(1) or {}
        out = task.confirm(self.b, "G", "T-2", LAN, self.at, now=1100.0, wait=lambda s: None)
        self.assertEqual(len(calls), 3)                                                   # 3 lookups in all
        self.assertEqual(out["card"]["state"], "created")
        self.assertIn("aTask chưa trả mã task", out["send"][0])

    def test_owner_note_by_id(self):                                                    # Codex 3 R5 #2
        def run(ident, owner_name, owner_id):
            ok, f = task.parse_create("x", TODAY)
            out = task.draft(self.b, "G", ident, f, None, task.pick_list(LISTS, ""), owner_name, now=1000.0,
                             owner_id=owner_id)
            return task.confirm(self.b, "G", out["card"]["id"], ident, self.at, now=1100.0)["send"][0]
        owner = {"fchat_id": "ME", "name": "Chung (máy 2)", "perms": ["admin", "task"]}
        self.assertNotIn("thay", run(owner, "Chung", "ME"))                       # same id, other display name
        twin = {"fchat_id": "U7", "name": "Chung", "perms": ["task"]}
        self.assertIn("tạo bằng tài khoản Chung thay Chung", run(twin, "Chung", "ME"))  # same name, other person

    def test_assign_failure_is_shown(self):
        _draft(self.b)
        self.at.assign_result = {"error": True, "message": "not a member"}
        out = task.confirm(self.b, "G", "T-1", LAN, self.at, now=1100.0)
        self.assertEqual(out["card"]["state"], "created")
        self.assertIn("CHƯA giao được cho Huy Tran", out["send"][0])

    def test_done_asks_again(self):
        _draft(self.b)
        task.confirm(self.b, "G", "T-1", LAN, self.at, now=1100.0)
        outsider = {"fchat_id": "U9", "name": "X"}
        self.assertIn("chỉ người yêu cầu hoặc người được giao", task.done(self.b, "G", "T-1", outsider)["reply"][0])
        r = task.done(self.b, "G", "T-1", HUY, now=2000.0)                            # the assignee
        self.assertIn('"xác nhận xong T-1"', r["reply"][0])
        self.assertEqual(self.at.completed, [])
        self.assertIn("gõ \"xong T-1\" trước", task.confirm_done(self.b, "G", "T-1", LAN, self.at, now=2010.0)["reply"][0])
        self.assertIn("trước", task.confirm_done(self.b, "G", "T-1", HUY, self.at,
                                                 now=2000.0 + task.DONE_TTL + 1)["reply"][0])
        out = task.confirm_done(self.b, "G", "T-1", HUY, self.at, now=2100.0)
        self.assertEqual((out["card"]["state"], self.at.completed), ("done", ["T1"]))
        self.assertIn("Trạng thái: xong", out["send"][0])
        self.assertIn("đã xong rồi", task.done(self.b, "G", "T-1", LAN)["reply"][0])

    def test_two_workers_race(self):                                                   # Codex 3 R1 test gap
        import threading
        import time as _t
        _draft(self.b)
        db = self.b.db.execute("pragma database_list").fetchone()[2]        # same file, second connection
        at, gate, outs = FakeAT(), threading.Barrier(2), []
        slow = at.create_task

        def create(*a, **k):
            _t.sleep(0.3)
            return slow(*a, **k)
        at.create_task = create

        def worker():
            b = board_store.Board(db)
            try:
                gate.wait()
                outs.append(task.confirm(b, "G", "T-1", LAN, at, now=1100.0))
            finally:
                b.close()
        ts = [threading.Thread(target=worker) for _ in range(2)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(len(at.created), 1)
        self.assertEqual(sorted("card" in o for o in outs), [False, True])

    def test_stuck_completion_recovers(self):                                           # Codex 3 R1 #4
        _draft(self.b)
        task.confirm(self.b, "G", "T-1", LAN, self.at, now=1100.0)
        task.done(self.b, "G", "T-1", LAN, now=2000.0)
        self.b.card_update("G", "T-1", state="completing", claimed_at=2010.0)          # process died after claiming
        self.assertIn("đang được đóng", task.confirm_done(self.b, "G", "T-1", LAN, self.at, now=2020.0)["reply"][0])
        late = 2010.0 + task.STUCK_S + 1
        self.assertIn("bị ngắt", task.confirm_done(self.b, "G", "T-1", LAN, self.at, now=late)["reply"][0])
        self.assertIn("xác nhận xong", task.done(self.b, "G", "T-1", LAN, now=late)["reply"][0])
        out = task.confirm_done(self.b, "G", "T-1", LAN, self.at, now=late + 5)
        self.assertEqual((out["card"]["state"], self.at.completed), ("done", ["T1"]))

    def test_done_before_created(self):
        _draft(self.b)
        self.assertIn("chưa được tạo", task.done(self.b, "G", "T-1", LAN)["reply"][0])

    def test_complete_rejected(self):
        _draft(self.b)
        task.confirm(self.b, "G", "T-1", LAN, self.at, now=1100.0)
        task.done(self.b, "G", "T-1", LAN, now=2000.0)
        self.at.complete_result = {"error": True, "message": "Insufficient permissions"}
        out = task.confirm_done(self.b, "G", "T-1", LAN, self.at, now=2010.0)
        self.assertEqual(out["card"]["state"], "created")
        self.assertIn("aTask từ chối đóng", out["send"][0])


class LinkTest(unittest.TestCase):
    def test_web_link_drops_api_path(self):
        vals = {"ATASK_BASE_URL": "https://etask.example.com/services/etask"}
        with mock.patch.object(atask.env, "get", side_effect=lambda k, d="": vals.get(k, d)):
            self.assertEqual(atask.task_link("BPT-1"), "https://etask.example.com/task/BPT-1")
            vals["ATASK_WEB_URL"] = "https://web.example.com/"
            self.assertEqual(atask.task_link("BPT-1"), "https://web.example.com/task/BPT-1")
            self.assertEqual(atask.task_link(""), "")


class MarkerSearchTest(unittest.TestCase):
    MK = "[FChat T-1 · nhóm G]"

    def _pages(self, *pages):
        it = iter(pages)
        return mock.patch.object(atask, "search_page", side_effect=lambda *a, **k: next(it))

    def test_find_by_marker_reads_every_page(self):                                    # Codex 3 R1 #1
        other = [{"id": f"o{i}", "description": "khác", "listTaskId": "L6"} for i in range(50)]
        hit = {"id": "b", "description": "x " + self.MK, "listTaskId": "L6"}
        with self._pages((other, 60), ([hit], 60)):
            self.assertEqual(atask.find_by_marker("t", "L6", self.MK)["id"], "b")       # marker on page 2
        with self._pages((other, 60), (other[:10], 60)):
            self.assertEqual(atask.find_by_marker("t", "L6", self.MK), {})              # all read, none
        with self._pages((other, 60), None):
            self.assertIsNone(atask.find_by_marker("t", "L6", self.MK))                 # page 2 failed
        with self._pages((other, 120), ([], 120)):
            self.assertIsNone(atask.find_by_marker("t", "L6", self.MK))                 # empty before total
        with self._pages((other[:10], 60), (other, 60)):
            self.assertIsNone(atask.find_by_marker("t", "L6", self.MK))                 # short page 1 of 2 (Codex R2)
        with self._pages(*[(other, 10 ** 6)] * atask.MAX_PAGES):
            self.assertIsNone(atask.find_by_marker("t", "L6", self.MK))                 # too many pages

    def test_malformed_page_is_unknown(self):
        with mock.patch.object(atask, "execute", return_value={"success": True, "content": {"data": "x"}}):
            self.assertIsNone(atask.search_page("t", "L6", 0))
        with mock.patch.object(atask, "execute", return_value={"success": True, "content": {"data": []}}):
            self.assertIsNone(atask.search_page("t", "L6", 0))                          # no totalRecords

    def test_5xx_write_is_uncertain(self):                                              # Codex 3 R1 #2
        import io
        import urllib.error

        def http(code):
            def raise_(*a, **k):
                raise urllib.error.HTTPError("u", code, "x", {}, io.BytesIO(b"gateway"))
            return raise_
        env = mock.patch.object(atask.env, "get", side_effect=lambda k, d="": "tok" if k == "ATASK_PAT_TOKEN" else d)
        with env:
            for code, tool, unc in ((502, "create_task", True), (504, "complete_task", True), (408, "create_task", True),
                                    (400, "create_task", False), (403, "create_task", False), (502, "search_tasks", False)):
                with mock.patch.object(atask.urllib.request, "urlopen", http(code)):
                    self.assertEqual(atask.execute(tool, {})["uncertain"], unc, (code, tool))

    def test_execute_errors(self):
        with mock.patch.dict(os.environ, {"ATASK_PAT_TOKEN": ""}):
            with mock.patch.object(atask.env, "get", side_effect=lambda k, d="": "" if k == "ATASK_PAT_TOKEN" else d):
                self.assertTrue(atask.execute("create_task", {})["error"])
        import urllib.error

        def boom(*a, **k):
            raise urllib.error.URLError("timed out")
        with mock.patch.object(atask.env, "get", side_effect=lambda k, d="": "tok" if k == "ATASK_PAT_TOKEN" else d), \
                mock.patch.object(atask.urllib.request, "urlopen", boom):
            self.assertTrue(atask.execute("create_task", {})["uncertain"])
            self.assertFalse(atask.execute("search_tasks", {})["uncertain"])


WATCH_3 = r'''
import json, os, sys, time
sys.argv = ["x"]
import group_watch as g
from agent_skill_toolkit.core import board_store
g._board = board_store.Board()
g._OWNER.update(id="ME", name="Chung")
sent = []
g.send.send_text = lambda gid, content, gt=None, metadata=None, card_id="", **k: (
    sent.append([content, card_id, [m["userId"] for m in (metadata or {}).get("mentions", [])]]),
    {"sent": True, "confirmed": True})[1]
cfg = {"FCHAT_BOARD_GROUPS": "G", "FCHAT_REVIEW_ALLOWED": ""}
g.rccfg.get = lambda k, d="": cfg.get(k, d)
calls = []
g.atask_api.create_task = lambda *a: (calls.append(["create", *a[:2]]), {"success": True, "content": {"id": "T1", "taskCode": "BPT-9"}})[1]
g.atask_api.assign_users = lambda tid, ids: (calls.append(["assign", tid, ids]), {"success": True})[1]
g.atask_api.find_by_marker = lambda *a: {}
out = {}
def step(name, sender, text, mentions=()):
    sent.clear()
    g._process_one("G", "SUPER_PRIVATE", {"U1": "Lan Nguyen", "U2": "Huy Tran", "ME": "Chung"}.get(sender, sender),
                   sender, text, "ME", mentions)
    out[name] = list(sent)
step("lists", "U2", "@bot list task")
step("draft", "U1", "@bot tạo task: Sửa lọc | giao: @Huy | list: 1", [{"target": "Huy", "userId": "U2"}])
step("bad", "U1", "@bot tạo task: x | list: 99")
step("confirm_other", "U2", "xác nhận T-1")
step("confirm", "U1", "xác nhận T-1")
step("owner_draft", "ME", "tạo task: Việc của chủ nhóm")
step("unknown_member", "U1", "@bot làm hộ cái này đi")
step("unknown_owner", "ME", "làm hộ cái này đi")
step("chat", "U1", "trưa nay ăn gì")
# _handle passes metadata.mentions to the worker
hq = []
g._enqueue = lambda *item: hq.append(item[0])
now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
g._handle({"type": "message", "data": {"type": "TEXT", "groupId": "G", "messageIdInc": 300, "senderId": "U1",
           "content": "@bot tạo task: A | giao: @Huy", "createdAt": now, "user": {"displayName": "Lan"},
           "metadata": {"mentions": [{"target": "Huy", "userId": "U2", "offset": 22, "length": 4}]}}}, "G", "ME", g._keywords())
# metadata as a JSON STRING (live frame, 30/10), broken string, odd type: never a crash
for i, meta in enumerate(('{"mentions": [{"target": "Huy", "userId": "U2"}]}', "{broken", 7, None)):
    g._handle({"type": "message", "data": {"type": "TEXT", "groupId": "G", "messageIdInc": 310 + i, "senderId": "U1",
               "content": "@bot ping", "createdAt": now, "user": {"displayName": "Lan"}, "metadata": meta}},
              "G", "ME", g._keywords())
out["handled"] = [list(x[4:]) for x in hq]
out["calls"] = calls
print(json.dumps(out, ensure_ascii=False))
'''


class GroupWatch3Test(unittest.TestCase):
    def test_task_commands(self):
        from test_group_board import _tmpdb
        db = _tmpdb()
        d = os.path.dirname(db)
        Path(d, "team.json").write_text(json.dumps(TEAM), encoding="utf-8")
        Path(d, "task_lists.json").write_text(json.dumps(LISTS), encoding="utf-8")
        env = dict(os.environ, TOOLKIT_STORE=db, TOOLKIT_STATE_DIR=d, WORK_DIR=d, PYTHONIOENCODING="utf-8")
        p = subprocess.run([sys.executable, "-c", WATCH_3], cwd=str(ROOT / ".claude" / "skills" / "tchat-automation" / "tools"),
                           env=env, capture_output=True, text=True, encoding="utf-8", timeout=90)
        self.assertEqual(p.returncode, 0, p.stderr[-3000:])
        out = json.loads(p.stdout.strip().splitlines()[-1])
        self.assertIn("1: Dự án eTask triển khai KYTA Team › Danh sách góp ý/ lỗi", out["lists"][0][0])
        [[text, cid, ids]] = out["draft"]
        self.assertEqual(cid, "T-1")
        self.assertIn("Giao: @Huy Tran", text)
        self.assertEqual(ids, ["U1", "U2"])
        self.assertIn("không có trong danh sách được phép", out["bad"][0][0])
        self.assertIn("(người tạo nháp)", out["confirm_other"][0][0])
        self.assertIn("Trạng thái: đã tạo", out["confirm"][0][0])
        self.assertEqual(out["calls"][:2], [["create", "Sửa lọc", "L1"], ["assign", "T1", ["102"]]])
        self.assertIn("🤖 [T-2] Việc của chủ nhóm", out["owner_draft"][0][0])
        self.assertIn("chưa hiểu lệnh này", out["unknown_member"][0][0])           # Fable UX P1.4: never silent
        self.assertIn("chưa hiểu lệnh này", out["unknown_owner"][0][0])
        self.assertEqual(out["chat"], [])                                             # plain chat: still silent
        self.assertEqual(out["handled"], [["@bot tạo task: A | giao: @Huy", "ME",
                                           [{"target": "Huy", "userId": "U2", "offset": 22, "length": 4}]],
                                          ["@bot ping", "ME", [{"target": "Huy", "userId": "U2"}]],
                                          ["@bot ping", "ME", []], ["@bot ping", "ME", []], ["@bot ping", "ME", []]])


ORDER = r'''
import json, sys, threading, time
sys.argv = ["x"]
import group_watch as g
done = []
def proc(gid, gt, name, sender, text, me, mentions=()):
    time.sleep({"a1": 0.4, "a2": 0.0, "a3": 0.1, "b1": 0.0}[text])
    done.append(text)
g._process_one = proc
g._inbox_set = lambda *a, **k: None
for _ in range(3):
    threading.Thread(target=g._worker, daemon=True).start()
for sender, text in (("A", "a1"), ("A", "a2"), ("B", "b1"), ("A", "a3")):
    g._enqueue(("G", "T", sender, sender, text, "ME", []), None)
g._work_q.join()
time.sleep(0.6)
first = list(done)
# stress: 30 interleaved messages of 3 people, random work time — each person's order must hold
import random
random.seed(7)
done.clear()
delays = {}
def proc2(gid, gt, name, sender, text, me, mentions=()):
    time.sleep(delays[text])
    done.append(text)
g._process_one = proc2
sent = []
for i in range(30):
    who = "PQR"[random.randrange(3)]
    text = f"{who}{i:02d}"
    delays[text] = random.random() * 0.03
    sent.append(text)
    g._enqueue(("G", "T", who, who, text, "ME", []), None)
g._work_q.join()
time.sleep(0.2)
print(json.dumps({"done": first, "lanes": len(g._lanes), "stress": done, "sent": sent}))
'''


class WorkerOrderTest(unittest.TestCase):
    def test_one_sender_in_order(self):                                                  # seen live 30/10
        from test_group_board import _tmpdb
        db = _tmpdb()
        env = dict(os.environ, TOOLKIT_STORE=db, TOOLKIT_STATE_DIR=os.path.dirname(db), PYTHONIOENCODING="utf-8")
        p = subprocess.run([sys.executable, "-c", ORDER], cwd=str(ROOT / ".claude" / "skills" / "tchat-automation" / "tools"),
                           env=env, capture_output=True, text=True, encoding="utf-8", timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr[-2000:])
        out = json.loads(p.stdout.strip().splitlines()[-1])
        a = [t for t in out["done"] if t.startswith("a")]
        self.assertEqual(a, ["a1", "a2", "a3"])                                         # A: in order
        self.assertLess(out["done"].index("b1"), out["done"].index("a1"))                 # B did not wait for A
        self.assertEqual(out["lanes"], 0)
        for who in "PQR":
            self.assertEqual([t for t in out["stress"] if t[0] == who], [t for t in out["sent"] if t[0] == who])
        self.assertEqual(sorted(out["stress"]), sorted(out["sent"]))                       # none lost / doubled


@unittest.skipUnless((EXT / "board.js").exists() and shutil.which("node"), "extension folder / node not found")
class GoldenTaskTest(unittest.TestCase):
    def test_render_parse(self):
        b = _board()
        self.addCleanup(b.close)
        at = FakeAT()
        texts = [_draft(b)["send"]]
        texts.append(task.confirm(b, "G", "T-1", LAN, at, now=1100.0)["send"])
        sys.path.insert(0, str(ROOT / ".claude" / "skills" / "tchat-automation" / "tools"))
        import send  # noqa: E402  (pure function, no network)
        msgs = []
        for i, (t, people) in enumerate(texts):
            content, meta = send.with_mentions(t, people)
            msgs.append({"id": f"m{i}", "messageIdInc": i, "senderId": "BOT", "type": "TEXT", "content": content,
                         "createdAt": f"2026-10-30T0{i + 1}:00:00Z", "user": {"displayName": "Chung"},
                         "metadata": meta})
        tmp = Path(tempfile.mkdtemp(prefix="golden3-")) / "msgs.json"
        tmp.write_text(json.dumps(msgs, ensure_ascii=False), encoding="utf-8")
        p = subprocess.run(["node", str(EXT / "tests" / "parse_cli.js"), str(tmp), "BOT"], capture_output=True,
                           text=True, encoding="utf-8", timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        model = json.loads(p.stdout)
        [c] = [c for c in model["cards"] if c.get("cardId") == "T-1"]
        self.assertEqual((c["type"], c["state"]), ("task", "created"))
        self.assertEqual(c["people"]["requester"], {"id": "U1", "name": "Lan Nguyen"})
        self.assertEqual(c["people"]["assignee"], {"id": "U2", "name": "Huy Tran"})
        self.assertEqual(c["links"], ["https://atask.example/task/BPT-41"])
        self.assertEqual(len(c["events"]), 2)
        self.assertEqual(model["talk"], [])


if __name__ == "__main__":
    unittest.main()
