#!/usr/bin/env python3
"""Stdlib unittest for the shared-platform layer of project_config.py and the
`work:` kubeconfig prefix of k8s.py. Run with:
    python -m unittest discover -s tests
"""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_TOOLS = os.path.join(_HERE, "..", ".claude", "skills", "dev-automation", "tools")
sys.path.insert(0, os.path.abspath(_TOOLS))

import k8s  # noqa: E402
import project_config  # noqa: E402

PLATFORMS = {
    "econtract": {"environments": {
        "prod": {"config_cloud": {"gitlab_repo": "g/config_new"},
                 "k8s": {"kubeconfig": "work:kube/fke.yaml", "context": "fke", "namespace": "shared"}},
        "dev": {"k8s": {"context": "dev-k8s", "namespace": "idaas"}},
    }},
}
PROJECTS = {
    "app": {"platform": "econtract",
            "stack": {"db": {"engine": "mysql"}},
            "environments": {"prod": {"k8s": {"namespace": "fpt-kyta", "deploy": "app-02"}},
                             "local": {"api_base_url": "http://localhost"}}},
    "plain": {"environments": {"dev": {"api_base_url": "x"}}},
}


class PlatformLayerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        for name, data in (("platforms.json", PLATFORMS), ("projects.json", PROJECTS)):
            with open(os.path.join(self.tmp.name, name), "w", encoding="utf-8") as f:
                json.dump(data, f)
        self.env = mock.patch.dict(os.environ, {"WORK_DIR": self.tmp.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_project_env_overrides_platform_env(self):
        k = project_config.resolve("app", "prod")["stack"]["k8s"]
        self.assertEqual(k["namespace"], "fpt-kyta")
        self.assertEqual(k["deploy"], "app-02")
        self.assertEqual(k["context"], "fke")
        self.assertEqual(k["kubeconfig"], "work:kube/fke.yaml")

    def test_platform_env_is_lowest_layer(self):
        stack = project_config.resolve("app", "prod")["stack"]
        self.assertEqual(stack["config_cloud"]["gitlab_repo"], "g/config_new")
        self.assertEqual(stack["db"]["engine"], "mysql")

    def test_platform_only_env_is_known(self):
        ctx = project_config.resolve("app", "dev")
        self.assertEqual(ctx["stack"]["k8s"]["namespace"], "idaas")
        self.assertEqual(ctx["known_envs"], ["dev", "local", "prod"])

    def test_unknown_env_still_rejected(self):
        with self.assertRaises(project_config.ResolveError):
            project_config.resolve("app", "uat")

    def test_project_without_platform_unchanged(self):
        ctx = project_config.resolve("plain", "dev")
        self.assertEqual(ctx["stack"], {"api_base_url": "x"})
        self.assertEqual(ctx["known_envs"], ["dev"])

    def test_work_prefix_kubeconfig(self):
        self.assertEqual(k8s.expand_kubeconfig("work:kube/fke.yaml"),
                         os.path.join(self.tmp.name, "kube", "fke.yaml"))


if __name__ == "__main__":
    unittest.main()
