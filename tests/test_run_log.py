#!/usr/bin/env python3
"""Stdlib unittest for run_log evidence-gating (Phase 1).

No third-party deps (project rule: stdlib only). Run with:
    python -m unittest discover -s tests
or  python tests/test_run_log.py
"""

import json
import os
import sys
import tempfile
import unittest
from argparse import Namespace

# Make the dev-automation tools importable.
_HERE = os.path.dirname(os.path.abspath(__file__))
_TOOLS = os.path.join(_HERE, "..", ".claude", "skills", "dev-automation", "tools")
sys.path.insert(0, os.path.abspath(_TOOLS))

import run_log  # noqa: E402


def ns(**kw):
    return Namespace(**kw)


class RunLogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="runlog-test-")
        # Redirect run storage to a throwaway dir so tests never touch temp/runs.
        self._orig = run_log._runs_dir
        run_log._runs_dir = lambda: self.tmp

    def tearDown(self):
        run_log._runs_dir = self._orig

    def init(self, rid="r1", tier=None, mode=None, **extra):
        return run_log.cmd_init(ns(run_id=rid, task="1", project="p",
                                   type="bugfix", title="t", tier=tier,
                                   mode=mode, **extra))

    def set_stage(self, rid, stage, status):
        run_log.cmd_stage(ns(run_id=rid, stage=stage, status=status))

    def record(self, rid, gate, verdict, summary=None, json_path=None, kind=None):
        return run_log.cmd_record_gate(ns(run_id=rid, gate=gate, verdict=verdict,
                                          summary=summary, json=json_path, kind=kind))

    def advance(self, rid, stage, force=False):
        return run_log.cmd_advance(ns(run_id=rid, stage=stage, force=force))

    # --- init / defaults ---
    def test_init_defaults(self):
        st = self.init()
        self.assertEqual(st["tier"], "standard")
        self.assertEqual(st["mode"], "checkpoint")
        self.assertEqual(st["gates"], {})
        self.assertEqual(st["acceptance_criteria"], [])

    def test_init_validates_enums(self):
        with self.assertRaises(ValueError):
            self.init(tier="huge")
        with self.assertRaises(ValueError):
            self.init(mode="yolo")

    def test_normalize_old_file(self):
        # Simulate a run file written before evidence-gating existed.
        old = {"run_id": "old", "stages": {s: "pending" for s in run_log.STAGES},
               "checkpoints": {}}
        with open(run_log._path("old"), "w", encoding="utf-8") as f:
            json.dump(old, f)
        st = run_log._read("old")
        self.assertEqual(st["tier"], "standard")
        self.assertEqual(st["mode"], "checkpoint")
        self.assertEqual(st["gates"], {})
        self.assertEqual(st["acceptance_criteria"], [])

    # --- policy: order guard ---
    def test_order_guard_blocks(self):
        self.init(mode="auto")
        # implement not done -> cannot advance test
        d = run_log.policy(run_log._read("r1"), "test")
        self.assertFalse(d["allowed"])
        self.assertIn("prior stage", d["reason"])

    # --- policy: auto mode blocks on missing/failed gates ---
    def test_auto_blocks_missing(self):
        self.init(mode="auto")
        self.set_stage("r1", "plan", "done")
        self.set_stage("r1", "implement", "done")
        res = self.advance("r1", "test")
        self.assertFalse(res["allowed"])
        self.assertFalse(res["written"])
        self.assertEqual(run_log._read("r1")["stages"]["test"], "pending")
        self.assertTrue(any(m.startswith("test:") for m in res["missing"]))
        self.assertTrue(any(m.startswith("lint:") for m in res["missing"]))

    def test_auto_passes_when_satisfied(self):
        self.init(mode="auto")
        self.set_stage("r1", "plan", "done")
        self.set_stage("r1", "implement", "done")
        self.record("r1", "test", "pass")
        self.record("r1", "lint", "pass")
        res = self.advance("r1", "test")
        self.assertTrue(res["allowed"])
        self.assertTrue(res["written"])
        self.assertEqual(run_log._read("r1")["stages"]["test"], "done")

    def test_lint_waivable(self):
        self.init(mode="auto")
        self.set_stage("r1", "plan", "done")
        self.set_stage("r1", "implement", "done")
        self.record("r1", "test", "pass")
        self.record("r1", "lint", "waived", summary="no linter")
        res = self.advance("r1", "test")
        self.assertTrue(res["written"])

    # --- checkpoint provenance: an agent may not approve its own irreversible step ---
    def cp(self, rid, name, status, source=None, proof=None):
        return run_log.cmd_checkpoint(ns(run_id=rid, name=name, status=status,
                                         source=source, proof=proof))

    def _ready_for_deliver(self, rid="r1", mode="checkpoint"):
        self.init(rid, mode=mode)
        for st in ("plan", "implement", "test"):
            self.set_stage(rid, st, "done")
        self.record(rid, "review", "pass")
        run_log.cmd_ac_add(ns(run_id=rid, text="AC1 works", id=None))
        run_log.cmd_ac_map(ns(run_id=rid, id="AC1", evidence="manual", verify_json=None))

    def test_checkpoint_records_provenance(self):
        self.init()
        st = self.cp("r1", "before_mr", "approved", source="telegram", proof="p.txt")
        rec = st["checkpoints"]["before_mr"]
        self.assertEqual(rec["status"], "approved")
        self.assertEqual(rec["source"], "telegram")
        self.assertEqual(rec["proof"], "p.txt")
        self.assertTrue(rec["at"])

    def test_checkpoint_defaults_to_untrusted_agent_source(self):
        self.init()
        st = self.cp("r1", "before_mr", "approved")
        self.assertEqual(st["checkpoints"]["before_mr"]["source"], "agent")

    def test_deliver_blocked_on_agent_self_approval(self):
        self._ready_for_deliver()
        self.cp("r1", "before_mr", "approved")            # agent approves itself
        res = self.advance("r1", "deliver")
        self.assertFalse(res["allowed"])
        self.assertFalse(res["written"])
        self.assertTrue(any(m.startswith("checkpoint:before_mr") for m in res["missing"]))

    def test_deliver_allowed_on_trusted_approval(self):
        self._ready_for_deliver()
        self.cp("r1", "before_mr", "approved", source="telegram")
        res = self.advance("r1", "deliver")
        self.assertTrue(res["allowed"], res["reason"])
        self.assertTrue(res["written"])

    def test_deliver_blocked_when_checkpoint_missing(self):
        self._ready_for_deliver()
        res = self.advance("r1", "deliver")
        self.assertFalse(res["allowed"])

    def test_auto_mode_has_no_human_so_checkpoint_not_required(self):
        self._ready_for_deliver(mode="auto")
        res = self.advance("r1", "deliver")
        self.assertTrue(res["allowed"], res["reason"])

    def test_legacy_string_checkpoint_is_not_trusted(self):
        self._ready_for_deliver()
        path = os.path.join(self.tmp, "r1.json")
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        raw["checkpoints"]["before_mr"] = "approved"       # pre-provenance format
        with open(path, "w", encoding="utf-8") as f:
            json.dump(raw, f)
        st = run_log._read("r1")
        self.assertEqual(st["checkpoints"]["before_mr"]["source"], "legacy")
        self.assertFalse(self.advance("r1", "deliver")["allowed"])

    def test_legacy_source_cannot_be_recorded(self):
        self.init()
        with self.assertRaises(ValueError):
            self.cp("r1", "before_mr", "approved", source="legacy")

    # --- list: artifacts in the runs dir must not crash the listing ---
    def test_list_skips_non_run_artifacts(self):
        self.init("r1")
        with open(os.path.join(self.tmp, "r1_answers.json"), "w", encoding="utf-8") as f:
            json.dump([{"q": "?", "answer": "!"}], f)          # a list artifact
        with open(os.path.join(self.tmp, "r1_verify.json"), "w", encoding="utf-8") as f:
            json.dump({"steps": [], "passed": True}, f)        # dict, but no run_id
        with open(os.path.join(self.tmp, "broken.json"), "w", encoding="utf-8") as f:
            f.write("{not json")
        rows = run_log.cmd_list(ns(open=False))
        self.assertEqual([r["run_id"] for r in rows], ["r1"])

    # --- policy: checkpoint mode informs but does not block ---
    def test_checkpoint_informs(self):
        self.init(mode="checkpoint")
        self.set_stage("r1", "plan", "done")
        self.set_stage("r1", "implement", "done")
        res = self.advance("r1", "test")  # no gates recorded
        self.assertTrue(res["allowed"])
        self.assertTrue(res["written"])
        self.assertTrue(res["missing"])  # surfaced for the human
        self.assertEqual(run_log._read("r1")["stages"]["test"], "done")

    # --- advisory gates never block ---
    def test_advisory_does_not_block(self):
        self.init(mode="auto")
        self.set_stage("r1", "plan", "done")
        self.set_stage("r1", "implement", "done")
        self.record("r1", "test", "pass")
        self.record("r1", "lint", "pass")
        self.record("r1", "build", "fail")  # advisory
        res = self.advance("r1", "test")
        self.assertTrue(res["written"])
        self.assertTrue(any(a.startswith("build:") for a in res["advisory"]))

    # --- force ---
    def test_force_writes_and_audits(self):
        self.init(mode="auto")
        self.set_stage("r1", "plan", "done")
        self.set_stage("r1", "implement", "done")
        res = self.advance("r1", "test", force=True)
        self.assertFalse(res["allowed"])   # policy still denied
        self.assertTrue(res["forced"])
        self.assertTrue(res["written"])
        self.assertEqual(run_log._read("r1")["stages"]["test"], "done")
        self.assertTrue(any("forced" in n for n in run_log._read("r1")["notes"]))

    # --- record-gate from a runner JSON file ---
    def test_record_gate_json_summary_and_warning(self):
        self.init()
        result = {"passed": False, "kind": "test", "exit_code": 1,
                  "summary": "FAIL (exit 1)"}
        jp = os.path.join(self.tmp, "result.json")
        with open(jp, "w", encoding="utf-8") as f:
            json.dump(result, f)
        out = self.record("r1", "test", "pass", json_path=jp)  # verdict disagrees
        self.assertEqual(out["summary"], "FAIL (exit 1)")
        self.assertEqual(out["kind"], "test")
        self.assertIn("warning", out)

    # --- AC ledger + derived ac gate ---
    def test_ac_ledger_and_deliver_gate(self):
        self.init(mode="auto")
        for s in ("plan", "implement", "test"):
            self.set_stage("r1", s, "done")
        self.record("r1", "review", "pass")
        run_log.cmd_ac_add(ns(run_id="r1", text="GET /x returns 200", id=None))
        # ac open -> deliver blocked in auto
        res = self.advance("r1", "deliver")
        self.assertFalse(res["written"])
        self.assertTrue(any(m.startswith("ac:") for m in res["missing"]))
        # map it -> deliver allowed
        run_log.cmd_ac_map(ns(run_id="r1", id="AC1", evidence="probe_api ok"))
        res = self.advance("r1", "deliver")
        self.assertTrue(res["written"])

    def test_empty_ac_ledger_passes(self):
        self.init(mode="auto")
        for s in ("plan", "implement", "test"):
            self.set_stage("r1", s, "done")
        self.record("r1", "review", "pass")
        res = self.advance("r1", "deliver")  # no AC recorded
        self.assertTrue(res["written"])

    def test_ac_waive(self):
        self.init()
        run_log.cmd_ac_add(ns(run_id="r1", text="legacy edge", id="AC9"))
        run_log.cmd_ac_waive(ns(run_id="r1", id="AC9", note="out of scope"))
        st = run_log._read("r1")
        self.assertEqual(st["acceptance_criteria"][0]["status"], "waived")
        self.assertTrue(any("AC9 waived" in n for n in st["notes"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
