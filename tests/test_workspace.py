"""Workspace: project inferred from the cwd (clone or worktree), registry discovery draft, plugin hook dedupe."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / ".claude" / "skills" / "dev-automation" / "tools"
sys.path.insert(0, str(TOOLS))
import project_config as pc  # noqa: E402
import workspace as ws  # noqa: E402

GIT = ["git", "-c", "user.email=t@t", "-c", "user.name=t"]


def _repo(path: str, remote: str = "") -> str:
    os.makedirs(path, exist_ok=True)
    subprocess.run(["git", "init", "-q", path], check=True)
    subprocess.run(GIT + ["-C", path, "commit", "-q", "--allow-empty", "-m", "init"], check=True)
    if remote:
        subprocess.run(["git", "-C", path, "remote", "add", "origin", remote], check=True)
    return path


class Base(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="ws-work-")
        self.env = mock.patch.dict(os.environ, {"WORK_DIR": self.work})
        self.env.start()
        self.addCleanup(self.env.stop)

    def registry(self, data: dict):
        with open(os.path.join(self.work, "projects.json"), "w", encoding="utf-8") as f:
            json.dump(data, f)


class ProjectFromCwdTest(Base):
    def test_clone_subdir_worktree_and_unrelated(self):
        main = _repo(os.path.join(self.work, "etask"))
        other = _repo(os.path.join(self.work, "gitlab", "idaas", "eform-be"))
        wt = os.path.join(self.work, "etask-feature")
        subprocess.run(["git", "-C", main, "worktree", "add", "-q", wt], check=True)
        os.makedirs(os.path.join(main, "src", "x"))
        self.registry({"etask": {"clone_dir": main}, "nodir": {"clone_dir": os.path.join(self.work, "gone")},
                       "_meta": "not an entry"})
        self.assertEqual(pc.project_from_cwd(main), "etask")
        self.assertEqual(pc.project_from_cwd(os.path.join(main, "src", "x")), "etask")
        self.assertEqual(pc.project_from_cwd(wt), "etask")                   # worktree of the clone
        self.assertIsNone(pc.project_from_cwd(other))                         # not registered
        self.assertIsNone(pc.project_from_cwd(self.work))

    def test_work_prefixed_and_nested_deepest_wins_tie_is_none(self):
        outer = _repo(os.path.join(self.work, "mono"))
        inner = os.path.join(outer, "svc")
        os.makedirs(inner)
        self.registry({"mono": {"clone_dir": "work:mono"}, "svc": {"clone_dir": inner}})
        self.assertEqual(pc.project_from_cwd(inner), "svc")
        self.assertEqual(pc.project_from_cwd(outer), "mono")
        self.registry({"a": {"clone_dir": outer}, "b": {"clone_dir": outer}})
        self.assertIsNone(pc.project_from_cwd(outer))                         # ambiguous

    def test_foreign_repo_nested_or_foreign_worktree_never_inherits(self):
        """Codex R1 #1: path containment alone would point k8s/DB tools at the outer project's env."""
        outer = _repo(os.path.join(self.work, "outer"))
        foreign = _repo(os.path.join(outer, "vendor", "foreign"))
        os.makedirs(os.path.join(foreign, "src"))
        other = _repo(os.path.join(self.work, "other"))
        other_wt = os.path.join(outer, "wt-of-other")                   # a worktree of ANOTHER repo inside outer
        subprocess.run(["git", "-C", other, "worktree", "add", "-q", other_wt], check=True)
        plain = os.path.join(self.work, "not-a-repo")
        os.makedirs(plain)
        self.registry({"outer": {"clone_dir": outer}, "plain": {"clone_dir": plain}})
        self.assertIsNone(pc.project_from_cwd(os.path.join(foreign, "src")))
        self.assertIsNone(pc.project_from_cwd(other_wt))
        self.assertIsNone(pc.project_from_cwd(plain))                     # no git identity -> nothing
        self.assertEqual(pc.project_from_cwd(outer), "outer")

    def test_default_project_keeps_explicit_and_fills_from_cwd(self):
        main = _repo(os.path.join(self.work, "etask"))
        self.registry({"etask": {"clone_dir": main}})
        args = type("A", (), {"project": "kpi"})()
        self.assertEqual(pc.default_project(args), "kpi")
        args = type("A", (), {"project": None})()
        with mock.patch("os.getcwd", return_value=main):
            self.assertEqual(pc.default_project(args), "etask")
        self.assertEqual(args.project, "etask")


