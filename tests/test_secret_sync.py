#!/usr/bin/env python3
"""secret_sync + core.filesync: two temp toolkit roots stand in for this machine and the peer; the SSH hop
is replaced by running filesync.main on the peer root, so no network and never the real .env.
    python -m unittest discover -s tests
"""

import base64
import io
import json
import os
import runpy
import shutil
import sys
import tempfile
import unittest
from unittest import mock

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
runpy.run_path(os.path.join(_ROOT, ".claude", "skills", "_runtime", "bootstrap.py"))["activate"]()
from agent_skill_toolkit.core import filesync  # noqa: E402
from agent_skill_toolkit.integrations import secret_sync  # noqa: E402


def write(root, rel, text):
    full = os.path.join(root, *rel.split("/"))
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def read(root, rel):
    with open(os.path.join(root, *rel.split("/")), encoding="utf-8") as f:
        return f.read()


class FilesyncTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="fs-")
        self.addCleanup(shutil.rmtree, self.root, True)

    def test_walk_skips_backups_and_lock_files(self):
        for rel in ("work/memory/a.md", "work/memory/backups/old.md", "work/projects.json.lock",
                    "work/projects.json.bak-20261023", "work/kube/x.yaml"):
            write(self.root, rel, "x")
        got = filesync.walk(self.root, ["work/memory", "work/kube", "work/projects.json", "missing"])
        self.assertEqual(got, ["work/memory/a.md", "work/kube/x.yaml"])

    def test_portable_rewrites_windows_and_posix_registry_paths(self):
        win = "D:/Tool/Kit"
        data = json.dumps({"a": {"clone_dir": "d:/tool/kit/work/gitlab/g/r"},
                           "b": {"clone_dir": "D:\\Tool\\Kit\\work\\gitlab\\g\\s"},
                           "c": {"clone_dir": "E:/elsewhere/r"}}).encode()
        with mock.patch.object(filesync.os.path, "abspath", lambda p: p):
            out = json.loads(filesync.portable("work/projects.json", data, win))
        self.assertEqual(out["a"]["clone_dir"], "work:gitlab/g/r")
        self.assertEqual(out["b"]["clone_dir"], "work:gitlab/g/s")
        self.assertEqual(out["c"]["clone_dir"], "E:/elsewhere/r")
        posix = json.dumps({"a": {"clone_dir": "/home/u/kit/work/gitlab/g/r"}}).encode()
        with mock.patch.object(filesync.os.path, "abspath", lambda p: p):
            out = json.loads(filesync.portable("work/projects.json", posix, "/home/u/kit"))
        self.assertEqual(out["a"]["clone_dir"], "work:gitlab/g/r")
        self.assertEqual(filesync.portable(".env", b"D:/Tool/Kit/work/x", win), b"D:/Tool/Kit/work/x")

    def test_unpack_backs_up_replaced_files_and_rejects_traversal(self):
        write(self.root, ".env", "OLD=1\n")
        src = tempfile.mkdtemp(prefix="fs-src-")
        self.addCleanup(shutil.rmtree, src, True)
        write(src, ".env", "NEW=1\n")
        write(src, "work/kube/k.yaml", "k\n")
        res = filesync.unpack(self.root, filesync.pack(src, [".env", "work/kube/k.yaml"]),
                              os.path.join(self.root, "bk"))
        self.assertEqual(read(self.root, ".env"), "NEW=1\n")
        self.assertEqual(res["replaced"], [".env"])
        self.assertTrue(os.path.isfile(res["backup"]))
        bad = base64.b64encode(filesync._tar([("../evil", b"x", 0)])).decode()
        with self.assertRaises(ValueError):
            filesync.unpack(self.root, bad, os.path.join(self.root, "bk"))
        self.assertFalse(os.path.exists(os.path.join(os.path.dirname(self.root), "evil")))

    def test_peer_main_refuses_non_toolkit_root(self):
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            rc = filesync.main("manifest", {"root": self.root, "paths": [".env"]})
        self.assertEqual(rc, 1)
        self.assertIn("not a toolkit root", out.getvalue())


class SecretSyncTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ss-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.local, self.peer = os.path.join(self.tmp, "local"), os.path.join(self.tmp, "peer")
        for r in (self.local, self.peer):
            write(r, "agentkit.py", "")
        write(self.local, "work/hosts.json", json.dumps({"pc": {"host": "h", "toolkit_dir": self.peer}}))
        env = mock.patch.dict(os.environ, {"TOOLKIT_ROOT": self.local, "WORK_DIR": os.path.join(self.local, "work"),
                                           "TOOLKIT_STATE_DIR": os.path.join(self.local, "temp")})
        env.start()
        self.addCleanup(env.stop)
        fake = mock.patch.object(secret_sync, "_remote", self._fake_remote)
        fake.start()
        self.addCleanup(fake.stop)

    def _fake_remote(self, spec, op, args, data=None):
        """The real peer boot (PEER_BOOT) fed the real envelope, minus the ssh hop."""
        with open(filesync.__file__, encoding="utf-8") as f:
            envelope = {"src": f.read(), "op": op, "args": {"root": spec["toolkit_dir"], **args}, "data": data}
        out = io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch("sys.stdin", io.StringIO(json.dumps(envelope))):
            with self.assertRaises(SystemExit):
                exec(secret_sync.PEER_BOOT, {})
        return json.loads(out.getvalue())

    def test_push_dry_run_then_apply_then_same(self):
        write(self.local, ".env", "A=1\n")
        dry = secret_sync.push("pc")
        self.assertTrue(dry["dry_run"])
        self.assertIn(".env", dry["files"])
        self.assertFalse(os.path.exists(os.path.join(self.peer, ".env")))
        done = secret_sync.push("pc", apply=True)
        self.assertIn(".env", done["written"])
        self.assertEqual(read(self.peer, ".env"), "A=1\n")
        self.assertEqual(secret_sync.status("pc")["files"], [])

    def test_one_side_change_goes_that_way_both_sides_is_conflict(self):
        write(self.local, ".env", "A=1\n")
        secret_sync.push("pc", apply=True)
        write(self.peer, ".env", "A=2\n")
        self.assertEqual(secret_sync.status("pc")["files"][0]["state"], "remote-changed")
        self.assertEqual(secret_sync.push("pc", apply=True)["files"], [])
        secret_sync.pull("pc", apply=True)
        self.assertEqual(read(self.local, ".env"), "A=2\n")
        write(self.local, ".env", "A=3\n")
        write(self.peer, ".env", "A=4\n")
        res = secret_sync.push("pc", apply=True)
        self.assertEqual(res["files"], [])
        self.assertEqual(res["conflicts"][0]["path"], ".env")
        secret_sync.push("pc", force=True, apply=True)
        self.assertEqual(read(self.peer, ".env"), "A=3\n")
        self.assertTrue(os.listdir(os.path.join(self.peer, "temp", "secret-sync", "backups")))

    def test_registry_lands_portable_on_the_peer(self):
        clone = os.path.join(self.local, "work", "gitlab", "g", "r").replace("\\", "/")
        write(self.local, "work/projects.json", json.dumps({"p": {"clone_dir": clone}}))
        secret_sync.push("pc", only=["work/projects.json"], apply=True)
        self.assertEqual(json.loads(read(self.peer, "work/projects.json"))["p"]["clone_dir"], "work:gitlab/g/r")
        self.assertEqual(secret_sync.status("pc", ["work/projects.json"])["files"], [])

    def test_unknown_host_or_missing_toolkit_dir(self):
        with self.assertRaises(LookupError):
            secret_sync.status("nope")


if __name__ == "__main__":
    unittest.main()
