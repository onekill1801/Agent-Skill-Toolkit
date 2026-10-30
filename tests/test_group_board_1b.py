"""Group workboard phase 1b (docs/design/group-workboard.md §3.1, §3.4): identity + permissions from
work/team.json (default member = review+task, owner = admin), anchored member commands (`review lại R-n`,
`@bot tôi là ai`, `@bot ping`, `@bot help`), the review policy in workboard groups, team.py perms/verify."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".claude" / "skills" / "_runtime"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from agent_skill_toolkit.core import identity, policy  # noqa: E402
from agent_skill_toolkit.workflows.group_board import commands  # noqa: E402

TEAM = {
    "lan": {"name": "Lan", "handles": {"fchat_id": "U1", "gitlab_username": "lan.nt", "atask_user_id": 42},
            "verified_by": "Chung", "personality": "SECRET-NOTE"},
    "huy": {"name": "Huy", "handles": {"fchat_id": "U2"}, "perms": ["task", "bogus", "deploy:prod"]},
    "dup1": {"handles": {"fchat_id": "U9"}}, "dup2": {"handles": {"fchat_id": "U9"}},
}


class IdentityPolicyTest(unittest.TestCase):
    def test_resolve(self):
        lan = identity.resolve("U1", "Lan", "ME", TEAM)
        self.assertEqual((lan["key"], lan["gitlab"], lan["atask"], lan["verified"]), ("lan", "lan.nt", "42", True))
        self.assertEqual((lan["perms"], lan["perms_source"]), (["review", "task"], "default"))
        huy = identity.resolve("U2", "Huy", "ME", TEAM)
        self.assertEqual((huy["perms"], huy["perms_source"]), (["task"], "team.json"))   # invalid ones dropped
        stranger = identity.resolve("U7", "Ai đó", "ME", TEAM)
        self.assertEqual((stranger["key"], stranger["perms"]), (None, ["review", "task"]))
        amb = identity.resolve("U9", "", "ME", TEAM)                                      # ambiguous link
        self.assertEqual((amb["key"], amb["perms"], amb["perms_source"]), (None, [], "ambiguous"))
        me = identity.resolve("ME", "Chung", "ME", TEAM)
        self.assertEqual((me["owner"], me["perms_source"]), (True, "owner"))

    def test_malformed_perms_fail_closed(self):
        """Codex 1b R1 #1: a perms field of the wrong type never falls back to the defaults."""
        for bad in ("task", None, {}, ["review", 3]):
            team = {"x": {"handles": {"fchat_id": "U5"}, "perms": bad}}
            r = identity.resolve("U5", "X", "ME", team)
            self.assertEqual((r["perms"], r["perms_source"]), ([], "invalid"), bad)
            self.assertFalse(policy.check("review", r)[0])
        both_empty = {"a": {"handles": {"fchat_id": "U6"}, "perms": []}, "b": {"handles": {"fchat_id": "U6"}, "perms": []}}
        self.assertEqual(identity.resolve("U6", "", "ME", both_empty)["perms"], [])           # Codex 1b R1 #2

    def test_policy(self):
        self.assertTrue(policy.check("review", {"perms": ["review"]})[0])
        self.assertTrue(policy.check("review", {"perms": ["admin"]})[0])
        ok, why = policy.check("review", {"perms": ["task"]})
        self.assertFalse(ok)
        self.assertIn("review", why)
        self.assertFalse(policy.check("deploy", {"perms": ["admin"]})[0])                # not in phase 1b

    def test_broken_registry_raises(self):
        d = tempfile.mkdtemp(prefix="team-")
        Path(d, "team.json").write_text("{nope", encoding="utf-8")
        with mock.patch.dict(os.environ, {"WORK_DIR": d}):
            with self.assertRaises(ValueError):
                identity.load_team()
        with mock.patch.dict(os.environ, {"WORK_DIR": tempfile.mkdtemp(prefix="team-")}):
            self.assertEqual(identity.load_team(), {})