class DiscoverTest(Base):
    def test_draft_skips_known_flags_configs_and_unique_keys(self):
        known = _repo(os.path.join(self.work, "etask"), "https://gitlab.x/idaas/etask.git")
        for group in ("econtract", "ceca"):
            _repo(os.path.join(self.work, "gitlab", group, "gateway"), f"https://gitlab.x/{group}/gateway.git")
        _repo(os.path.join(self.work, "gitlab", "econtract", "econtract-gateway"),
              "git@gitlab.x:econtract/econtract-gateway.git")
        _repo(os.path.join(self.work, "gitlab", "econtract", "config_uat"), "https://gitlab.x/econtract/config_uat.git")
        be = _repo(os.path.join(self.work, "gitlab", "idaas", "eform-be"), "https://gitlab.x/idaas/eform-be.git")
        Path(be, "pom.xml").write_text("<project/>")
        Path(be, ".yo-rc.json").write_text("{}")
        self.registry({"etask": {"clone_dir": known, "repo_path": "idaas/etask"}})
        d = ws.discover(self.work, use_api=False)
        paths = {k: v["repo_path"] for k, v in d["projects"].items()}
        self.assertNotIn("idaas/etask", paths.values())                       # already registered
        self.assertEqual(len(paths), 4)                                       # no entry lost to a key clash
        self.assertEqual(len(set(paths)), 4)
        self.assertNotIn("gateway", paths)                                    # a shared name is never bare
        self.assertEqual(paths["eform-be"], "idaas/eform-be")
        self.assertEqual(d["projects"]["eform-be"]["stack"],
                         {"lang": "java", "build": "maven", "framework": "jhipster-spring-boot"})
        self.assertEqual([c["repo_path"] for c in d["config_repos"]], ["econtract/config_uat"])

    def test_other_host_is_not_looked_up_nor_deduplicated_by_path(self):
        """Codex R1 #3: the same group/repo path on another host is a different repository."""
        known = _repo(os.path.join(self.work, "etask"), "https://gitlab.x/idaas/etask.git")
        _repo(os.path.join(self.work, "gitlab", "mirror", "etask"), "https://github.com/idaas/etask.git")
        self.registry({"etask": {"clone_dir": known, "repo_path": "idaas/etask"}})
        calls = []
        with mock.patch.object(ws, "gitlab_host", return_value="gitlab.x"), \
                mock.patch.object(ws._gitlab, "project_info", side_effect=lambda p: calls.append(p) or {"id": 1}):
            d = ws.discover(self.work, use_api=True)
        self.assertEqual(calls, [])                                       # never asked OUR GitLab about it
        (key, entry), = d["projects"].items()
        self.assertEqual(entry["remote_host"], "github.com")
        self.assertNotIn("gitlab_project_id", entry)
        self.assertTrue(any("host khác" in p["problem"] for p in d["problems"]))
        # R2 #1: a registered FOREIGN entry with the same path must not hide our host's clone
        mirror = os.path.join(self.work, "gitlab", "mirror", "etask")
        ours = _repo(os.path.join(self.work, "gitlab", "idaas", "etask2"), "https://gitlab.x/group/service.git")
        self.registry({"svc-mirror": {"clone_dir": mirror, "repo_path": "group/service", "remote_host": "github.com"}})
        with mock.patch.object(ws, "gitlab_host", return_value="gitlab.x"), \
                mock.patch.object(ws._gitlab, "project_info", return_value={"id": 7, "default_branch": "main"}):
            d = ws.discover(self.work, use_api=True)
        got = {v["clone_dir"]: v for v in d["projects"].values()}
        self.assertEqual(got[ours.replace("\\", "/")]["gitlab_project_id"], 7)

    def test_concurrent_applies_keep_every_key(self):
        """Codex R1 #2: read-merge-replace is one locked step; backups never collide."""
        import threading
        self.registry({})
        drafts = []
        for i in range(6):
            p = os.path.join(self.work, f"d{i}.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"projects": {f"k{i}": {"clone_dir": str(i)}}}, f)
            drafts.append(p)
        outs = []
        ts = [threading.Thread(target=lambda p=p: outs.append(ws.apply(p))) for p in drafts]
        [t.start() for t in ts]
        [t.join() for t in ts]
        with open(os.path.join(self.work, "projects.json"), encoding="utf-8") as f:
            self.assertEqual(sorted(json.load(f)), [f"k{i}" for i in range(6)])
        self.assertEqual(len({o["backup"] for o in outs}), 6)

    def test_apply_adds_only_new_keys_with_backup(self):
        self.registry({"etask": {"clone_dir": "x"}})
        draft = os.path.join(self.work, "draft.json")
        with open(draft, "w", encoding="utf-8") as f:
            json.dump({"projects": {"etask": {"clone_dir": "other"}, "kpi": {"clone_dir": "k"},
                                    "eform-be": {"clone_dir": "e"}}}, f)
        out = ws.apply(draft, only=["etask", "kpi"])
        self.assertEqual(out["added"], ["kpi"])
        self.assertEqual(out["skipped_existing"], ["etask"])
        self.assertTrue(os.path.isfile(out["backup"]))
        with open(os.path.join(self.work, "projects.json"), encoding="utf-8") as f:
            reg = json.load(f)
        self.assertEqual(reg["etask"], {"clone_dir": "x"})                    # never overwritten
        self.assertNotIn("eform-be", reg)                                     # not selected


class PluginHookTest(unittest.TestCase):
    HOOK = ROOT / ".claude" / "hooks" / "guard_write.py"
    EVENT = json.dumps({"tool_name": "Bash", "tool_input": {"command": "cat " + "." + "env"}})

    def run_hook(self, project_dir: str, *args) -> str:
        env = {k: v for k, v in os.environ.items() if k not in ("TOOLKIT_GUARD_ASK", "TOOLKIT_HEADLESS",
                                                               "CLAUDE_TG_BRIDGE")}
        env["CLAUDE_PROJECT_DIR"] = project_dir
        return subprocess.run([sys.executable, str(self.HOOK), *args], input=self.EVENT, capture_output=True,
                              text=True, env=env, timeout=30).stdout

    def test_plugin_hook_defers_to_the_toolkit_project_hook_only_inside_the_toolkit(self):
        self.assertEqual(self.run_hook(str(ROOT), "--plugin"), "")                     # project hook guards it
        self.assertIn('"ask"', self.run_hook(str(ROOT / "work" / "etask"), "--plugin"))  # a product repo
        self.assertIn('"ask"', self.run_hook(str(ROOT)))                                # the project hook itself

    def test_plugin_manifest_declares_skills_commands_hooks(self):
        m = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
        for k in ("skills", "commands", "hooks"):
            self.assertTrue((ROOT / m[k]).exists(), k)
        hooks = json.loads((ROOT / m["hooks"]).read_text(encoding="utf-8"))
        proj = json.loads((ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(hooks["hooks"]["PreToolUse"][0]["matcher"], proj["hooks"]["PreToolUse"][0]["matcher"])
        self.assertIn("--plugin", hooks["hooks"]["PreToolUse"][0]["hooks"][0]["command"])
        self.assertIs(proj["enabledPlugins"][m["name"] + "@inline"], False)       # no double load in the toolkit


if __name__ == "__main__":
    unittest.main()
