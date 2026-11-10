#!/usr/bin/env python3
"""Stdlib unittest for jenkins_ops.py (k8s restart/rollback through a Jenkins
ops job). No Jenkins or cluster needed: HTTP is faked, tags come from a temp
git repo. Run with:
    python -m unittest discover -s tests
"""

import argparse
import os
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_TOOLS = os.path.join(_HERE, "..", ".claude", "skills", "dev-automation", "tools")
sys.path.insert(0, os.path.abspath(_TOOLS))

import jenkins_ops as ops  # noqa: E402

STACK = {"jenkins": {
    "job": "job/Special-SANBOX/job/P-KYTA/job/etask/job/etask",
    "agent": "tranglv", "k8s_credential": "kubeconfig-x",
    "resource": "deployment/kyta-etask", "container": "kyta-etask",
    "namespace": "fpt-sanbox-kyta", "image": "registry.example/p/prod-kyta-etask",
    "git_tag_prefix": "prod-econ.",
}}


def run_args(**kw):
    base = dict(action="status", tag=None, dry_run=True, wait=False, timeout=60, _clone_dir="")
    base.update(kw)
    return argparse.Namespace(**base)


class SpecTest(unittest.TestCase):
    def test_ops_job_sits_beside_build_job(self):
        spec = ops.ops_spec(STACK)
        self.assertEqual(spec["folder"], "job/Special-SANBOX/job/P-KYTA/job/etask")
        self.assertEqual(spec["ops_job"], "job/Special-SANBOX/job/P-KYTA/job/etask/job/etask-ops")

    def test_missing_fields_rejected(self):
        with self.assertRaises(ops.OpsError):
            ops.ops_spec({"jenkins": {"job": "job/a/job/b"}})

    def test_unsafe_value_rejected(self):
        bad = {"jenkins": dict(STACK["jenkins"], namespace="ns'; rm -rf /")}
        with self.assertRaises(ops.OpsError):
            ops.ops_spec(bad)


class RenderTest(unittest.TestCase):
    def test_xml_is_valid_and_parameterized(self):
        root = ET.fromstring(ops.render_xml(ops.ops_spec(STACK)).encode("utf-8"))
        names = [n.text for n in root.iter("name")]
        self.assertEqual(names, ["ACTION", "TAG", "REPLICAS"])
        script = root.find("definition/script").text
        for needle in ("label 'tranglv'", "credentials('kubeconfig-x')", "NS = 'fpt-sanbox-kyta'",
                       "set image $RES $CONTAINER=$IMAGE:$TAG", "params.ACTION == 'rollback'"):
            self.assertIn(needle, script)
        self.assertEqual(root.find("definition/sandbox").text, "true")


class RunTest(unittest.TestCase):
    def setUp(self):
        self.spec = ops.ops_spec(STACK)

    def test_rollback_requires_valid_tag(self):
        for tag in (None, "latest", "1.2"):
            with self.assertRaises(ops.OpsError):
                ops.cmd_run(self.spec, run_args(action="rollback", tag=tag))

    def test_rollback_unknown_tag_rejected(self):
        with self.assertRaises(ops.OpsError):
            ops.cmd_run(self.spec, run_args(action="rollback", tag="9.9.9"),
                        tags_fn=lambda *a: ["1.2.3"])

    def test_tag_only_for_rollback(self):
        with self.assertRaises(ops.OpsError):
            ops.cmd_run(self.spec, run_args(action="restart", tag="1.2.3"))

    def test_rollback_dry_run_targets_ops_job_with_params(self):
        with mock.patch.dict(os.environ, {"JENKINS_URL": "http://j"}):
            out = ops.cmd_run(self.spec, run_args(action="rollback", tag="1.2.3"),
                              tags_fn=lambda *a: ["1.2.3"])
        self.assertTrue(out["dry_run"])
        self.assertIn("/job/etask/job/etask-ops/buildWithParameters?", out["url"])
        self.assertEqual(out["params"], {"ACTION": "rollback", "TAG": "1.2.3", "REPLICAS": ""})

    def test_scale_requires_replicas_in_range(self):
        for n in (None, 0, ops.MAX_REPLICAS + 1):
            with self.assertRaises(ops.OpsError):
                ops.cmd_run(self.spec, run_args(action="scale", replicas=n))

    def test_replicas_only_for_scale(self):
        with self.assertRaises(ops.OpsError):
            ops.cmd_run(self.spec, run_args(action="restart", replicas=3))

    def test_scale_dry_run_passes_replicas(self):
        with mock.patch.dict(os.environ, {"JENKINS_URL": "http://j"}):
            out = ops.cmd_run(self.spec, run_args(action="scale", replicas=3))
        self.assertEqual(out["params"], {"ACTION": "scale", "TAG": "", "REPLICAS": "3"})


