#!/usr/bin/env python3
"""Refactor safety net (checklist F.10c): snapshot + metrics rungs read their reference from the BASE ref;
`refactor.py snapshot|metrics record` writes it on purpose. Real git + the real verify ladder.
    python -m unittest discover -s tests
"""

import json
import os
import runpy
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
runpy.run_path(os.path.join(_ROOT, ".claude", "skills", "_runtime", "bootstrap.py"))["activate"]()
from agent_skill_toolkit.core import store as S  # noqa: E402
from agent_skill_toolkit.workflows.verify import ladder as L  # noqa: E402
from agent_skill_toolkit.workflows.verify import safety  # noqa: E402

sys.path.insert(0, os.path.join(_ROOT, ".claude", "skills", "auto-dev", "tools"))
import refactor as RF  # noqa: E402
import verify as V  # noqa: E402

PY = sys.executable.replace("\\", "/")


def git(repo, *a):
    return subprocess.run(["git", "-C", repo, *a], check=True, capture_output=True, text=True, encoding="utf-8",
                          errors="replace").stdout.strip()


class UnitTest(unittest.TestCase):
    def test_snapshot_normalisation(self):
        self.assertTrue(safety.compare_snapshot('{"b": 1, "a": [1, 2]}', '{"a":[1,2],"b":1}', "json")["equal"])
        self.assertTrue(safety.compare_snapshot("x  \r\ny\n\n", "x\ny", "text")["equal"])
        diff = safety.compare_snapshot('{"a": 1}', '{"a": 2}', "json")
        self.assertFalse(diff["equal"])
        self.assertTrue(any(ln.startswith("+") and "2" in ln for ln in diff["diff"]))
        self.assertFalse(safety.compare_snapshot("{}", "not json", "json")["equal"])

    def test_metrics_directions_and_tolerance(self):
        base = safety.parse_baseline(json.dumps({"metrics": {
            "latency_ms": {"value": 100, "tolerance": 0.1},
            "rps": {"value": 500, "higher_is_better": True, "tolerance": 0.05},
            "errors": {"value": 0, "tolerance": 0.5}}}))
        ok = safety.compare_metrics(base, {"latency_ms": 109, "rps": 476, "errors": 0.4})
        self.assertTrue(ok["passed"], ok)
        bad = safety.compare_metrics(base, {"latency_ms": 111, "rps": 474})
        self.assertFalse(bad["passed"])
        self.assertEqual(bad["missing"], ["errors"])
        self.assertEqual({r["name"] for r in bad["rows"] if not r["ok"]}, {"latency_ms", "rps"})
        for doc in ('{"metrics": {}}', '{"metrics": {"a": {"value": true}}}', '{"metrics": {"a": {"value": 1, "tolerance": 2}}}'):
            with self.assertRaises(ValueError):
                safety.parse_baseline(doc)
        with self.assertRaises(ValueError):
            safety.parse_metrics('{"a": "fast"}')

    def test_record_keeps_baseline_settings(self):
        prev = {"rps": {"value": 1, "higher_is_better": True, "tolerance": 0.2}}
        doc = json.loads(safety.baseline_doc({"rps": 900.0, "latency_ms": 12.0}, prev))
        self.assertEqual(doc["metrics"]["rps"], {"value": 900.0, "higher_is_better": True, "tolerance": 0.2})
        self.assertEqual(doc["metrics"]["latency_ms"]["higher_is_better"], False)


class LadderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="safety-")
        mock.patch.dict(os.environ, {"TOOLKIT_STORE": os.path.join(self.tmp, "t.db"), "TOOLKIT_STATE_DIR": self.tmp,
                                     "WORK_DIR": self.tmp, "TOOLKIT_MAX_PARALLEL": "0"}).start()
        mock.patch.object(L, "_agent", side_effect=AssertionError("no agent may run here")).start()
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(os.path.join(self.repo, "scripts"))
        self.write("scripts/api.py", "import json\nprint(json.dumps({'GET /tasks': ['id', 'name']}))\n")
        self.write("scripts/bench.py", "import json\nprint(json.dumps({'latency_ms': 100}))\n")
        self.write(".gitignore", "__pycache__/\n")
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "t@t")
        git(self.repo, "config", "user.name", "t")
        self.spec = os.path.join(self.tmp, "spec.yml")
        with open(self.spec, "w") as f:
            f.write(textwrap.dedent(f"""
                verify:
                  base: main
                  ladder:
                    contract:
                      kind: snapshot
                      run:
                        - {PY}
                        - scripts/api.py
                      snapshot: .agent/snapshots/api.json
                      format: json
                      protects:
                        - scripts/api.py
                    perf:
                      kind: metrics
                      run:
                        - {PY}
                        - scripts/bench.py
                      baseline: .agent/baseline.json
                      tolerance: 0.1
                      protects:
                        - scripts/bench.py
                """))
        RF.record("snapshot", self.repo, "contract", self.spec)
        RF.record("metrics", self.repo, "perf", self.spec)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "init + recorded safety net")
        git(self.repo, "checkout", "-qb", "feat/x")

    def tearDown(self):
        mock.patch.stopall()

    def write(self, rel, body):
        p = os.path.join(self.repo, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(body)

    def commit(self, msg="change"):
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", msg)

    def verify(self):
        V.start(self.repo, "main", self.spec, "implement", False)
        ran = V.work("w", once=True, interval=0)["ran"]
        with S.Store() as st:
            t = st.list(kind=L.KIND, limit=5)[0]
        return ran[-1]["status"], t

    def test_recorded_files(self):
        self.assertEqual(json.load(open(os.path.join(self.repo, ".agent/snapshots/api.json"))), {"GET /tasks": ["id", "name"]})
        self.assertEqual(json.load(open(os.path.join(self.repo, ".agent/baseline.json")))["metrics"]["latency_ms"],
                         {"value": 100.0, "higher_is_better": False, "tolerance": 0.1})

    def test_unchanged_behaviour_passes(self):
        self.write("scripts/notes.txt", "refactor\n")
        self.commit()
        status, t = self.verify()
        self.assertEqual(status, "done", t.get("last_error"))

    def test_contract_change_stops_for_a_human(self):
        self.write("scripts/api.py", "import json\nprint(json.dumps({'GET /tasks': ['id']}))\n")
        self.commit("drop a field")
        status, t = self.verify()
        self.assertEqual(status, "blocked")
        self.assertIn("not auto-fixable", t["last_error"])
        self.assertIn('"name"', t["last_error"])

    def test_performance_regression_stops(self):
        self.write("scripts/bench.py", "import json\nprint(json.dumps({'latency_ms': 125}))\n")
        self.commit("slower")
        status, t = self.verify()
        self.assertEqual(status, "blocked")
        self.assertIn("latency_ms: 125", t["last_error"])

    def test_branch_cannot_re_record_its_own_reference(self):
        self.write("scripts/api.py", "import json\nprint(json.dumps({'GET /tasks': ['id']}))\n")
        RF.record("snapshot", self.repo, "contract", self.spec)          # snapshot re-recorded ON the branch
        self.commit("change + new snapshot")
        status, _ = self.verify()
        self.assertEqual(status, "blocked")                            # the BASE snapshot is the reference

    def test_missing_reference_on_base_blocks(self):
        git(self.repo, "checkout", "-q", "main")
        git(self.repo, "rm", "-q", ".agent/baseline.json")
        git(self.repo, "commit", "-qm", "drop baseline")
        git(self.repo, "checkout", "-q", "feat/x")
        git(self.repo, "rebase", "-q", "main")
        status, t = self.verify()
        self.assertEqual(status, "blocked")
        self.assertIn("metrics record", t["last_error"])

    def test_record_refuses_unknown_rungs(self):
        with self.assertRaises(ValueError):
            RF.record("snapshot", self.repo, "perf", self.spec)


if __name__ == "__main__":
    unittest.main()


class Round1Test(unittest.TestCase):
    """Codex F.10c round 1."""

    def test_non_finite_metrics_rejected(self):
        for doc in ('{"latency_ms": -Infinity}', '{"x": NaN}'):
            with self.assertRaises(ValueError):
                safety.parse_metrics(doc)
        with self.assertRaises(ValueError):
            safety.parse_baseline('{"metrics": {"a": {"value": Infinity}}}')

    def test_repo_path_containment(self):
        repo = tempfile.mkdtemp(prefix="rp-")
        self.assertTrue(safety.repo_path(repo, ".agent/x.json").endswith(os.path.join(".agent", "x.json")))
        for bad in ("../x", "/etc/passwd", "C:/x", "a/../../x", ""):
            with self.assertRaises(ValueError, msg=bad):
                safety.repo_path(repo, bad)
        os.makedirs(os.path.join(repo, ".agent"))
        with mock.patch.object(safety.os.path, "islink", side_effect=lambda p: p.endswith(".agent")):
            with self.assertRaises(ValueError):
                safety.repo_path(repo, ".agent/snapshots/api.json")

    def test_redaction(self):
        out = safety.redact("token=abcd1234efgh glpat-xxxxxxxxxxxxxx Bearer abcdefghijklmnopqrst normal text")
        self.assertNotIn("abcd1234efgh", out)
        self.assertNotIn("glpat-", out)
        self.assertIn("normal text", out)


class Round1LadderTest(LadderTest):
    def test_stale_output_file_never_passes(self):
        rung = ("    filed:\n      kind: snapshot\n      run:\n        - " + PY + "\n        - scripts/dump.py\n"
                "      output: build/api.json\n      snapshot: .agent/snapshots/filed.json\n      format: json\n"
                "      protects:\n        - scripts/dump.py\n")
        text = open(self.spec).read()
        open(self.spec, "w").write(text.rstrip("\n") + "\n" + rung)
        git(self.repo, "checkout", "-q", "main")
        self.write("scripts/dump.py", "import json, os\nos.makedirs('build', exist_ok=True)\n"
                   "json.dump({'a': 1}, open('build/api.json', 'w'))\n")
        self.write(".gitignore", "__pycache__/\nbuild/\n")
        RF.record("snapshot", self.repo, "filed", self.spec)
        self.commit("filed snapshot")
        git(self.repo, "checkout", "-q", "feat/x")
        git(self.repo, "rebase", "-q", "main")
        self.write("scripts/dump.py", "print('forgot to write the file')\n")        # build/api.json stays
        self.commit("stops writing")
        status, t = self.verify()
        self.assertEqual(status, "blocked")
        self.assertIn("not written by this run", t["last_error"])

    def test_secrets_in_failing_output_are_redacted(self):
        self.write("scripts/api.py", "import sys\nprint('password=hunter2hunter2 glpat-abcdefghijklmnop')\nsys.exit(3)\n")
        self.commit("leaky failure")
        _, t = self.verify()
        self.assertNotIn("hunter2hunter2", t.get("last_error") or "")
        self.assertNotIn("glpat-abcdefghijklmnop", t.get("last_error") or "")

    def test_leading_whitespace_is_content(self):
        exists, text = L._git_blob(self.repo, "main", ".agent/snapshots/api.json")
        self.assertTrue(exists)
        self.assertTrue(text.startswith("{"))
        self.assertEqual(L._git_blob(self.repo, "main", "nope.txt"), (False, ""))
