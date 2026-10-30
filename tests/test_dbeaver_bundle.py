#!/usr/bin/env python3
"""DBeaver credential store round-trip, project write/registration, platform forwards of podfwd.
    python -m unittest discover -s tests
"""

import argparse
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, ".."))
sys.path.insert(0, os.path.join(_ROOT, ".claude", "skills", "_runtime"))
sys.path.insert(0, os.path.join(_ROOT, ".claude", "skills", "dev-automation", "tools"))

from agent_skill_toolkit.integrations import dbeaver_store as ds  # noqa: E402

HAS_OPENSSL = bool(shutil.which("openssl"))


@unittest.skipUnless(HAS_OPENSSL, "openssl CLI needed")
class StoreTest(unittest.TestCase):
    def test_round_trip(self):
        creds = {"mysql8-1": {"#connection": {"user": "u", "password": "p@ss ✓"}}}
        raw = ds.encrypt_credentials(creds)
        self.assertNotIn(b"p@ss", raw)
        self.assertEqual(ds.decrypt_credentials(raw), creds)

    def test_write_project_registers(self):
        ws = tempfile.mkdtemp()
        data = {"folders": {"PROD": {}}, "connections": {"c1": {"name": "x", "folder": "PROD"}}}
        path = ds.write_project(ws, "Kyta", data, {"c1": {"#connection": {"user": "a"}}})
        self.assertTrue(os.path.isfile(os.path.join(path, ".project")))
        self.assertTrue(os.path.isdir(os.path.join(ws, ".metadata", ".plugins", "org.eclipse.core.resources",
                                                   ".projects", "Kyta")))
        self.assertEqual(ds.projects(ws), ["Kyta"])
        back = ds.read_project(path)
        self.assertEqual(back["data"], data)
        self.assertEqual(back["creds"]["c1"]["#connection"]["user"], "a")


class PlatformForwardsTest(unittest.TestCase):
    PLAT = {"k8s_access": {"pf": {"kubeconfig": "work:kube/x.yaml", "context": "c", "namespace": "fpt-tool"}},
            "forwards": {"_doc": "x",
                         "sb-a": {"access": "pf", "target": "svc/s", "ports": "13308:3308", "group": "sandbox"},
                         "prod-b": {"access": "pf", "target": "svc/p", "ports": "23326:3326", "group": "prod",
                                    "prod": True}}}

    def setUp(self):
        import podfwd
        self.pf = podfwd
        p = mock.patch.object(podfwd.project_config, "load_platform", return_value=self.PLAT)
        p.start()
        self.addCleanup(p.stop)

    def test_filters(self):
        self.assertEqual([f["name"] for f in self.pf.platform_forwards("econtract")], ["sb-a", "prod-b"])
        self.assertEqual([f["name"] for f in self.pf.platform_forwards("econtract", group="prod")], ["prod-b"])
        self.assertEqual([f["name"] for f in self.pf.platform_forwards("econtract", only="sb-a")], ["sb-a"])
        with self.assertRaises(self.pf.k8s.K8sError):
            self.pf.platform_forwards("econtract", only="nope")

    def test_prod_forward_needs_allow_prod(self):
        calls = []

        def fake_start(args, target, ports, name=""):
            calls.append((name, args.kubeconfig, args.context, args.namespace, args._ctx["protected"]))
            return {"name": name}

        args = argparse.Namespace(platform="econtract", group="", only="", allow_prod=False, insecure=False,
                                  project=None, env=None, context=None, kubeconfig=None, namespace=None)
        with mock.patch.object(self.pf, "start", side_effect=fake_start):
            out = self.pf.start_saved(args)
        self.assertEqual([s["name"] for s in out["started"]], ["sb-a", "prod-b"])
        self.assertEqual(calls[0][1:4], ("work:kube/x.yaml", "c", "fpt-tool"))
        self.assertEqual([c[4] for c in calls], [False, True])


if __name__ == "__main__":
    unittest.main()