class CommandsTest(unittest.TestCase):
    def test_parse(self):
        P = commands.parse
        self.assertEqual(P("review lại R-7"), ("review_again", ("R-7",)))
        self.assertEqual(P("@bot review lại r-12 nhé"), ("review_again", ("r-12",)))
        self.assertEqual(P("@bot tôi là ai?"), ("whoami", ()))
        self.assertIsNone(P("tôi là ai"))                       # members need the prefix
        self.assertEqual(P("tôi là ai", prefixed=True), ("whoami", ()))
        self.assertEqual(P("@bot ping"), ("ping", ()))
        self.assertEqual(P("@bot: help"), ("help", ()))
        self.assertIsNone(P("nhờ ai review lại R-7 với"))       # anchored at the start
        self.assertIsNone(P("@bot deploy etask lên dev"))       # not a phase 1b command

    def test_whoami_text(self):
        text, people = commands.whoami_text(identity.resolve("U1", "Lan", "ME", TEAM), "Chung")
        self.assertIn("GitLab: lan.nt", text)
        self.assertIn("✅ đã xác minh", text)
        self.assertIn("Quyền: review, task (mặc định thành viên)", text)
        self.assertNotIn("SECRET", text)                        # never profile notes
        self.assertEqual(people, [("Lan", "U1")])
        text, _ = commands.whoami_text(identity.resolve("U7", "Ai", "ME", TEAM), "Chung")
        self.assertIn("chưa liên kết GitLab/aTask — nhờ Chung đăng ký", text)
        text, _ = commands.whoami_text(identity.resolve("ME", "Chung", "ME", TEAM), "Chung")
        self.assertIn("chủ nhóm", text)
        for ln in text.split("\n")[1:]:
            self.assertFalse(ln.startswith(("@bot", "🤖 [")))

    def test_ping_text(self):
        t = commands.ping_text({"age": 12.4}, {"commands": 3}, [{"id": "R-2"}, {"id": "R-5"}])
        self.assertIn("heartbeat 12 giây trước", t)
        self.assertIn("review đang chờ: R-2, R-5", t)
        self.assertIn("chưa có heartbeat", commands.ping_text({}, {}, []))


class TeamCliTest(unittest.TestCase):
    def run_team(self, *args):
        p = subprocess.run([sys.executable, str(ROOT / ".claude" / "skills" / "team-registry" / "tools" / "team.py"),
                            *args], env=dict(os.environ, WORK_DIR=self.dir, PYTHONIOENCODING="utf-8"),
                           capture_output=True, text=True, encoding="utf-8", timeout=60)
        return p.returncode, json.loads(p.stdout)

    def test_perms_and_verify(self):
        self.dir = tempfile.mkdtemp(prefix="teamcli-")
        Path(self.dir, "team.json").write_text(json.dumps({"lan": {"name": "Lan", "handles": {}}}), encoding="utf-8")
        rc, out = self.run_team("perms", "lan", "--add", "deploy:dev", "--remove", "task")
        self.assertEqual((rc, out["perms"], out["explicit"]), (0, ["review", "deploy:dev"], True))
        rc, out = self.run_team("perms", "lan", "--add", "deploy:prod")
        self.assertEqual(rc, 1)
        self.assertIn("không hợp lệ", out["message"])
        rc, out = self.run_team("perms", "lan", "--default")
        self.assertEqual((out["perms"], out["explicit"]), (["review", "task"], False))
        rc, out = self.run_team("verify", "lan", "--by", "Chung")
        self.assertEqual(rc, 1)                                  # no fchat_id yet
        self.run_team("set", "lan", "--fchat-id", "U1")
        rc, out = self.run_team("verify", "lan", "--by", "Chung")
        self.assertEqual((rc, out["verified_by"]), (0, "Chung"))
        with mock.patch.dict(os.environ, {"WORK_DIR": self.dir}):
            self.assertTrue(identity.resolve("U1", "Lan", "ME")["verified"])