class CreateTest(unittest.TestCase):
    def setUp(self):
        self.spec = ops.ops_spec(STACK)
        self.env = mock.patch.dict(os.environ, {"JENKINS_URL": "http://j", "JENKINS_USER": "u",
                                                "JENKINS_TOKEN": "t"})
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def test_create_posts_to_folder_create_item(self):
        with mock.patch.object(ops.jenkins, "_crumb_header", return_value={}), \
             mock.patch.object(ops.jenkins, "_req", return_value=(200, {}, "")) as req:
            out = ops.cmd_create(self.spec, argparse.Namespace(update=False, dry_run=False))
        self.assertTrue(out["passed"])
        method, url = req.call_args[0][:2]
        self.assertEqual(method, "POST")
        self.assertEqual(url, "http://j/job/Special-SANBOX/job/P-KYTA/job/etask/createItem?name=etask-ops")

    def test_update_posts_config_xml(self):
        with mock.patch.object(ops.jenkins, "_crumb_header", return_value={}), \
             mock.patch.object(ops.jenkins, "_req", return_value=(200, {}, "")) as req:
            ops.cmd_create(self.spec, argparse.Namespace(update=True, dry_run=False))
        self.assertTrue(req.call_args[0][1].endswith("/job/etask/job/etask-ops/config.xml"))

    def test_existing_job_hint(self):
        with mock.patch.object(ops.jenkins, "_crumb_header", return_value={}), \
             mock.patch.object(ops.jenkins, "_req", return_value=(400, {}, "A job already exists with the name")):
            out = ops.cmd_create(self.spec, argparse.Namespace(update=False, dry_run=False))
        self.assertTrue(out["error"])
        self.assertIn("--update", out["message"])


ORIGINAL = ("<?xml version='1.1'?><flow-definition><description>build</description><definition>"
            "<script>pipeline { stages { stage('Build') { steps { sh 'mvn &amp;&amp; deploy' } } } }</script>"
            "<sandbox>true</sandbox></definition><triggers/></flow-definition>")


class FakeJenkins:
    """In-memory build job: GET/POST config.xml, job state; records every call."""

    def __init__(self, busy=False, broken_restore=False):
        self.config, self.busy, self.broken_restore = ORIGINAL, busy, broken_restore
        self.posts = []

    def req(self, method, url, data=None, extra_headers=None):
        if method == "GET" and url.endswith("/config.xml"):
            return 200, {}, self.config
        if method == "GET" and "api/json" in url:
            return 200, {}, '{"inQueue": false, "lastBuild": {"number": 1, "building": %s}}' % (
                "true" if self.busy else "false")
        if method == "POST" and url.endswith("/config.xml"):
            body = data.decode("utf-8")
            self.posts.append(body)
            if self.broken_restore and body == ORIGINAL:
                return 500, {}, "boom"
            self.config = body
            return 200, {}, ""
        raise AssertionError(f"unexpected {method} {url}")


