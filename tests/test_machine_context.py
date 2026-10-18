#!/usr/bin/env python3
"""Stdlib unittest for machine_context.py (machine-specific context check). Uses temp dirs
for repo root, work/ and HOME — never the real machine. Run with:
    python -m unittest discover -s tests
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_TOOLS = os.path.join(_HERE, "..", ".claude", "skills", "dev-automation", "tools")
sys.path.insert(0, os.path.abspath(_TOOLS))

import machine_context as mc  # noqa: E402


class MachineContextTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "repo")
        self.home = os.path.join(self.tmp, "home")
        self.work = os.path.join(self.root, "work")
        os.makedirs(self.work)
        os.makedirs(self.home)
        env = {"HOME": self.home, "USERPROFILE": self.home, "WORK_DIR": self.work,
               "APPDATA": os.path.join(self.home, "appdata"), "XDG_CONFIG_HOME": os.path.join(self.home, "cfg"),
               "DBEAVER_WORKSPACE": os.path.join(self.home, "dbeaver")}
        clean = {k: v for k, v in os.environ.items()
                 if k not in {"KUBECONFIG", "GH_CONFIG_DIR", "TOOLKIT_ENV_FILE"}
                 and not any(k in keys for keys in mc.ENV_GROUPS.values()) and k != "FCHAT_PRIVATE_KEY_PATH"}
        clean.update(env)
        self.env = mock.patch.dict(os.environ, clean, clear=True)
        self.env.start()
        self.cfg = mock.patch.object(mc, "config", None)  # never load the real .env
        self.cfg.start()

    def tearDown(self):
        self.cfg.stop()
        self.env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, rel, data):
        path = os.path.join(self.root, rel) if not os.path.isabs(rel) else rel
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps(data) if not isinstance(data, str) else data)
        return path

    def test_empty_machine_reports_missing_without_crashing(self):
        r = mc.collect(self.root)
        self.assertFalse(r["env"]["file_exists"])
        self.assertEqual(r["projects"]["error"], "missing")
        self.assertEqual(r["agents"]["error"], "missing")
        self.assertFalse(r["ssh"]["ok"])
        self.assertEqual(r["features"]["gitlab_api"]["state"], "missing")
        self.assertIn("Bối cảnh máy", mc.human(r))

    def test_env_groups_presence_only(self):
        os.environ.update({"GITLAB_URL": "https://g", "GITLAB_PRIVATE_TOKEN": "secret-xyz", "ATASK_BASE_URL": "u"})
        r = mc.collect(self.root)
        self.assertTrue(r["env"]["groups"]["gitlab"]["ok"])
        self.assertEqual(r["env"]["groups"]["atask"]["missing"], ["ATASK_PAT_TOKEN"])
        self.assertNotIn("secret-xyz", json.dumps(r))  # values never reported

    def test_projects_clone_dirs(self):
        cloned = os.path.join(self.tmp, "a")
        os.makedirs(os.path.join(cloned, ".git"))
        self.write("work/projects.json", {"a": {"clone_dir": cloned},
                                          "b": {"clone_dir": os.path.join(self.tmp, "nope")},
                                          "c": {"stack": {}}})
        p = mc.check_projects(self.root)
        self.assertEqual(p["count"], 3)
        self.assertEqual(p["not_cloned"], ["b"])
        self.assertFalse(p["ok"])

    def test_agents_account_dirs(self):
        good = os.path.join(self.tmp, "acc1")
        os.makedirs(good)
        self.write("work/agents.json", {"accounts": {
            "claude": {"x": {"env": {"CLAUDE_CONFIG_DIR": good}},
                       "y": {"env": {"CLAUDE_CONFIG_DIR": os.path.join(self.tmp, "gone")}}},
            "codex": {"main": {"env": {"CODEX_HOME": "~/.codex"}}}}})
        a = mc.check_agents(self.root)
        self.assertEqual(a["count"], 3)
        self.assertEqual(sorted(a["unusable"]), ["claude@y", "codex@main"])
        os.makedirs(os.path.join(self.home, ".codex"))
        self.assertEqual(mc.check_agents(self.root)["unusable"], ["claude@y"])

    def test_ssh_ignores_public_keys(self):
        self.write(os.path.join(self.home, ".ssh", "id_ed25519.pub"), "pub")
        self.assertFalse(mc.check_ssh()["ok"])
        self.write(os.path.join(self.home, ".ssh", "id_ed25519"), "priv")
        self.assertEqual(mc.check_ssh()["private_keys"], ["id_ed25519"])

    def test_kube_default_and_registry_refs(self):
        self.assertFalse(mc.check_kube(self.root)["ok"])
        self.write(os.path.join(self.home, ".kube", "config"), "apiVersion: v1")
        self.assertTrue(mc.check_kube(self.root)["ok"])
        self.write("work/projects.json", {"p": {"environments": {"dev": {"kubeconfig": "~/.kube/other"}}}})
        k = mc.check_kube(self.root)
        self.assertEqual(k["registry_missing"], ["~/.kube/other"])
        self.assertFalse(k["ok"])

    def test_feature_states(self):
        self.assertEqual(mc._feature({"a": True, "b": True}, "h")["state"], "ready")
        self.assertEqual(mc._feature({"a": True, "b": False}, "h")["state"], "partial")
        f = mc._feature({"a": False}, "h")
        self.assertEqual((f["state"], f["hint"]), ("missing", "h"))


if __name__ == "__main__":
    unittest.main()
