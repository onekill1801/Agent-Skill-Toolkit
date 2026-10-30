"""Group workboard phase 2a (docs/design/group-workboard.md §5): env map, read side — k8s workload snapshot,
verification levels (never ✅ without build provenance), "MR in env X?" in 4 levels, the `@bot env` /
`@bot MR … ở đâu` commands (repo allowlist, prod off by default)."""

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".claude" / "skills" / "_runtime"))
sys.path.insert(0, str(ROOT / ".claude" / "skills" / "dev-automation" / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from agent_skill_toolkit.workflows.group_board import commands, envmap  # noqa: E402

IMG = "registry/x/prod-kyta-etask:0.198.45"
PROJECT = {"protected_envs": ["prod"], "environments": {
    "local": {},
    "dev": {"jenkins": {"branch": "merge/rfc", "scanned_at": "2026-10-23 01:50"}, "k8s": {"deploy": "etask"},
            "envmap": {"git_tag_prefix": "dev."}},
    "sandbox": {"jenkins": {"resource": "deployment/kyta-etask", "container": "kyta-etask",
                            "git_tag_prefix": "prod-econ.", "branch": "merge/rfc-into-vitess"}},
    "prod": {"jenkins": {"branch": "merge/rfc"}, "k8s": {"deploy": "kyta-etask-02"}},
    "demo-fci": {"jenkins": {"branch": "develop"}},
}}


HEX_A = "a" * 64


def pod(name, image=IMG, digest=HEX_A, ready=True, owner="ReplicaSet/rs1", created="2026-10-29T18:16:42Z"):
    return {"name": name, "owner": owner, "owner_created": created,
            "containers": [{"name": "kyta-etask", "image": image, "image_id": f"x@sha256:{digest}", "ready": ready},
                           {"name": "sidecar", "image": "envoy:1", "image_id": "e@sha256:zzz", "ready": True}]}


def workload(*pods, spec=IMG):
    return {"kind": "Deployment", "spec_images": {"kyta-etask": spec, "sidecar": "envoy:1"}, "pods": list(pods)}


TAGS = {"prod-econ.0.198.45": {"commit_id": "c" * 40, "created_at": "2026-10-28T19:18:24Z"}}
tag = lambda n: TAGS.get(n) or {"error": True, "status": 404}  # noqa: E731


class TargetsTest(unittest.TestCase):
    def test_env_targets(self):
        t = {x["env"]: x for x in envmap.env_targets(PROJECT)}
        self.assertEqual(sorted(t), ["demo-fci", "dev", "sandbox"])                  # prod off, local skipped
        self.assertEqual((t["dev"]["target"], t["dev"]["tag_prefix"]), ("deployment/etask", "dev."))
        self.assertEqual((t["sandbox"]["target"], t["sandbox"]["container"]), ("deployment/kyta-etask", "kyta-etask"))
        self.assertEqual(t["demo-fci"]["target"], "")
        self.assertIn("prod", [x["env"] for x in envmap.env_targets(PROJECT, show_prod=True)])


class SnapshotTest(unittest.TestCase):
    def sbx(self):
        return {x["env"]: x for x in envmap.env_targets(PROJECT)}["sandbox"]

    def test_inferred_never_verified(self):
        s = envmap.snapshot(self.sbx(), workload(pod("a"), pod("b")), tag)
        self.assertEqual((s["level"], s["version"], s["commit"], s["ready"]), ("inferred", "0.198.45", "c" * 40, "2/2"))
        self.assertEqual(s["rs_created_at"], "2026-10-29T18:16:42Z")                  # the ReplicaSet, not the build
        self.assertEqual(s["commit_at"], "2026-10-28T19:18:24Z")                      # a commit time, not a build time
        self.assertNotIn("built_at", s)
        self.assertNotEqual(s["level"], "verified")

    def test_drift_and_unknown(self):
        s = envmap.snapshot(self.sbx(), workload(pod("a"), pod("b", digest="b" * 64)), tag)
        self.assertEqual(s["level"], "drift")                                          # two digests
        s = envmap.snapshot(self.sbx(), workload(pod("a", image="registry/x/prod-kyta-etask:0.198.44")), tag)
        self.assertEqual(s["level"], "drift")                                          # pod != spec
        s = envmap.snapshot(self.sbx(), workload(pod("a", ready=False)), tag)
        self.assertEqual(s["level"], "drift")
        s = envmap.snapshot(self.sbx(), workload(pod("a"), spec="registry/x/prod-kyta-etask:0.199.45"), tag)
        self.assertEqual(s["level"], "drift")                                          # rollout in progress
        s = envmap.snapshot(self.sbx(), {"error": True, "kind": "timeout"}, tag)
        self.assertEqual((s["level"], s["notes"]), ("unknown", ["không đọc được k8s (timeout)"]))
        s = envmap.snapshot(self.sbx(), workload(pod("a", image="registry/x/prod-kyta-etask:0.1.1"),
                                                 spec="registry/x/prod-kyta-etask:0.1.1"), tag)
        self.assertEqual((s["level"], s["commit"]), ("unknown", ""))                   # no git tag for it
        self.assertIn("không có git tag prod-econ.0.1.1", s["notes"])
        # Codex 2a R1 #2: missing evidence is never "consistent"
        no_digest = pod("a")
        no_digest["containers"][0]["image_id"] = ""
        no_status = {"name": "b", "owner": "ReplicaSet/rs1", "containers": []}
        s = envmap.snapshot(self.sbx(), workload(no_digest, no_status), tag)
        self.assertEqual((s["level"], s["ready"]), ("drift", "1/2"))
        for bad in ("xyz", "abc", "g" * 64, "A" * 64, "a" * 63):                       # Codex 2a R2: not a sha256
            s = envmap.snapshot(self.sbx(), workload(pod("a", digest=bad)), tag)
            self.assertEqual(s["level"], "drift", bad)
        long_a, long_b = "1" * 12 + "a" * 52, "1" * 12 + "b" * 52                     # same first 12 chars
        s = envmap.snapshot(self.sbx(), workload(pod("a", digest=long_a), pod("b", digest=long_b)), tag)
        self.assertEqual(s["level"], "drift")
        fci = {x["env"]: x for x in envmap.env_targets(PROJECT)}["demo-fci"]
        self.assertEqual(envmap.snapshot(fci, {}, tag)["notes"], ["chưa khai workload k8s cho env này"])


class MrWhereTest(unittest.TestCase):
    SNAPS = [{"env": "dev", "commit": "d" * 40, "branch": "merge/rfc", "level": "inferred"},
             {"env": "sandbox", "commit": "s" * 40, "branch": "merge/rfc-into-vitess"},
             {"env": "demo-fci", "commit": "", "notes": ["chưa khai workload k8s cho env này"]},
             {"env": "x", "commit": "e" * 40, "branch": "b"}]
    MR = {"iid": 7, "state": "merged", "title": "Fix A", "merge_commit_sha": "m" * 40, "target_branch": "merge/rfc"}

    def test_levels(self):
        anc = {("m" * 40, "d" * 40): True, ("m" * 40, "s" * 40): False, ("p" * 40, "s" * 40): True,
               ("m" * 40, "e" * 40): False}

        def merged(branch, title):
            return [{"iid": 8, "title": "Fix A", "merge_commit_sha": "p" * 40, "web_url": "u8"}] \
                if branch == "merge/rfc-into-vitess" else [{"iid": 9, "title": "Fix A (other)", "sha": "q" * 40}]
        rows = {r["env"]: r for r in envmap.mr_where(self.MR, self.SNAPS, lambda a, b: anc.get((a, b)), merged)}
        self.assertEqual(rows["dev"]["verdict"], "có")
        self.assertEqual((rows["sandbox"]["verdict"], rows["sandbox"]["via"]["iid"]), ("nghi port", 8))
        self.assertEqual(rows["demo-fci"]["verdict"], "không xác định")
        self.assertEqual(rows["x"]["verdict"], "không thấy commit gốc")                # never "chưa có"
        drift = [{"env": "dev", "commit": "d" * 40, "level": "drift", "notes": ["pod lệch"]}]
        rows = envmap.mr_where(self.MR, drift, lambda a, b: True, lambda b, t: [])
        self.assertEqual(rows[0]["verdict"], "không xác định")                         # Codex 2a R1 #1
        rows = envmap.mr_where({**self.MR, "state": "opened"}, self.SNAPS, None, None)
        self.assertEqual({r["verdict"] for r in rows}, {"chưa merge"})
        rows = envmap.mr_where(self.MR, self.SNAPS[:1], lambda a, b: None, lambda b, t: [])
        self.assertEqual(rows[0]["verdict"], "không xác định")                         # GitLab did not answer

    def test_mr_commit_by_merge_method(self):
        self.assertEqual(envmap.mr_commit({"squash_commit_sha": "s", "sha": "h"}), "s")
        self.assertEqual(envmap.mr_commit({"sha": "h"}), "h")                          # fast-forward

    def test_texts(self):
        snaps = [envmap.snapshot({x["env"]: x for x in envmap.env_targets(PROJECT)}["sandbox"], workload(pod("a")), tag)]
        t = envmap.env_text("etask", "https://g/idaas/etask", snaps)
        self.assertTrue(t.startswith("🌐 etask — đang chạy ở từng môi trường (quan sát "))
        self.assertIn("ReplicaSet tạo ", t)
        self.assertIn("Sandbox: 🟡 suy ra · 0.198.45 · commit cccccccc", t)
        self.assertIn("🔗 https://g/idaas/etask/-/tags/prod-econ.0.198.45", t)
        t = envmap.mr_text("etask", "https://g/idaas/etask", {**self.MR, "web_url": "u7"},
                           [{"env": "sandbox", "verdict": "có", "detail": "d", "via": None},
                            {"env": "x", "verdict": "không xác định", "detail": "d", "via": None}], snaps)
        self.assertIn("Sandbox: có — d · mức env: 🟡 suy ra", t)                         # every row has its level
        self.assertIn("X: không xác định — d · mức env: ❔ chưa xác minh", t)
        self.assertIn(f"🔗 https://g/idaas/etask/-/compare/{'c' * 40}...{'m' * 40}", t)     # env...MR (§5.2)
        self.assertIn("(quan sát ", t.split("\n")[0])


class K8sWorkloadTest(unittest.TestCase):
    def test_workload_snapshot(self):
        import k8s
        obj = {"kind": "DeploymentConfig", "metadata": {"name": "idaas-etask"}, "spec": {"replicas": 1, "template": {
            "spec": {"containers": [{"name": "idaas-etask", "image": "r/idaas-etask:0.0.367"}]}}}}
        pods = [{"metadata": {"name": "p1", "ownerReferences": [{"kind": "ReplicationController", "name": "idaas-etask-388"}]},
                 "status": {"phase": "Running", "containerStatuses": [
                     {"name": "idaas-etask", "image": "r/idaas-etask:0.0.367", "imageID": "r@sha256:abc", "ready": True}]}}]
        out = k8s.workload_snapshot("dc/idaas-etask", obj, pods, {"ReplicationController/idaas-etask-388": "2026-10-29T04:31:30Z"})
        self.assertEqual(out["spec_images"], {"idaas-etask": "r/idaas-etask:0.0.367"})
        self.assertEqual(out["pods"][0]["owner_created"], "2026-10-29T04:31:30Z")
        self.assertEqual(out["pods"][0]["containers"][0]["image_id"], "r@sha256:abc")

    def test_workload_refuses_odd_targets(self):
        p = subprocess.run([sys.executable, str(ROOT / ".claude" / "skills" / "dev-automation" / "tools" / "k8s.py"),
                            "workload", "--context", "c", "--", "-l"], capture_output=True, text=True, timeout=60)
        self.assertNotEqual(p.returncode, 0)


class CommandsTest(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(commands.parse("@bot env etask"), ("env", ("etask",)))
        self.assertEqual(commands.parse("@bot MR 1388 etask ở đâu?"), ("mr_where", ("1388", "etask")))
        self.assertEqual(commands.parse("@bot mr !12 etask"), ("mr_where", ("12", "etask")))
        self.assertIsNone(commands.parse("env etask"))                                  # members need @bot
        self.assertIsNone(commands.parse("@bot env etask; rm -rf"))


WATCH_2A = r'''
import json, os, sys
sys.argv = ["x"]
import group_watch as g
sent, k8s_calls = [], []
g.send.send_text = lambda gid, content, gt=None, metadata=None, **k: (sent.append(content), {"sent": True})[1]
cfg = {"FCHAT_ENV_REPOS": "etask", "GITLAB_URL": "https://g"}
g.rccfg.get = lambda k, d="": cfg.get(k, d)
reg = {"etask": {"repo_path": "idaas/etask", "gitlab_project_id": 5401, "protected_envs": ["prod"], "environments": {
    "sandbox": {"jenkins": {"resource": "deployment/kyta-etask", "git_tag_prefix": "prod-econ.", "branch": "b"}},
    "demo": {"k8s": {"deploy": "idaas-etask"}, "envmap": {"git_tag_prefix": "demo."}},
    "prod": {"k8s": {"deploy": "kyta-etask-02"}}}}}
g._registry = lambda: reg
WL = {"spec_images": {"c": "r/x:0.198.45"}, "pods": [{"owner_created": "2026-10-29T18:16:42Z",
      "containers": [{"name": "c", "image": "r/x:0.198.45", "image_id": "r@sha256:" + "a" * 64 + "", "ready": True}]}]}
class P:
    def __init__(self, out): self.stdout = json.dumps(out)
def run(argv, **k):
    k8s_calls.append(argv[-1])
    if argv[-1] == "deployment/idaas-etask":
        return P({"error": True, "kind": "not_found"})
    return P(WL if argv[-1] != "dc/idaas-etask" else {**WL, "spec_images": {"c": "r/x:0.0.367"},
             "pods": [{**WL["pods"][0], "containers": [{**WL["pods"][0]["containers"][0], "image": "r/x:0.0.367"}]}]})
g.subprocess.run = run
class GL:
    def __init__(self, pid): pass
    def tag(self, n): return {"commit_id": "c" * 40} if n in ("prod-econ.0.198.45", "demo.0.0.367") else {"error": True}
    def get_merge_request(self, iid): return {"iid": iid, "state": "opened", "title": "T", "web_url": "u"}
    def is_ancestor(self, a, b): return True
    def merged_mrs(self, b, t): return []
g.gitlab_api.Client = GL
out = {}
g._env_command("G", "S", "Lan", "U1", "env", ("etask",))
out["env"] = sent[-1]
out["k8s"] = list(k8s_calls)
n = len(k8s_calls)
g._env_command("G", "S", "Lan", "U1", "mr_where", ("12", "etask"))
out["mr"] = sent[-1]
out["cached"] = len(k8s_calls) == n
g._env_command("G", "S", "Lan", "U1", "env", ("econtract",))
out["not_enabled"] = sent[-1]
print(json.dumps(out, ensure_ascii=False))
'''


class GroupWatchEnvTest(unittest.TestCase):
    def test_env_commands(self):
        from test_group_board import _run_tchat, _tmpdb
        out = _run_tchat(WATCH_2A, _tmpdb())
        self.assertIn("Sandbox: 🟡 suy ra · 0.198.45", out["env"])
        self.assertIn("Demo: 🟡 suy ra · 0.0.367", out["env"])                         # dc/ fallback
        self.assertNotIn("Prod", out["env"])                                           # prod off by default
        self.assertEqual(sorted(out["k8s"]), ["dc/idaas-etask", "deployment/idaas-etask", "deployment/kyta-etask"])
        self.assertIn("chưa merge", out["mr"])
        self.assertTrue(out["cached"])                                                 # 5-min cache
        self.assertIn("chưa bật bản đồ môi trường cho 'econtract'", out["not_enabled"])


if __name__ == "__main__":
    unittest.main()
