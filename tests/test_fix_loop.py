#!/usr/bin/env python3
"""Stdlib unittest cho fix_loop — vòng chẩn đoán → sửa → compile → chạy lại.

Phủ:
  1. Bóc nguyên nhân: boot log ('Caused by' chain) + flow_check step đỏ.
  2. Mode CHECKPOINT: fail -> fixer sửa -> DỪNG chờ duyệt diff (1 cycle/lần gọi);
     gọi lại -> compile + retest -> xanh -> ghi bài học vào feedback ledger.
  3. Mode AUTO: fail -> sửa -> compile -> retest NGAY trong một lần gọi.
  4. Give-up sau max-attempts -> status failed + history đầy đủ + note vào run_log.
No pip deps. Runner/fixer/compile/git đều được monkeypatch — không agent/app thật.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from argparse import Namespace

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(
    _HERE, "..", ".claude", "skills", "auto-dev", "tools")))
sys.path.insert(0, os.path.abspath(os.path.join(
    _HERE, "..", ".claude", "skills", "dev-automation", "tools")))

import fix_loop   # noqa: E402
import run_log    # noqa: E402


def ns(**kw):
    return Namespace(**kw)


BOOT_LOG = """\
2026-07-06 INFO  Starting AtaskApp
2026-07-06 ERROR o.s.boot.SpringApplication - Application run failed
java.lang.IllegalStateException: Failed to introspect Class [SearchConfiguration]
Caused by: java.lang.NoClassDefFoundError: com/example/search/service/ElasticSearchService
Caused by: java.lang.ClassNotFoundException: com.example.search.service.ElasticSearchService
APPLICATION FAILED TO START
Description:
A component required a bean that could not be found.
"""

FLOW_FAIL = {
    "passed": False,
    "steps": [
        {"name": "AC1: api ok", "type": "api", "passed": True, "result": {"passed": True}},
        {"name": "AC2: row updated", "type": "db", "passed": False,
         "result": {"passed": False,
                    "checks": [{"check": "value", "expected": "DONE", "actual": "OPEN",
                                "passed": False}],
                    "sample": [["OPEN"]]}},
    ],
}


class DiagnosisTest(unittest.TestCase):
    def test_boot_cause_extracts_caused_by_chain(self):
        out = fix_loop.extract_boot_cause(BOOT_LOG)
        self.assertIn("ClassNotFoundException: com.example.search", out)
        self.assertIn("APPLICATION FAILED TO START", out)
        self.assertIn("Description:", out)

    def test_flow_fail_summarizes_first_red_step(self):
        out = fix_loop.summarize_flow_fail(FLOW_FAIL)
        self.assertIn("AC2: row updated", out)
        self.assertIn("expected", out)
        self.assertIn("OPEN", out)
        self.assertNotIn("AC1", out)


class LoopBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fixloop_")
        self.clone = os.path.join(self.tmp, "clone")
        os.makedirs(self.clone)
        os.environ["WORK_DIR"] = self.tmp
        with open(os.path.join(self.tmp, "projects.json"), "w", encoding="utf-8") as f:
            json.dump({"p1": {"clone_dir": self.clone}}, f)
        self._runs = run_log._runs_dir
        run_log._runs_dir = lambda: self.tmp
        self._orig = (fix_loop._run_test, fix_loop._spawn_fixer,
                      fix_loop._git_diff, fix_loop._compile)
        fix_loop._spawn_fixer = lambda ctx, failure: "root cause X; fixed Y in Foo.java"
        fix_loop._git_diff = lambda d: "M Foo.java\n-old\n+new"
        fix_loop._compile = lambda ctx: {"passed": True}

    def tearDown(self):
        (fix_loop._run_test, fix_loop._spawn_fixer,
         fix_loop._git_diff, fix_loop._compile) = self._orig
        run_log._runs_dir = self._runs
        os.environ.pop("WORK_DIR", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _init_run(self, mode):
        run_log.cmd_init(ns(run_id="r1", task="t", project="p1", type="bugfix",
                            title="x", tier="standard", mode=mode))

    def _args(self, **kw):
        base = dict(run="r1", project="p1", kind="test", env="dev", scenario=None,
                    base_url=None, health_url=None, cwd=None, compile_cmd=None,
                    max_attempts=3, backend="dry-run", model=None,
                    dry_run_text="fix ok")
        base.update(kw)
        return ns(**base)

    def _fail_then_pass(self, fails):
        calls = {"n": 0}

        def runner(ctx):
            calls["n"] += 1
            if calls["n"] <= fails:
                return {"passed": False, "kind": "unit",
                        "context": f"<error_context>NPE lan {calls['n']}</error_context>"}
            return {"passed": True, "result": {"passed": True}}
        fix_loop._run_test = runner
        return calls


class CheckpointModeTest(LoopBase):
    def test_stops_awaiting_approval_then_green_on_reinvoke(self):
        self._init_run("checkpoint")
        self._fail_then_pass(fails=1)
        out = fix_loop.cmd_run(self._args())
        self.assertEqual(out["status"], "awaiting_approval")
        self.assertEqual(out["attempt"], 1)
        self.assertIn("NPE lan 1", out["cause"])
        self.assertIn("+new", out["diff"])
        # người duyệt xong -> gọi lại: compile + retest -> xanh + ghi bài học
        out2 = fix_loop.cmd_run(self._args())
        self.assertEqual(out2["status"], "green")
        self.assertEqual(out2["attempts"], 1)
        ledger = os.path.join(self.tmp, "feedback", "p1.jsonl")
        self.assertTrue(os.path.isfile(ledger))
        with open(ledger, encoding="utf-8") as f:
            rec = json.loads(f.read().splitlines()[0])
        self.assertEqual(rec["stage"], "fix")
        self.assertIn("auto-fix", rec["tags"])

    def test_compile_failure_reported_on_reinvoke(self):
        self._init_run("checkpoint")
        self._fail_then_pass(fails=1)
        fix_loop.cmd_run(self._args())              # -> awaiting_approval
        fix_loop._compile = lambda ctx: {"passed": False, "context": "compile boom"}
        out = fix_loop.cmd_run(self._args())
        self.assertEqual(out["status"], "compile_failed")


class AutoModeTest(LoopBase):
    def test_fixes_and_retests_in_one_invocation(self):
        self._init_run("auto")
        calls = self._fail_then_pass(fails=1)
        out = fix_loop.cmd_run(self._args())
        self.assertEqual(out["status"], "green")
        self.assertEqual(out["attempts"], 1)
        self.assertEqual(calls["n"], 2)             # fail 1 lần + retest xanh, CÙNG lần gọi

    def test_gives_up_after_max_attempts(self):
        self._init_run("auto")
        self._fail_then_pass(fails=99)
        out = fix_loop.cmd_run(self._args(max_attempts=2))
        self.assertEqual(out["status"], "failed")
        self.assertEqual(out["attempts"], 2)
        self.assertEqual(len(out["history"]), 2)
        notes = run_log._read("r1")["notes"]
        self.assertTrue(any("still red after 2" in n for n in notes))

    def test_reset_clears_state(self):
        self._init_run("auto")
        self._fail_then_pass(fails=99)
        fix_loop.cmd_run(self._args(max_attempts=1))
        self.assertTrue(os.path.isfile(fix_loop._state_path("r1")))
        out = fix_loop.cmd_reset(ns(run="r1"))
        self.assertTrue(out["reset"])
        self.assertFalse(os.path.isfile(fix_loop._state_path("r1")))


class CtxValidationTest(LoopBase):
    def test_verify_requires_scenario(self):
        self._init_run("auto")
        out_err = None
        try:
            fix_loop.cmd_run(self._args(kind="verify", base_url="http://localhost:1"))
        except ValueError as e:
            out_err = str(e)
        self.assertIn("verify scenario not found", out_err or "")


if __name__ == "__main__":
    unittest.main()