class SwapTest(unittest.TestCase):
    def setUp(self):
        self.spec = ops.ops_spec(STACK)
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [mock.patch.dict(os.environ, {"JENKINS_URL": "http://j", "JENKINS_USER": "u",
                                                     "JENKINS_TOKEN": "t", "WORK_DIR": self.tmp.name}),
                        mock.patch.object(ops.jenkins, "_crumb_header", return_value={}),
                        mock.patch.object(ops.time, "sleep")]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def _run(self, fake, build=None, **kw):
        args = run_args(dry_run=False, via="swap", project="etask", env="sandbox", **kw)
        build = build or mock.MagicMock(return_value={"passed": True, "result": "SUCCESS", "number": 2})
        with mock.patch.object(ops.jenkins, "_req", side_effect=fake.req), \
             mock.patch.object(ops.jenkins, "cmd_build", build):
            return ops.cmd_run(self.spec, args, tags_fn=lambda *a: ["1.2.3"]), build

    def test_baked_script_has_no_params(self):
        s = ops.render_script(self.spec, "rollback", "1.2.3")
        self.assertNotIn("params.", s)
        self.assertIn("'rollback' == 'rollback'", s)
        self.assertIn("$IMAGE:1.2.3", s)
        self.assertTrue(s.startswith(ops.SWAP_MARK))

    def test_baked_scale_script(self):
        s = ops.render_script(self.spec, "scale", "", 3)
        self.assertNotIn("params.", s)
        self.assertIn("'scale' == 'scale'", s)
        self.assertIn("REPLICAS = \"${'3'}\"", s)
        self.assertIn("--replicas $REPLICAS", s)

    def test_restore_verify_ignores_jenkins_reserialization(self):
        saved = ORIGINAL.replace("&amp;&amp;", "&#38;&#38;").replace("<description>build</description>",
                                                                  "<description>build</description>")
        self.assertTrue(ops._same_script(ORIGINAL, saved.replace("sh 'mvn", "sh &apos;mvn")))
        self.assertFalse(ops._same_script(ORIGINAL, ORIGINAL.replace("mvn", "gradle")))

    def test_swap_xml_replaces_only_script(self):
        out = ops.swap_xml(ORIGINAL, "new <script> & stuff")
        self.assertIn("<script>new &lt;script&gt; &amp; stuff</script>", out)
        self.assertEqual(out.replace(ops._script_of(out), ""), ORIGINAL.replace(ops._script_of(ORIGINAL), ""))

    def test_swap_runs_then_restores_original(self):
        fake = FakeJenkins()
        out, build = self._run(fake, action="restart")
        self.assertTrue(out["passed"])
        self.assertEqual(out["restore"], {"ok": True})
        self.assertIn(ops.SWAP_MARK.replace("'", "&apos;")[:20], fake.posts[0])
        self.assertEqual(fake.posts[-1], ORIGINAL)
        self.assertEqual(fake.config, ORIGINAL)
        self.assertEqual(build.call_args[0][0].path, self.spec["job"])
        self.assertTrue(os.path.isfile(out["backup"]))
        with open(out["backup"], encoding="utf-8") as f:
            self.assertEqual(f.read(), ORIGINAL)

    def test_restore_happens_even_if_build_crashes(self):
        fake = FakeJenkins()
        with self.assertRaises(RuntimeError):
            self._run(fake, build=mock.MagicMock(side_effect=RuntimeError("network")), action="restart")
        self.assertEqual(fake.config, ORIGINAL)

    def test_busy_job_refused_without_touching_config(self):
        fake = FakeJenkins(busy=True)
        with self.assertRaises(ops.OpsError):
            self._run(fake, action="restart")
        self.assertEqual(fake.posts, [])

    def test_failed_restore_is_loud(self):
        fake = FakeJenkins(broken_restore=True)
        out, _ = self._run(fake, action="restart")
        self.assertTrue(out["error"])
        self.assertIn("NOT RESTORED", out["message"])
        self.assertIn(out["backup"], out["message"])

    def test_swap_dry_run_touches_nothing(self):
        fake = FakeJenkins()
        args = run_args(dry_run=True, via="swap", action="rollback", tag="1.2.3")
        with mock.patch.object(ops.jenkins, "_req", side_effect=fake.req) as req:
            out = ops.cmd_run(self.spec, args, tags_fn=lambda *a: ["1.2.3"])
        self.assertTrue(out["dry_run"])
        req.assert_not_called()


class TagsTest(unittest.TestCase):
    def test_versions_sorted_numerically_and_filtered(self):
        with tempfile.TemporaryDirectory() as d:
            git = lambda *a: subprocess.run(["git", "-C", d, *a], check=True, capture_output=True)  # noqa: E731
            git("init", "-q")
            git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "x")
            for t in ("prod-econ.0.9.45", "prod-econ.0.10.45", "prod-econ.bad", "demo.0.0.1"):
                git("tag", t)
            self.assertEqual(ops.list_tags(d, "prod-econ.", 10), ["0.10.45", "0.9.45"])


if __name__ == "__main__":
    unittest.main()
