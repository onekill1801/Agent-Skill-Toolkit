#!/usr/bin/env python3
"""Stdlib unittest for k8s.py (read-only kubectl wrapper). No cluster needed:
subprocess.run is faked. Run with:
    python -m unittest discover -s tests
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_TOOLS = os.path.join(_HERE, "..", ".claude", "skills", "dev-automation", "tools")
sys.path.insert(0, os.path.abspath(_TOOLS))

import k8s  # noqa: E402

T = {"kubectl": "kubectl", "kubeconfig": "/kc", "context": "ctx", "namespace": "ns"}


def done(stdout="", stderr="", rc=0):
    return subprocess.CompletedProcess([], rc, stdout, stderr)


class ReadOnlyGuardTest(unittest.TestCase):
    def test_write_verbs_refused_before_exec(self):
        with mock.patch("subprocess.run") as run:
            for verb in ("delete", "apply", "patch", "rollout", "scale", "exec", "edit", "set"):
                with self.assertRaises(k8s.K8sError) as cm:
                    k8s._kubectl(T, [verb, "x"])
                self.assertEqual(cm.exception.kind, "usage")
            run.assert_not_called()

    def test_config_only_view(self):
        with self.assertRaises(k8s.K8sError):
            k8s._kubectl(T, ["config", "use-context", "x"])

    def test_always_explicit_context_and_namespace(self):
        with mock.patch("subprocess.run", return_value=done("{}")) as run:
            k8s._kubectl(T, ["get", "pods"])
        cmd = run.call_args[0][0]
        self.assertEqual(cmd[:5], ["kubectl", "--kubeconfig", "/kc", "--context", "ctx"])
        self.assertIn("-n", cmd)


class ClassifyTest(unittest.TestCase):
    def kind(self, err):
        return k8s.classify(err, 1).kind

    def test_kinds(self):
        self.assertEqual(self.kind("error: You must be logged in to the server (Unauthorized)"), "auth")
        self.assertEqual(self.kind('pods is forbidden: User "x" cannot list'), "forbidden")
        self.assertEqual(self.kind('Error from server (NotFound): pods "a" not found'), "not_found")
        self.assertEqual(self.kind("x509: certificate signed by unknown authority"), "tls")
        self.assertEqual(self.kind("dial tcp 10.0.0.1:6443: i/o timeout"), "unreachable")
        self.assertEqual(self.kind('error: context "zz" does not exist'), "config")

    def test_message_is_last_line(self):
        e = k8s.classify("E1009 noise\nE1009 noise\nerror: You must be logged in\n", 1)
        self.assertEqual(e.message, "error: You must be logged in")


class PathsTest(unittest.TestCase):
    def test_lens_prefix(self):
        tmp = tempfile.mkdtemp()
        os.makedirs(os.path.join(tmp, "Lens", "kubeconfigs"))
        open(os.path.join(tmp, "Lens", "kubeconfigs", "a.yaml"), "w").close()
        with mock.patch.object(k8s, "lens_dirs", return_value=[os.path.join(tmp, "Lens")]):
            self.assertEqual(k8s.expand_kubeconfig("lens:a.yaml"),
                             os.path.join(tmp, "Lens", "kubeconfigs", "a.yaml"))

    def test_home(self):
        self.assertEqual(k8s.expand_kubeconfig("~/.kube/config"),
                         os.path.join(os.path.expanduser("~"), ".kube", "config"))

    def test_kubectl_prefers_requested_lens_version(self):
        with mock.patch.object(k8s, "lens_kubectls", return_value={"1.26": "/l/126", "1.29": "/l/129"}), \
             mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("KUBECTL_BIN", None)
            self.assertEqual(k8s.find_kubectl("1.26"), "/l/126")
            with mock.patch("shutil.which", return_value=None):
                self.assertEqual(k8s.find_kubectl(), "/l/129")


class TargetTest(unittest.TestCase):
    def args(self, **kw):
        base = dict(project=None, env=None, context=None, kubeconfig=None, namespace=None, insecure=False)
        base.update(kw)
        return Namespace(**base)

    def test_no_target_refuses_current_context(self):
        with self.assertRaises(k8s.K8sError) as cm:
            k8s.resolve_target(self.args())
        self.assertEqual(cm.exception.kind, "usage")

    def test_registry_block(self):
        kc = tempfile.NamedTemporaryFile(delete=False).name
        ctx = {"env": "dev", "stack": {"k8s": {"kubeconfig": kc, "context": "c1", "namespace": "n1"}}}
        with mock.patch.object(k8s.project_config, "resolve", return_value=ctx), \
             mock.patch.object(k8s, "find_kubectl", return_value="kubectl"):
            t = k8s.resolve_target(self.args(project="p", env="dev", namespace="override"))
        self.assertEqual((t["context"], t["namespace"]), ("c1", "override"))


class SummaryTest(unittest.TestCase):
    def test_pod_problems(self):
        pod = {"metadata": {"name": "api-1", "creationTimestamp": "2026-01-01T00:00:00Z"},
               "spec": {"nodeName": "n", "containers": [{"image": "img:1"}]},
               "status": {"phase": "Running", "containerStatuses": [{
                   "name": "api", "ready": False, "restartCount": 4,
                   "state": {"waiting": {"reason": "CrashLoopBackOff"}},
                   "lastState": {"terminated": {"reason": "OOMKilled", "exitCode": 137}}}]}}
        s = k8s.summarize_pod(pod)
        self.assertEqual((s["ready"], s["restarts"]), ("0/1", 4))
        self.assertIn("api: CrashLoopBackOff", s["problems"])
        self.assertTrue(any("OOMKilled" in p for p in s["problems"]))

    def test_filter_lines_context(self):
        lines = ["a", "b", "ERROR x", "c", "d"]
        self.assertEqual(k8s.filter_lines(lines, "ERROR", 1), ["b", "ERROR x", "c"])


class PatternTest(unittest.TestCase):
    def test_error_styles(self):
        for ln in ('2026 ERROR c.f.Svc - boom', '{"level":"error","msg":"x"}', '{"level":"ERROR"}',
                   '[pod/a/c] 2026-10-09T13:41:12+07:00 E1009 13:41:12.33 1 tablet.go:1] x'):
            self.assertTrue(k8s.ERROR_PATTERNS.search(ln), ln)
        self.assertFalse(k8s.ERROR_PATTERNS.search('{"level":"info","msg":"no errors here"}'))

    def test_warn_styles(self):
        for ln in ('WARN x', '{"level":"warn"}', ' W1009 13:41:12.1 x'):
            self.assertTrue(k8s.WARN_PATTERNS.search(ln), ln)


class LogsTest(unittest.TestCase):
    def test_deploy_expands_to_selector_all_pods(self):
        deploy = {"spec": {"selector": {"matchLabels": {"app": "api", "tier": "be"}}}}
        calls = []

        def fake(cmd, **kw):
            calls.append(cmd)
            if "get" in cmd:
                return done(json.dumps(deploy))
            return done("2026 [pod/a] INFO ok\n2026 [pod/b] ERROR boom\n")

        a = Namespace(project=None, env=None, context="ctx", kubeconfig=None, namespace="ns", insecure=False,
                      target="deploy/api", selector=None, container=None, since="10m", tail=100,
                      previous=False, grep=None, errors=False, context_lines=0)
        with mock.patch("subprocess.run", side_effect=fake), \
             mock.patch.object(k8s, "find_kubectl", return_value="kubectl"):
            out = k8s.cmd_logs(a)
        logs_cmd = calls[-1]
        self.assertIn("app=api,tier=be", logs_cmd)
        self.assertIn("--prefix", logs_cmd)
        self.assertEqual(out["stats"]["errors"], 1)


if __name__ == "__main__":
    unittest.main()
