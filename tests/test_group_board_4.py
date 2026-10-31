"""Group workboard phase 4 (docs/design/group-workboard.md §3.4–§3.5, §6): deploy cards D-n — opt-in envs only,
another person approves (the owner via Telegram), the approval binds to branch HEAD + job config, the runner is
durable stage by stage (a lost trigger answer is reconciled, never triggered twice), and "thành công" only when the
pods run the digest the build pushed."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".claude" / "skills" / "_runtime"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from agent_skill_toolkit.core import board_store, policy  # noqa: E402
from agent_skill_toolkit.workflows.group_board import commands, deploy  # noqa: E402

D = "sha256:" + "d" * 64
REG = {"etask": {"gitlab_project_id": 5401, "repo_path": "idaas/etask", "environments": {
    "dev": {"jenkins": {"job": "job/DEV/job/etask", "branch": "merge/rfc"}, "k8s": {"deploy": "etask"},
            "chat_deploy": {"allow_unpinned": True}},
    "sandbox": {"jenkins": {"job": "job/SB/job/etask", "branch": "merge/rfc-into-vitess"}},
    "prod": {"jenkins": {"job": "job/PROD/job/etask"}, "chat_deploy": {"allow_unpinned": True}}}}}
LAN = {"fchat_id": "U1", "name": "Lan", "perms": ["review", "deploy:dev"], "verified": True}
HUY = {"fchat_id": "U2", "name": "Huy", "perms": ["review", "approve:dev"], "verified": True}
MINH = {"fchat_id": "U3", "name": "Minh", "perms": ["review"], "verified": True}
OWNER = {"fchat_id": "ME", "name": "Chung", "perms": ["admin", "review", "task"]}


class FakeOps:
    """GitLab / Jenkins / k8s stand-in: scripted answers, records every trigger."""

    def __init__(self):
        self.head, self.hash = "a" * 40, "h1"
        self.triggers = []
        self.trigger_result = {"queue_url": "http://j/queue/1/"}
        self.queue_result = {"number": 1421, "url": "http://j/1421/"}
        self.build_result = {"building": False, "result": "SUCCESS", "failed_stages": [],
                             "pushes": [{"image": "reg/idaas-etask", "version": "0.0.1275", "digest": D}],
                             "commit_sha": "a" * 40}
        self.builds_result = []
        self.wl = {"spec_images": {"etask": "reg/idaas-etask:0.0.1275"}, "replicas": 1,
                   "pods": [{"phase": "Running", "ready": True, "containers": [{"name": "etask", "image_id": "docker-pullable://reg/idaas-etask@" + D,
                                             "ready": True, "image": "reg/idaas-etask:0.0.1275"}]}]}

    def branch_head(self, pid, branch):
        return self.head

    def job_hash(self, job):
        return self.hash

    def trigger(self, job):
        self.triggers.append(job)
        return self.trigger_result

    def queue(self, url):
        return self.queue_result

    def buildinfo(self, job, n):
        return self.build_result

    def builds(self, job, limit):
        return self.builds_result

    def workload(self, repo, env, target):
        return self.wl

    def jenkins_user(self):
        return "bot"


def _board():
    return board_store.Board(os.path.join(tempfile.mkdtemp(prefix="board4-"), "toolkit.db"))


def _target(env="dev"):
    t, why = deploy.deploy_target(REG, "etask", env)
    assert not why, why
    return t


class TargetAndCommandsTest(unittest.TestCase):
    def test_target(self):
        t = _target()
        self.assertEqual((t["job"], t["branch"], t["workload"], t["unpinned"]),
                         ("job/DEV/job/etask", "merge/rfc", "deployment/etask", True))
        self.assertIn("chỉ cho dev", deploy.deploy_target(REG, "etask", "sandbox")[1])        # hard gate
        self.assertIn("được bảo vệ", deploy.deploy_target(REG, "etask", "prod")[1])         # even when opted in
        self.assertIn("không có repo", deploy.deploy_target(REG, "nope", "dev")[1])
        self.assertIn("chỉ cho dev", deploy.deploy_target(REG, "etask", "demo")[1])
        reg = {"etask": {**REG["etask"], "protected_envs": ["uat"]}}                  # a custom list never frees prod
        self.assertIn("được bảo vệ", deploy.deploy_target(reg, "etask", "prod")[1])
        reg = {"x": {"environments": {"dev": {"jenkins": {"job": "j"}}}}}
        self.assertIn("chưa bật deploy qua chat", deploy.deploy_target(reg, "x", "dev")[1])

    def test_commands_and_policy(self):
        p = commands.parse
        self.assertEqual(p("@bot deploy etask lên dev | lý do: thử"), ("deploy_request", ("etask", "dev", "| lý do: thử")))
        self.assertEqual(deploy.parse_reason("| lý do: thử fix"), "thử fix")
        self.assertEqual(p("duyệt D-3"), ("deploy_approve", ("D-3",)))
        self.assertEqual(p("từ chối D-3: chưa test"), ("deploy_reject", ("D-3", "chưa test")))
        self.assertEqual(p("huỷ D-3"), ("deploy_cancel", ("D-3",)))
        self.assertEqual(p("huỷ T-3"), ("task_cancel", ("T-3",)))
        self.assertEqual(p("deploy lại D-3"), ("deploy_redo", ("D-3",)))
        self.assertTrue(policy.check("deploy:dev", LAN)[0])
        self.assertFalse(policy.check("deploy:dev", HUY)[0])
        self.assertFalse(policy.check("deploy:sandbox", LAN)[0])


class FlowTest(unittest.TestCase):
    def setUp(self):
        self.b = _board()
        self.addCleanup(lambda: self.b.close())
        self.ops = FakeOps()

    def _request(self, now=1000.0):
        out = deploy.request(self.b, "G", LAN, _target(), "thử fix", self.ops, [HUY], "Chung", now=now)
        return out["card"]["id"], out

    def _approved(self, now=1000.0):
        cid, _ = self._request(now=now)
        deploy.approve(self.b, "G", cid, HUY, self.ops, now=now + 100, owner_id="ME")
        return cid

    def _run(self, cid, n=6, t0=1200.0):
        outs = []
        for i in range(n):
            o = deploy.step(self.b, "G", cid, self.ops, now=t0 + i)
            if o:
                outs.append(o)
            if self.b.card_get("G", cid)["state"] != "running":
                break
        return outs

    def test_request_card(self):
        cid, out = self._request()
        text, people = out["send"]
        self.assertTrue(text.startswith(f"🤖 [{cid}] Deploy etask → dev\nTrạng thái: chờ duyệt · Yêu cầu: @Lan"))
        self.assertIn("Nhánh: merge/rfc · HEAD lúc yêu cầu: aaaaaaaa", text)
        self.assertIn("HEAD của nhánh merge/rfc TẠI LÚC CHẠY", text)
        self.assertIn("(@Huy)", text)
        self.assertEqual(people, [("Lan", "U1"), ("Huy", "U2")])
        again = deploy.request(self.b, "G", LAN, _target(), "", self.ops, [], "Chung", now=1001.0)
        self.assertIn(f"đang có {cid}", again["reply"][0])

    def test_who_may_approve(self):
        cid, _ = self._request()

        def a(who, **k):
            return deploy.approve(self.b, "G", cid, who, self.ops, now=1100.0, owner_id="ME", **k)
        self.assertIn("duyệt qua Telegram", a(OWNER)["reply"][0])
        self.assertIn("không tự duyệt", a(LAN)["reply"][0])
        self.assertIn("approve:dev", a(MINH)["reply"][0])
        self.assertIn("chưa được chủ nhóm xác minh", a({**HUY, "verified": False})["reply"][0])
        self.assertEqual(self.b.card_get("G", cid)["state"], "waiting")
        out = a(HUY, requester_ident=LAN)
        c = out["card"]
        self.assertEqual((c["state"], c["payload"]["stage"], c["payload"]["approval"]["via"]), ("running", "trigger", "chat"))
        self.assertIn("Duyệt: @Huy", out["send"][0])
        self.assertIn("không còn chờ duyệt", a(HUY)["reply"][0])

    def test_head_moved_or_config_changed(self):
        cid, _ = self._request()
        self.ops.head = "b" * 40
        out = deploy.approve(self.b, "G", cid, HUY, self.ops, now=1100.0, owner_id="ME")
        self.assertEqual(out["card"]["state"], "waiting")
        self.assertIn("HEAD nhánh merge/rfc đã đổi (aaaaaaaa → bbbbbbbb)", out["send"][0])
        self.assertEqual(out["card"]["payload"]["commit"], "b" * 40)
        self.ops.hash = "h2"
        out = deploy.approve(self.b, "G", cid, HUY, self.ops, now=1200.0, owner_id="ME")
        self.assertEqual(out["card"]["state"], "cancelled")
        self.assertIn("cấu hình job Jenkins đã đổi", out["send"][0])

    def test_expired_lost_perm_reject_cancel_redo(self):
        cid, _ = self._request()
        out = deploy.approve(self.b, "G", cid, HUY, self.ops, now=1000.0 + deploy.APPROVE_TTL + 1, owner_id="ME")
        self.assertEqual(out["card"]["state"], "expired")
        cid, _ = self._request(now=20000.0)
        out = deploy.approve(self.b, "G", cid, HUY, self.ops, now=20100.0, owner_id="ME",
                             requester_ident={**LAN, "perms": ["review"]})
        self.assertIn("không còn quyền deploy:dev", out["send"][0])
        cid, _ = self._request(now=30000.0)
        self.assertIn("approve:dev", deploy.reject(self.b, "G", cid, MINH, "x", owner_id="ME")["reply"][0])
        self.assertIn("chỉ người yêu cầu", deploy.cancel(self.b, "G", cid, HUY)["reply"][0])
        out = deploy.reject(self.b, "G", cid, HUY, "chưa test", owner_id="ME")
        self.assertEqual(out["card"]["state"], "rejected")
        self.assertIn("Kết quả: bị từ chối: chưa test", out["send"][0])
        redo = deploy.redo(self.b, "G", cid, LAN, lambda r, e: (_target(e), ""), self.ops, [HUY], "Chung", now=30100.0)
        self.assertEqual(redo["card"]["state"], "waiting")
        self.assertNotEqual(redo["card"]["id"], cid)
        self.assertIn("đã huỷ", deploy.cancel(self.b, "G", redo["card"]["id"], LAN)["send"][0])

    def test_happy_path(self):
        cid = self._approved()
        outs = self._run(cid)
        self.assertEqual(self.b.card_get("G", cid)["state"], "done")
        self.assertEqual(self.ops.triggers, ["job/DEV/job/etask"])
        self.assertEqual([o["card"]["payload"]["stage"] for o in outs[:3]], ["queued", "building", "rollout"])
        last = outs[-1]["send"][0]
        self.assertIn("Trạng thái: thành công", last)
        self.assertIn("Build: aaaaaaaa · Phiên bản: 0.0.1275", last)
        self.assertIn("1/1 pod chạy đúng image build vừa push", last)
        self.assertIn("🔗 http://j/1421/", last)
        self.assertIsNone(self.b.meta_get("deploylock:etask:dev"))

    def test_failed_stage(self):
        cid = self._approved()
        self.ops.build_result = {**self.ops.build_result, "result": "FAILURE", "failed_stages": ["Set image on k8s"]}
        outs = self._run(cid)
        self.assertEqual(self.b.card_get("G", cid)["state"], "failed")
        self.assertIn("build #1421 FAILURE ở bước Set image on k8s", outs[-1]["send"][0])

    def test_failure_outside_stages_still_verified_by_pods(self):
        cid = self._approved()
        self.ops.build_result = {**self.ops.build_result, "result": "FAILURE", "failed_stages": []}
        outs = self._run(cid)
        self.assertEqual(self.b.card_get("G", cid)["state"], "done")
        self.assertIn("Jenkins ghi FAILURE — lỗi ngoài các bước chính", outs[-1]["send"][0])

    def test_build_commit_differs(self):
        cid = self._approved()
        self.ops.build_result = {**self.ops.build_result, "commit_sha": "c" * 40}
        outs = self._run(cid)
        self.assertEqual(self.b.card_get("G", cid)["state"], "mismatch")
        self.assertIn("⚠ build khác commit đã duyệt (aaaaaaaa → cccccccc)", outs[-1]["send"][0])

    def test_rollout_never_matches(self):
        cid = self._approved()
        self.ops.wl = {**self.ops.wl, "pods": [{"containers": [{"name": "etask", "image_id": "x@sha256:" + "e" * 64,
                                                               "ready": True}]}]}
        self._run(cid, n=3)
        self.assertEqual(self.b.card_get("G", cid)["payload"]["stage"], "rollout")
        built = self.b.card_get("G", cid)["payload"]["built_at"]
        o = deploy.step(self.b, "G", cid, self.ops, now=built + deploy.ROLLOUT_TIMEOUT + 1)
        self.assertEqual(o["card"]["state"], "failed")
        self.assertIn("chưa thấy pod chạy bản mới", o["send"][0])

    def test_k8s_unreadable(self):
        cid = self._approved()
        self.ops.wl = {"error": True, "kind": "timeout"}
        self._run(cid, n=3)
        built = self.b.card_get("G", cid)["payload"]["built_at"]
        self.assertEqual(deploy.step(self.b, "G", cid, self.ops, now=built + 5), {})
        o = deploy.step(self.b, "G", cid, self.ops, now=built + deploy.ROLLOUT_TIMEOUT + 1)
        self.assertEqual(o["card"]["state"], "built")                                   # never "thành công" w/o proof
        self.assertIn("Trạng thái: đã build — chưa xác minh rollout", o["send"][0])
        self.assertIn("CHƯA xác minh rollout", o["send"][0])

    def test_lost_trigger_answer_is_reconciled(self):
        cid = self._approved()
        self.ops.trigger_result = {"error": True, "uncertain": True, "message": "timeout"}
        self.assertEqual(deploy.step(self.b, "G", cid, self.ops, now=1200.0), {})
        p = self.b.card_get("G", cid)["payload"]
        self.assertEqual((p["stage"], p["triggered_at"]), ("trigger", 1200.0))
        self.ops.builds_result = [{"number": 1421, "timestamp": 1201000, "url": "http://j/1421/", "users": ["bot"]},
                                  {"number": 1420, "timestamp": 900000, "users": ["bot"]}]
        o = deploy.step(self.b, "G", cid, self.ops, now=1210.0)
        self.assertIn("đối soát: build #1421", o["send"][0])
        self.assertIn("có thể nhầm", o["send"][0])
        self.assertEqual(self.ops.triggers, ["job/DEV/job/etask"])
        self.assertEqual(self.b.card_get("G", cid)["payload"]["stage"], "building")

    def test_trigger_rejected_and_no_build_after_lost_answer(self):
        cid = self._approved()
        self.ops.trigger_result = {"error": True, "message": "HTTP 403"}
        o = deploy.step(self.b, "G", cid, self.ops, now=1200.0)
        self.assertEqual(o["card"]["state"], "failed")
        self.assertIn("Jenkins từ chối kích build: HTTP 403", o["send"][0])
        self.assertIsNone(self.b.meta_get("deploylock:etask:dev"))
        cid = self._approved(now=2000.0)
        self.ops.trigger_result = {"error": True, "uncertain": True}
        deploy.step(self.b, "G", cid, self.ops, now=2200.0)
        self.ops.builds_result = []
        self.assertEqual(deploy.step(self.b, "G", cid, self.ops, now=2300.0), {})
        o = deploy.step(self.b, "G", cid, self.ops, now=2200.0 + deploy.QUEUE_TIMEOUT + 1)
        self.assertEqual(o["card"]["state"], "uncertain")                              # Jenkins may still run it
        self.assertEqual(self.b.meta_get("deploylock:etask:dev"), f"G|{cid}")           # lock KEPT
        self.assertIn("mở khoá", o["send"][0])
        self.assertIn("chỉ chủ nhóm", deploy.unlock(self.b, "G", cid, LAN)["reply"][0])
        out = deploy.unlock(self.b, "G", cid, OWNER)
        self.assertEqual(out["card"]["state"], "error")
        self.assertIsNone(self.b.meta_get("deploylock:etask:dev"))

    def test_one_deploy_per_env_at_a_time(self):
        other = deploy.request(self.b, "G2", LAN, _target(), "", self.ops, [HUY], "Chung", now=1000.0)["card"]["id"]
        deploy.approve(self.b, "G2", other, HUY, self.ops, now=1001.0, owner_id="ME")
        self.b.meta_set("deploylock:etask:dev", f"G2|{other}")                         # same card code, other group
        cid = self._approved()
        self.assertEqual(cid, other)                                                    # D-1 in both groups
        self.assertEqual(deploy.step(self.b, "G", cid, self.ops, now=1200.0), {})
        self.assertEqual(self.ops.triggers, [])
        self.b.card_update("G2", other, state="failed")                                 # holder died after finishing
        deploy.step(self.b, "G", cid, self.ops, now=1201.0)                             # stale lock taken over
        self.assertEqual(self.ops.triggers, ["job/DEV/job/etask"])
        self.assertEqual(self.b.meta_get("deploylock:etask:dev"), f"G|{cid}")

    def test_gate_again_before_trigger(self):
        cid = self._approved()
        o = deploy.step(self.b, "G", cid, self.ops, now=1100.0 + deploy.RUN_WITHIN_S + 1)
        self.assertEqual(o["card"]["state"], "waiting")                                 # approval too old
        self.assertIn("cần duyệt lại", o["send"][0])
        deploy.approve(self.b, "G", cid, HUY, self.ops, now=5000.0, owner_id="ME")
        self.ops.head = "b" * 40
        o = deploy.step(self.b, "G", cid, self.ops, now=5001.0)
        self.assertEqual((o["card"]["state"], o["card"]["payload"]["commit"]), ("waiting", "b" * 40))
        self.assertIn("lần duyệt trước không tính", o["send"][0])
        deploy.approve(self.b, "G", cid, HUY, self.ops, now=5002.0, owner_id="ME")
        o = deploy.step(self.b, "G", cid, self.ops, now=5003.0, recheck=lambda p: "người yêu cầu không còn quyền deploy:dev")
        self.assertEqual(o["card"]["state"], "cancelled")
        self.assertEqual(self.ops.triggers, [])

    def test_two_candidate_builds_is_uncertain(self):
        cid = self._approved()
        self.ops.trigger_result = {"error": True, "uncertain": True}
        deploy.step(self.b, "G", cid, self.ops, now=1200.0)
        self.ops.builds_result = [{"number": 1421, "timestamp": 1201000, "users": ["bot"]},
                                  {"number": 1422, "timestamp": 1202000, "users": ["bot"]},
                                  {"number": 1423, "timestamp": 1203000, "users": ["someone"]}]
        o = deploy.step(self.b, "G", cid, self.ops, now=1210.0)
        self.assertEqual(o["card"]["state"], "uncertain")
        self.assertIn("2 build cùng lúc", o["send"][0])

    def test_no_self_approval_on_any_channel(self):
        out = deploy.request(self.b, "G", {**OWNER, "perms": ["admin"]}, _target(), "", self.ops, [], "Chung", now=1000.0)
        cid = out["card"]["id"]
        r = deploy.approve(self.b, "G", cid, {"fchat_id": "ME", "name": "Chung"}, self.ops, now=1001.0,
                           via="telegram", owner_id="ME")
        self.assertIn("không tự duyệt", r["reply"][0])
        self.assertEqual(self.b.card_get("G", cid)["state"], "waiting")

    def test_rollout_evidence(self):
        pushes = FakeOps().build_result["pushes"]
        wl = FakeOps().wl
        self.assertEqual(deploy.rollout_state(wl, "etask", pushes)[0], "done")
        self.assertEqual(deploy.rollout_state({**wl, "replicas": None}, "etask", pushes)[0], "error")      # unknown desired
        pod = wl["pods"][0]
        for bad in ({**pod, "phase": None}, {**pod, "ready": False},
                    {**pod, "containers": pod["containers"] + [{"name": "sidecar", "ready": False}]}):
            self.assertEqual(deploy.rollout_state({**wl, "pods": [bad]}, "etask", pushes)[0], "rolling", bad)

    def test_config_hash_fail_closed_and_claimed_build(self):
        self.ops.hash = ""
        r = deploy.request(self.b, "G", LAN, _target(), "", self.ops, [HUY], "Chung", now=1000.0)
        self.assertIn("không đọc được cấu hình job", r["reply"][0])
        self.ops.hash = "h1"
        cid = self._approved()
        self.ops.hash = ""
        self.assertEqual(deploy.step(self.b, "G", cid, self.ops, now=1200.0), {})          # never trigger blind
        self.assertEqual(self.ops.triggers, [])
        self.ops.hash = "h1"
        deploy.step(self.b, "G", cid, self.ops, now=1201.0)
        self.b.meta_set("deploybuilds:job/DEV/job/etask", [1421])                        # someone else's card owns it
        o = deploy.step(self.b, "G", cid, self.ops, now=1202.0)
        self.assertEqual(o["card"]["state"], "uncertain")
        self.assertIn("thẻ deploy khác nhận", o["send"][0])

    def test_rollout_needs_every_replica_and_proof(self):
        cid = self._approved()
        self.ops.wl = {**self.ops.wl, "replicas": 2}                                    # 1 of 2 replicas up
        self._run(cid, n=4)
        self.assertEqual(self.b.card_get("G", cid)["payload"]["stage"], "rollout")
        st, detail = deploy.rollout_state({**self.ops.wl, "replicas": 1, "spec_images": {"etask": "other/img:1"}},
                                          "etask", self.ops.build_result["pushes"])
        self.assertEqual(st, "rolling")
        self.assertIn("template chưa trỏ", detail)
        self.ops.wl = {**self.ops.wl, "replicas": 1}
        self.ops.build_result = {**self.ops.build_result, "commit_sha": ""}
        self.b.card_update("G", cid, state="failed")
        self.ops.queue_result = {"number": 1422, "url": "http://j/1422/"}                # a new build for the new card
        cid2 = self._approved(now=9000.0)
        self._run(cid2, n=6, t0=9200.0)
        c = self.b.card_get("G", cid2)
        self.assertEqual(c["state"], "built")                                           # no SHA: no provenance
        self.assertIn("không in commit SHA", c["payload"]["result"])

    def test_resume_after_restart(self):
        cid = self._approved()
        deploy.step(self.b, "G", cid, self.ops, now=1200.0)
        path = self.b.db.execute("pragma database_list").fetchone()[2]
        self.b.close()
        self.b = board_store.Board(path)
        outs = self._run(cid, t0=1300.0)
        self.assertEqual(self.b.card_get("G", cid)["state"], "done")
        self.assertEqual(self.ops.triggers, ["job/DEV/job/etask"])
        self.assertTrue(outs)

    def test_queue_cancelled(self):
        cid = self._approved()
        deploy.step(self.b, "G", cid, self.ops, now=1200.0)
        self.ops.queue_result = {"cancelled": True}
        o = deploy.step(self.b, "G", cid, self.ops, now=1201.0)
        self.assertEqual(o["card"]["state"], "failed")


class BuildLogTest(unittest.TestCase):
    """jenkins.find_build_pushes — what ONE build pushed (version-agnostic), from a real-shaped dev log."""

    def test_pushes(self):
        sys.path.insert(0, str(ROOT / ".claude" / "skills" / "dev-automation" / "tools"))
        import jenkins
        log = chr(10).join(["Commit SHA: " + "f" * 40, "+ docker push 10.14.121.18:32000/idaas-etask:0.0.1274",
                          "The push refers to repository [10.14.121.18:32000/idaas-etask]",
                          "0.0.1274: digest: " + D + " size: 2841", " * [new tag]         dev.0.0.1274 -> dev.0.0.1274"])
        out = jenkins.find_build_pushes(log)
        self.assertEqual(out["pushes"], [{"image": "10.14.121.18:32000/idaas-etask", "version": "0.0.1274", "digest": D}])
        self.assertEqual((out["commit_sha"], out["tags_pushed"]), ("f" * 40, ["dev.0.0.1274"]))
        self.assertEqual(jenkins.find_build_pushes("nothing")["pushes"], [])


EXT = Path(os.environ.get("FQR_EXT_DIR") or ROOT.parent / "fpt-chat-quick-reply")


@unittest.skipUnless((EXT / "board.js").exists() and __import__("shutil").which("node"), "extension / node not found")
class GoldenDeployTest(unittest.TestCase):
    """What the bot renders for D cards is what the extension reads (state, people, link)."""

    def test_render_parse(self):
        import subprocess
        b = _board()
        self.addCleanup(lambda: b.close())
        ops = FakeOps()
        out = deploy.request(b, "G", LAN, _target(), "thử", ops, [HUY], "Chung", now=1000.0)
        cid = out["card"]["id"]
        texts = [out["send"]]
        texts.append(deploy.approve(b, "G", cid, HUY, ops, now=1100.0, owner_id="ME")["send"])
        ops.wl = {"error": True}
        for i in range(4):
            o = deploy.step(b, "G", cid, ops, now=1200.0 + i)
            if o:
                texts.append(o["send"])
        o = deploy.step(b, "G", cid, ops, now=99999.0)
        texts.append(o["send"])
        sys.path.insert(0, str(ROOT / ".claude" / "skills" / "tchat-automation" / "tools"))
        import send
        msgs = []
        for i, (t, people) in enumerate(texts):
            content, meta = send.with_mentions(t, people)
            msgs.append({"id": f"m{i}", "messageIdInc": i, "senderId": "BOT", "type": "TEXT", "content": content,
                         "createdAt": f"2026-10-31T0{i}:00:00Z", "user": {"displayName": "Chung"}, "metadata": meta})
        tmp = Path(tempfile.mkdtemp(prefix="golden4-")) / "msgs.json"
        tmp.write_text(json.dumps(msgs, ensure_ascii=False), encoding="utf-8")
        p = subprocess.run(["node", str(EXT / "tests" / "parse_cli.js"), str(tmp), "BOT"], capture_output=True,
                           text=True, encoding="utf-8", timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        [c] = [c for c in json.loads(p.stdout)["cards"] if c.get("cardId") == cid]
        self.assertEqual((c["type"], c["state"]), ("deploy", "built"))
        self.assertEqual(c["people"]["requester"], {"id": "U1", "name": "Lan"})
        self.assertEqual(c["people"]["approver"], {"id": "U2", "name": "Huy"})
        self.assertIn("http://j/1421/", c["links"])
        self.assertGreaterEqual(len(c["events"]), 4)


WATCH_4 = r'''
import json, os, sys, time
sys.argv = ["x"]
import group_watch as g
from agent_skill_toolkit.core import board_store
g._board = board_store.Board()
g._OWNER.update(id="ME", name="Chung")
sent = []
g.send.send_text = lambda gid, content, gt=None, metadata=None, card_id="", **k: (sent.append([content, card_id]),
                                                                                  {"sent": True, "confirmed": True})[1]
cfg = {"FCHAT_BOARD_GROUPS": "G"}
g.rccfg.get = lambda k, d="": cfg.get(k, d)
REG = json.loads(os.environ["REG"])
g._registry = lambda: REG
sys.path.insert(0, os.environ["TESTS_DIR"])
import test_group_board_4 as t4
ops = t4.FakeOps()
g._DeployOps = lambda: ops
TG = {}
g._tg_ask_owner = lambda card: {"req": "R1", "chat": "777"}
import approvals
approvals.get = lambda req: TG.get(req, {})
out = {}
def step(name, sender, text):
    sent.clear()
    g._process_one("G", "SUPER_PRIVATE", {"U1": "Lan", "U2": "Huy", "ME": "Chung"}.get(sender, sender), sender, text, "ME")
    out[name] = [s[0] for s in sent]
step("denied", "U2", "@bot deploy etask lên dev")
step("sandbox", "U1", "@bot deploy etask lên sandbox")
step("request", "U1", "@bot deploy etask lên dev | lý do: thử")
cid = g._board.cards_in("G", "D", ("waiting",))[0]["id"]
out["tg"] = g._board.card_get("G", cid)["payload"].get("tg")
step("owner_chat", "ME", "duyệt " + cid)
TG["R1"] = {"status": "approved", "by": "999", "decided": 1}
sent.clear(); g._deploy_tick("G", drive=lambda gid, c: None)
out["tg_wrong_person"] = [g._board.card_get("G", cid)["state"], len(sent)]
TG["R1"] = {"status": "approved", "by": "777", "decided": 2}
driven = []
sent.clear(); g._deploy_tick("G", drive=lambda gid, c: driven.append(c))
c = g._board.card_get("G", cid)
out["tg_ok"] = [c["state"], c["payload"]["approval"]["via"], str(c["payload"]["approval"]["proof"]["by"]), driven,
                [s[0] for s in sent]]
print(json.dumps(out, ensure_ascii=False))
'''


class GroupWatch4Test(unittest.TestCase):
    def test_wiring(self):
        from test_group_board import _run_tchat, _tmpdb
        db = _tmpdb()
        Path(os.path.dirname(db), "team.json").write_text(json.dumps({
            "lan": {"name": "Lan", "handles": {"fchat_id": "U1"}, "perms": ["review", "deploy:dev"], "verified_by": "Chung"},
            "huy": {"name": "Huy", "handles": {"fchat_id": "U2"}, "perms": ["review", "approve:dev"], "verified_by": "Chung"}}),
            encoding="utf-8")
        os.environ["REG"] = json.dumps(REG)
        os.environ["TESTS_DIR"] = str(Path(__file__).resolve().parent)
        out = _run_tchat(WATCH_4, db)
        self.assertIn("deploy:dev", out["denied"][0])
        self.assertIn("chỉ cho dev", out["sandbox"][0])
        self.assertIn("Trạng thái: chờ duyệt · Yêu cầu: @Lan", out["request"][0])
        self.assertIn("(@Huy)", out["request"][0])
        self.assertEqual(out["tg"], {"req": "R1", "chat": "777"})
        self.assertIn("duyệt qua Telegram", out["owner_chat"][0])
        self.assertEqual(out["tg_wrong_person"], ["waiting", 0])
        state, via, by, driven, texts = out["tg_ok"]
        self.assertEqual((state, via, by), ("running", "telegram", "777"))
        self.assertEqual(len(driven), 1)
        self.assertTrue(any("Duyệt: @Chung" in t for t in texts))


if __name__ == "__main__":
    unittest.main()
