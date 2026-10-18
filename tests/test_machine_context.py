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
                 if k not in {"KUBECONFIG", "GH_CONFIG_DIR", "TOOLKIT_ENV_FILE", "CODEX_HOME", "KUBECTL_BIN", "CLAUDE_CONFIG_DIR"}
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

    def test_agents_need_cli_dir_and_credentials(self):
        good = os.path.join(self.tmp, "acc1")
        os.makedirs(good)
        self.write(os.path.join(good, ".credentials.json"), "x")
        self.write("work/agents.json", {"accounts": {
            "claude": {"x": {"env": {"CLAUDE_CONFIG_DIR": good}},
                       "y": {"env": {"CLAUDE_CONFIG_DIR": os.path.join(self.tmp, "gone")}},
                       "z": {"env": {}}},                      # falls back to ~/.claude (no creds)
            "codex": {"main": {"env": {"CODEX_HOME": "~/.codex"}}}}})
        which = lambda c: f"/bin/{c}" if c in ("claude", "codex") else None
        with mock.patch.object(mc.shutil, "which", which):
            a = mc.check_agents(self.root)
            self.assertEqual(a["count"], 4)
            self.assertEqual(sorted(a["unusable"]), ["claude@y", "claude@z", "codex@main"])
            os.makedirs(os.path.join(self.home, ".codex"))          # dir alone is not a login
            self.assertIn("codex@main", mc.check_agents(self.root)["unusable"])
            self.write(os.path.join(self.home, ".codex", "auth.json"), "t")
            self.assertNotIn("codex@main", mc.check_agents(self.root)["unusable"])
        with mock.patch.object(mc.shutil, "which", lambda c: "/bin/claude" if c == "claude" else None):
            self.assertIn("codex@main", mc.check_agents(self.root)["unusable"])   # codex CLI missing

    def test_codex_feature_needs_login(self):
        with mock.patch.object(mc.shutil, "which", lambda c: f"/bin/{c}"):
            self.assertEqual(mc.collect(self.root)["features"]["codex_review"]["state"], "partial")
            self.write(os.path.join(self.home, ".codex", "auth.json"), "t")
            self.assertEqual(mc.collect(self.root)["features"]["codex_review"]["state"], "ready")

    def test_wrong_schema_json_does_not_crash(self):
        for bad in ([1], "str", {"accounts": [1]}, {"accounts": {"claude": {"x": "y"}}},
                    {"accounts": {"claude": {"x": {"env": ["a"]}}}}):
            self.write("work/agents.json", bad)
            self.write("work/projects.json", bad)
            r = mc.collect(self.root)
            mc.human(r)
        self.write("work/projects.json", {"p": {"clone_dir": 5}})
        self.assertEqual(mc.check_projects(self.root)["not_cloned"], ["p"])
        self.write("work/projects.json", [1])
        self.assertTrue(mc.check_projects(self.root)["error"].startswith("invalid"))

    def test_doctor_survives_context_failure(self):
        import doctor
        with mock.patch.object(doctor.machine_context, "collect", side_effect=RuntimeError("boom")):
            report = doctor.collect()
        self.assertIn("boom", report["context"]["error"])
        self.assertIn("lỗi khi kiểm tra", doctor._human(report))

    def test_empty_files_are_not_evidence(self):
        self.write(os.path.join(self.home, ".ssh", "id_rsa"), "")
        self.assertFalse(mc.check_ssh()["ok"])
        self.write(os.path.join(self.home, ".kube", "config"), "")
        self.assertFalse(mc.check_kube(self.root)["usable"])
        os.makedirs(os.path.join(self.home, ".kube", "dir"))
        os.environ["KUBECONFIG"] = os.path.join(self.home, ".kube", "dir")
        self.assertFalse(mc.check_kube(self.root)["usable"])
        self.write(os.path.join(self.home, "dbeaver", "General", ".dbeaver", "data-sources.json"), "")
        self.assertFalse(mc.check_dbeaver()["ok"])
        os.environ["FCHAT_PRIVATE_KEY_PATH"] = self.write(os.path.join(self.home, "k.pem"), "")
        self.assertFalse(mc.check_files()["FCHAT_PRIVATE_KEY_PATH"]["ok"])

    def test_lens_dir_entries_not_counted(self):
        lens = os.path.join(self.home, "lens")
        os.makedirs(os.path.join(lens, "kubeconfigs", "subdir"))
        fake = mock.Mock(lens_dirs=lambda: [lens], expand_kubeconfig=mc._expand, lens_kubectls=lambda: {})
        with mock.patch.object(mc, "_k8s", lambda: fake):
            self.assertEqual(mc.check_kube(self.root)["lens_kubeconfigs"], {})
            self.write(os.path.join(lens, "kubeconfigs", "c1"), "apiVersion: v1")
            self.assertEqual(mc.check_kube(self.root)["lens_kubeconfigs"], {lens: 1})

    def test_kubectl_lookup_order(self):
        fake = mock.Mock(lens_dirs=lambda: [], expand_kubeconfig=mc._expand,
                         lens_kubectls=lambda: {"1.28": "/lens/kubectl"})
        with mock.patch.object(mc, "_k8s", lambda: fake), mock.patch.object(mc.shutil, "which", lambda c: None):
            self.assertEqual(mc.find_kubectl(), "/lens/kubectl")          # Lens binary, no kubeconfig needed
            kb = self.write(os.path.join(self.home, "kubectl.exe"), "bin")
            os.environ["KUBECTL_BIN"] = kb
            self.assertEqual(mc.find_kubectl(), kb)
            os.environ["KUBECTL_BIN"] = os.path.join(self.home, "nope")
            self.assertEqual(mc.find_kubectl(), "")
        fake.lens_kubectls = lambda: {}
        os.environ.pop("KUBECTL_BIN")
        with mock.patch.object(mc, "_k8s", lambda: fake), mock.patch.object(mc.shutil, "which", lambda c: None):
            lens = os.path.join(self.home, "lens")
            fake.lens_dirs = lambda: [lens]
            self.write(os.path.join(lens, "kubeconfigs", "c1"), "apiVersion: v1")
            r = mc.collect(self.root)                                   # kubeconfig only, no kubectl
            self.assertEqual(r["features"]["k8s"]["state"], "partial")

    def test_env_file_is_the_one_config_loaded(self):
        fake = mock.Mock(_ENV_PATH="/elsewhere/.env", get=lambda k, d="": "")
        with mock.patch.object(mc, "config", fake):
            e = mc.check_env(self.root)
        self.assertEqual((e["file"], e["file_exists"]), ("/elsewhere/.env", True))
        with mock.patch.object(mc, "config", mock.Mock(_ENV_PATH=None, get=lambda k, d="": "")):
            self.assertFalse(mc.check_env(self.root)["file_exists"])

    def test_jenkins_needs_user(self):
        os.environ.update({"JENKINS_URL": "u", "JENKINS_TOKEN": "t"})
        self.assertEqual(mc.check_env(self.root)["groups"]["jenkins"]["missing"], ["JENKINS_USER"])

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

    def test_kube_registry_only_is_usable(self):
        valid = self.write(os.path.join(self.tmp, "custom", "valid"), "apiVersion: v1")
        self.write("work/projects.json", {"p": {"environments": {"dev": {"kubeconfig": valid}}}})
        k = mc.check_kube(self.root)
        self.assertTrue(k["ok"] and k["usable"])
        self.assertEqual(k["registry_refs"], {valid: True})

    def test_feature_states(self):
        self.assertEqual(mc._feature({"a": True, "b": True}, "h")["state"], "ready")
        self.assertEqual(mc._feature({"a": True, "b": False}, "h")["state"], "partial")
        f = mc._feature({"a": False}, "h")
        self.assertEqual((f["state"], f["hint"]), ("missing", "h"))


if __name__ == "__main__":
    unittest.main()