WATCH_1B = r'''
import json, os, sys, time
sys.argv = ["x"]
import group_watch as g
from agent_skill_toolkit.core import board_store
g._board = board_store.Board()
g._OWNER.update(id="ME", name="Chung")
sent, tools, queued = [], [], []
g.send.send_text = lambda gid, content, gt=None, metadata=None, card_id="", **k: (
    sent.append(content), {"sent": True, "confirmed": True})[1]
g._auto_review_state = lambda: (None, None, False)
cfg = {"FCHAT_BOARD_GROUPS": "G", "FCHAT_REVIEW_ALLOWED": ""}
g.rccfg.get = lambda k, d="": cfg.get(k, d)
JOB = {"id": "5401-1388", "project_id": 5401, "iid": 1388, "project_path": "idaas/etask", "title": "Fix",
       "web_url": "https://gitlab.fis.vn/idaas/etask/-/merge_requests/1388", "head_sha": "a"}
def run_tool(script, *args, timeout=120):
    tools.append([script, *args])
    if script == "review_flow.py" and args[0] == "enqueue":
        return {"job": {**JOB, "head_sha": "b"}, "job_id": JOB["id"]}
    return {"added": [JOB["id"]], "refused": [], "unchanged": [], "jobs": [{**JOB, "same_head": False}]}
g._run_tool = run_tool
out = {}
def step(name, sender, text, me="ME"):
    sent.clear(); tools.clear()
    g._process_one("G", "SUPER_PRIVATE", {"U1": "Lan", "U2": "Huy", "ME": "Chung"}.get(sender, sender), sender, text, me)
    out[name] = {"sent": list(sent), "tools": [t[:2] for t in tools]}
step("member_review", "U1", "review giúp https://gitlab.fis.vn/idaas/etask/-/merge_requests/1388")
step("denied_review", "U2", "review giúp https://gitlab.fis.vn/idaas/etask/-/merge_requests/1388")
step("whoami", "U1", "@bot tôi là ai")
step("owner_ping", "ME", "ping")                       # owner path: '@bot' already stripped
step("help", "U2", "@bot help")
step("again", "U1", "review lại R-1")
step("again_missing", "U1", "review lại R-99")
step("again_denied", "U2", "review lại R-1")
# _handle prefilter: '@bot tôi là ai' has no review keyword but is a board command
kws = g._keywords()
hq = []
g._work_q.put = lambda item: hq.append(item[0][4])
now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
for i, (snd, txt) in enumerate((("U1", "@bot tôi là ai"), ("U1", "tôi là ai"), ("U2", "@bot ping"))):
    g._handle({"type": "message", "data": {"type": "TEXT", "groupId": "G", "messageIdInc": 100 + i, "senderId": snd,
               "content": txt, "createdAt": now, "user": {"displayName": snd}}}, "G", "ME", kws)
out["handled"] = hq
# catch-up after a restart: a board command sent while the bot was down is reported (Codex 1b R1 #3)
cfg["FCHAT_CATCHUP_REPLY"] = "1"
g._board.raise_watermark("G", 200)
iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 60))
hist = {"regulars": [{"type": "TEXT", "id": "h1", "messageIdInc": 201, "senderId": "U1", "content": "@bot tôi là ai",
                      "createdAt": iso, "user": {"displayName": "Lan"}},
                     {"type": "TEXT", "id": "h2", "messageIdInc": 202, "senderId": "U2", "content": "review lại R-1",
                      "createdAt": iso, "user": {"displayName": "Huy"}}]}
g.client.api_get = lambda path, params=None: hist
sent.clear()
g._catch_up("G", "ME", kws)
out["catchup"] = list(sent)
# broken registry -> fail closed
open(os.path.join(os.environ["WORK_DIR"], "team.json"), "w").write("{broken")
step("broken", "U1", "review giúp https://gitlab.fis.vn/idaas/etask/-/merge_requests/1388")
print(json.dumps(out, ensure_ascii=False))
'''


class GroupWatch1bTest(unittest.TestCase):
    def test_commands_and_policy(self):
        from test_group_board import _tmpdb
        db = _tmpdb()
        Path(os.path.dirname(db), "team.json").write_text(json.dumps(TEAM), encoding="utf-8")
        env = dict(os.environ, TOOLKIT_STORE=db, TOOLKIT_STATE_DIR=os.path.dirname(db),
                   WORK_DIR=os.path.dirname(db), PYTHONIOENCODING="utf-8")
        p = subprocess.run([sys.executable, "-c", WATCH_1B], cwd=str(ROOT / ".claude" / "skills" / "tchat-automation" / "tools"),
                           env=env, capture_output=True, text=True, encoding="utf-8", timeout=90)
        self.assertEqual(p.returncode, 0, p.stderr[-2000:])
        out = json.loads(p.stdout.strip().splitlines()[-1])
        self.assertEqual(out["member_review"]["tools"], [["review_flow.py", "enqueue-text"]])   # default perms
        self.assertEqual(out["member_review"]["sent"], [])                                       # no ack (1a)
        self.assertEqual(out["denied_review"]["tools"], [])
        self.assertTrue(out["denied_review"]["sent"][0].startswith("⛔ @Huy: chưa có quyền 'review'"))
        self.assertIn("GitLab: lan.nt", out["whoami"]["sent"][0])
        self.assertTrue(out["owner_ping"]["sent"][0].startswith("🤖 Bot đang chạy"))
        self.assertIn("review giúp MR", out["help"]["sent"][0])
        self.assertEqual(out["again"]["tools"], [["review_flow.py", "enqueue"]])
        self.assertEqual(out["again"]["sent"], [])                                               # new round: no ack
        self.assertIn("không thấy thẻ R-99", out["again_missing"]["sent"][0])
        self.assertEqual(out["again_denied"]["tools"], [])
        self.assertEqual(out["handled"], ["@bot tôi là ai", "@bot ping"])
        self.assertEqual(len(out["catchup"]), 1)
        self.assertIn("Lan", out["catchup"][0])
        self.assertIn("Huy", out["catchup"][0])
        self.assertEqual(out["broken"]["tools"], [])
        self.assertIn("danh bạ của bot đang lỗi", out["broken"]["sent"][0])


if __name__ == "__main__":
    unittest.main()
