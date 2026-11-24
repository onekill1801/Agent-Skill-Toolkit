"""Coverage manifest (mr-review/tools/review_coverage.py, temp/codex/ocr-eval/FINAL.md step 1): the machine lists
every file of the pinned diff, the review must account for each one, a gap needs a human, and the manifest is part
of what a human approves."""

import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(_ROOT, ".claude", "skills", "mr-review", "tools"))

import review_common as rc  # noqa: E402
import review_coverage as C  # noqa: E402
import review_post as rp  # noqa: E402
import review_rounds as R  # noqa: E402
import review_rules as rr  # noqa: E402

H2 = "2" * 40


def _write(path, data):
    with open(path, "wb") as f:
        f.write(data if isinstance(data, bytes) else data.encode("utf-8"))


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                          encoding="utf-8").stdout


class BuildTest(unittest.TestCase):
    """A real repo: modify, add, delete, rename, binary, lock file, non-ASCII name."""

    @classmethod
    def setUpClass(cls):
        cls.repo = tempfile.mkdtemp(prefix="cov-repo-")
        g = lambda *a: _git(cls.repo, *a)  # noqa: E731
        g("init", "-q")
        g("config", "user.email", "t@t")
        g("config", "user.name", "t")
        w = lambda p, t: _write(os.path.join(cls.repo, p), t)  # noqa: E731
        os.makedirs(os.path.join(cls.repo, "src"))
        w("src/A.java", "class A {}\n")
        w("src/Gone.java", "class Gone {}\n")
        w("src/Old.java", "class Old {\n  int a;\n  int b;\n  int c;\n}\n")
        g("add", "-A")
        g("commit", "-qm", "base")
        cls.base = g("rev-parse", "HEAD").strip()
        w("src/A.java", "class A { int x; }\n")
        w("src/New.java", "class New {}\n")
        w("package-lock.json", "{}\n")
        w("src/Tên.java", "class T {}\n")
        w("logo.png", b"\x89PNG\x00\x01\x02")
        os.remove(os.path.join(cls.repo, "src", "Gone.java"))
        g("mv", "src/Old.java", "src/Renamed.java")
        g("add", "-A")
        g("commit", "-qm", "head")
        cls.head = g("rev-parse", "HEAD").strip()
        cls.m = C.build(lambda *a: _git(cls.repo, *a), cls.base, cls.head)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.repo, ignore_errors=True)

    def by(self, path):
        return next(f for f in self.m["files"] if f["path"] == path)

    def test_every_file_is_listed_with_its_status(self):
        self.assertEqual({f["path"] for f in self.m["files"]},
                         {"src/A.java", "src/New.java", "package-lock.json", "src/Tên.java", "logo.png",
                          "src/Gone.java", "src/Renamed.java"})
        self.assertEqual(self.by("src/A.java")["status"], "M")
        self.assertEqual(self.by("src/Gone.java")["status"], "D")
        self.assertEqual(self.by("src/Renamed.java")["old_path"], "src/Old.java")
        self.assertEqual(self.by("src/A.java")["added"], 1)

    def test_machine_exclusions(self):
        self.assertEqual(self.by("logo.png")["excluded"], "binary")
        self.assertIn("package-lock.json", self.by("package-lock.json")["excluded"])
        self.assertIsNone(self.by("src/A.java")["excluded"])
        with mock.patch.dict(os.environ, {"REVIEW_COVERAGE_EXCLUDE": "src/New.*"}):
            self.assertTrue(C.excluded_reason("src/New.java", False))

    def test_entries_carry_full_modes_and_blobs(self):
        ident = self.by("src/A.java")["id"]
        self.assertEqual(len(ident), 4)
        self.assertTrue(all(len(b) == 40 for b in ident[2:]))

    def test_diff_of_a_renamed_file_by_its_new_path_shows_the_rename(self):
        """review_ws.diff --path NEW passes the old path too: alone, git shows a rename as an add (R10 #2)."""
        import review_ws
        job = {"id": "77-9", "project_id": "77", "worktree": os.path.join(self.repo, "wt"),
               "diff_refs": {"base_sha": self.base, "head_sha": self.head}, "coverage_sha": "x"}
        with open(C.path_of(job["worktree"]), "w", encoding="utf-8") as f:
            json.dump(self.m, f)
        job["coverage_sha"] = C.file_sha(C.path_of(job["worktree"]))
        repo = review_ws.Repo(self.repo, bare=False, use_token=False, kind="test")
        with mock.patch.object(rc, "load_job", return_value=job), \
                mock.patch.object(review_ws, "open_repo", return_value=repo):
            out = review_ws.diff("77-9", path="src/Renamed.java")
        self.assertIn("rename from src/Old.java", out["diff"])
        self.assertNotIn("new file mode", out["diff"])

    def test_file_key_on_real_git_add_then_rename(self):
        """Round 1 adds B next to A; round 2 also deletes A, so git sees A → B: same path B, other diff."""
        d = tempfile.mkdtemp(prefix="cov-ren-")
        self.addCleanup(shutil.rmtree, d, True)
        g = lambda *a: _git(d, *a)  # noqa: E731
        g("init", "-q")
        g("config", "user.email", "t@t")
        g("config", "user.name", "t")
        body = "".join(f"line {i}\n" for i in range(30))
        _write(os.path.join(d, "A.java"), body)
        _write(os.path.join(d, "C.java"), "c\n")
        g("add", "-A")
        g("commit", "-qm", "base")
        base = g("rev-parse", "HEAD").strip()
        _write(os.path.join(d, "B.java"), body)
        _write(os.path.join(d, "C.java"), "c2\n")
        g("add", "-A")
        g("commit", "-qm", "r1")
        r1 = C.build(lambda *a: _git(d, *a), base, g("rev-parse", "HEAD").strip())
        os.remove(os.path.join(d, "A.java"))
        g("add", "-A")
        g("commit", "-qm", "r2")
        r2 = C.build(lambda *a: _git(d, *a), base, g("rev-parse", "HEAD").strip())
        key = lambda m, p: C.file_key(next(f for f in m["files"] if f["path"] == p))  # noqa: E731
        self.assertEqual(key(r1, "C.java"), key(r2, "C.java"))      # untouched since round 1: reusable
        self.assertNotEqual(key(r1, "B.java"), key(r2, "B.java"))   # added → renamed: must be reviewed again

    def test_section_sizes_pair_with_files(self):
        self.assertTrue(all(isinstance(f["diff_lines"], int) and f["diff_lines"] > 0 for f in self.m["files"]))
        self.assertEqual(self.m["start_sha"], None)

    def test_sections_that_do_not_spell_the_paths_are_not_paired(self):
        entries = [{"path": "a b/c", "old_path": None}, {"path": "d", "old_path": None}]
        good = [("diff --git a/a b/c b/a b/c", 5), ("diff --git a/d b/d", 3)]
        self.assertEqual(C.pair_sizes(entries, good), [5, 3])
        self.assertEqual(C.pair_sizes(entries, [good[1], good[0]]), [None, None])     # order differs
        self.assertEqual(C.pair_sizes(entries, good[:1]), [None, None])               # count differs
        self.assertEqual(C.pair_sizes(entries, [('diff --git "a/a\\tb" "b/a\\tb"', 5), good[1]]), [None, None])

    def test_names_differing_only_by_cr_and_lf_stay_two_files(self):
        """POSIX allows "\\r" / "\\n" in names; the text-mode git() would fold them into one path (R12 #1)."""
        meta = ":100644 100644 " + "a" * 40 + " " + "b" * 40 + " M"
        raw = f"{meta}\x00x\ry\x00{meta}\x00x\ny\x00"
        num = "1\t0\tx\ry\x001\t0\tx\ny\x00"

        def exact(*a):
            return raw if "--raw" in a else num if "--numstat" in a else ""
        m = C.build(lambda *a: "", "b", "h", exact=exact)
        self.assertEqual(sorted(f["path"] for f in m["files"]), ["x\ny", "x\ry"])
        dup = {"files": [{"path": "x", "status": "M"}, {"path": "x", "status": "M"}]}
        errs = C.check({"coverage": [{"path": "x", "status": "reviewed"}]}, dup, None)["errors"]
        self.assertIn("same path twice", errs[0])

    def test_source_renamed_to_an_excluded_name_is_still_reviewed(self):
        """src/Parser.java → package-lock.json with edits must not leave the manifest (R14 #1)."""
        meta = ":100644 100644 " + "a" * 40 + " " + "b" * 40 + " R090"
        raw = f"{meta}\x00src/Parser.java\x00package-lock.json\x00{meta}\x00a/yarn.lock\x00b/yarn.lock\x00"
        num = "3\t1\t\x00src/Parser.java\x00package-lock.json\x002\t0\t\x00a/yarn.lock\x00b/yarn.lock\x00"

        def exact(*a):
            return raw if "--raw" in a else num if "--numstat" in a else ""
        m = C.build(lambda *a: "", "b", "h", exact=exact)
        by = {f["path"]: f for f in m["files"]}
        self.assertIsNone(by["package-lock.json"]["excluded"])
        self.assertTrue(by["b/yarn.lock"]["excluded"])                    # lock → lock: still excluded

    def test_backslash_is_part_of_a_posix_name_not_a_separator(self):
        self.assertFalse(C.excluded_reason("src\\package-lock.json", False))          # R13 #2
        self.assertTrue(C.excluded_reason("src/package-lock.json", False))

    def test_non_utf8_names_never_break_a_write(self):
        """A name git gives as non-UTF-8 bytes (surrogateescape) — R13 #1."""
        name = "bad" + chr(0xDCFF) + ".java"
        d = tempfile.mkdtemp(prefix="cov-nonutf8-")
        self.addCleanup(shutil.rmtree, d, True)
        wt = os.path.join(d, "wt")
        with mock.patch.dict(os.environ, {"REVIEW_COVERAGE": "on"}):
            C.record_served(wt, name, ["diff --git a/x b/x", "+1"], 800)
        m = {"files": [{"path": name, "status": "M", "id": _id("x")}, {"path": "ok.java", "status": "M",
                                                                     "id": _id("y")}]}
        mp = os.path.join(d, "m.json")
        with open(mp, "w", encoding="utf-8") as f:
            json.dump(m, f, ensure_ascii=True)
        out = R._reuse({"coverage": [{"path": name, "status": "reviewed"}, {"path": "ok.java", "status": "reviewed"}]},
                       mp, C.file_sha(mp))
        self.assertEqual(list(out["prev_files"]), ["ok.java"])                 # never reused: reviewed again
        self.assertEqual(list(out["prev_coverage"]), ["ok.java"])
        json.dumps(out, ensure_ascii=False).encode("utf-8")                    # prev.json writes

    def test_git_exact_keeps_bytes(self):
        import review_ws
        repo = review_ws.Repo(self.repo, bare=False, use_token=False, kind="test")
        out = repo.git_exact("diff", "--raw", "-z", "--no-abbrev", self.base, self.head)
        self.assertIn("\x00", out)
        self.assertEqual(out, subprocess.run(["git", "-C", self.repo, "diff", "--raw", "-z", "--no-abbrev",
                                              self.base, self.head], capture_output=True).stdout.decode("utf-8"))

    def test_numstat_path_with_a_tab(self):
        self.assertEqual(C.parse_numstat("1\t2\ta\tb.java\x00"), {"a\tb.java": (1, 2)})
        self.assertEqual(C.parse_numstat("3\t0\t\x00old.java\x00new.java\x00-\t-\tlogo.png\x00"),
                         {"new.java": (3, 0), "logo.png": (None, None)})

    def test_served_record_full_cut_and_boundary(self):
        text = ["diff --git a/A b/A", "+1", "+2", "diff --git a/B b/B", "+3"]
        self.assertEqual(C.served_record("", text, 100), {"path": None, "whole": 2, "cut": False})
        self.assertEqual(C.served_record("", text, 2), {"path": None, "whole": 0, "cut": True})
        self.assertEqual(C.served_record("", text, 3), {"path": None, "whole": 1, "cut": True})  # cut on boundary
        self.assertEqual(C.served_record("A", text, 3), {"path": "A", "full": False})


def _id(p, v="1"):
    return ["100644", "100644", "0" * 40, (v + p.encode().hex() + "0" * 40)[:40]]


def manifest(*paths, excluded=()):
    return {"version": 1, "base_sha": "b", "head_sha": H2,
            "files": [{"path": p, "status": "M", "old_path": None, "id": _id(p),
                       "excluded": "binary" if p in excluded else None} for p in paths]}


def prev_of(m, coverage):
    """prev.json "reuse" as review_rounds._reuse builds it from the previous round's manifest + review."""
    return {"reuse": {"prev_files": {f["path"]: C.file_key(f) for f in m["files"]},
                      "prev_coverage": coverage}}


class CheckTest(unittest.TestCase):
    M = manifest("A.java", "B.java", "x.png", excluded=("x.png",))

    def test_every_non_excluded_file_needs_one_entry(self):
        res = C.check({"coverage": [{"path": "A.java", "status": "reviewed"}]}, self.M, None)
        self.assertTrue(any("B.java missing" in e for e in res["errors"]))
        self.assertFalse(any("x.png" in e for e in res["errors"]))
        ok = C.check({"coverage": [{"path": "A.java", "status": "reviewed"},
                                   {"path": "B.java", "status": "reviewed"}]}, self.M, None)
        self.assertEqual(ok, {"errors": [], "gaps": []})

    def test_malformed_input_never_raises(self):
        for cov in (42, "x", [42], [{"path": []}], [{"path": "A.java", "status": "skipped", "reason": 42}],
                    [{"path": "", "status": "reviewed"}]):
            res = C.check({"coverage": cov}, self.M, None)
            self.assertTrue(res["errors"], cov)
            C.fingerprint({"coverage_sha": "x"}, {"coverage": cov})
            C.unverified({"coverage": cov}, "", self.M)
        self.assertTrue(C.check("not a dict", self.M, None)["errors"])

    def test_missing_list_unknown_duplicate_bad_status(self):
        self.assertTrue(C.check({}, self.M, None)["errors"])
        errs = " | ".join(C.check({"coverage": [{"path": "Z.java", "status": "reviewed"},
                                                {"path": "A.java", "status": "done"},
                                                {"path": "B.java", "status": "reviewed"},
                                                {"path": "B.java", "status": "reviewed"}]}, self.M, None)["errors"])
        self.assertIn("Z.java is not a file", errs)
        self.assertIn("status must be one of", errs)
        self.assertIn("given twice", errs)

    def test_gaps_need_a_reason_and_are_reported(self):
        res = C.check({"coverage": [{"path": "A.java", "status": "skipped"},
                                    {"path": "B.java", "status": "truncated", "reason": "file 4k dòng"}]},
                      self.M, None)
        self.assertTrue(any("A.java" in e and "needs a reason" in e for e in res["errors"]))
        self.assertEqual([g["path"] for g in res["gaps"]], ["A.java", "B.java"])

    COV = {"coverage": [{"path": "A.java", "status": "reused"}, {"path": "B.java", "status": "reused"}]}
    OK = {"A.java": "reviewed", "B.java": "reused"}

    def test_reused_needs_the_same_per_file_diff_as_a_reviewed_previous_round(self):
        self.assertTrue(any("only on a re-review" in e for e in C.check(self.COV, self.M, None)["errors"]))
        self.assertEqual(C.check(self.COV, self.M, prev_of(self.M, self.OK))["errors"], [])
        # previous round without an archived manifest (older ledger entry): no reuse
        self.assertEqual(len(C.check(self.COV, self.M, {"reuse": None})["errors"]), 2)

    def test_any_change_of_the_files_diff_refuses_reuse(self):
        old = manifest("A.java", "B.java")
        for change in ({"id": _id("A.java", "9")},                          # content (head or base side)
                       {"id": ["100755"] + _id("A.java")[1:]},               # mode
                       {"status": "R", "old_path": "Z.java"},                # added → renamed (Codex step1 R3 #2)
                       {"status": "A"}):
            now = {**self.M, "files": [{**self.M["files"][0], **change}, *self.M["files"][1:]]}
            errs = C.check(self.COV, now, prev_of(old, self.OK))["errors"]
            self.assertEqual(errs, ["coverage[A.java]: reused but its diff is not the one reviewed in the previous "
                                    "round"], change)

    def test_a_gap_of_the_previous_round_is_never_reused(self):
        errs = C.check(self.COV, self.M, prev_of(self.M, {"A.java": "reviewed", "B.java": "skipped"}))["errors"]
        self.assertTrue(errs and "did not review it (skipped)" in errs[0])
        self.assertIn("no entry", C.check(self.COV, self.M, prev_of(self.M, {"A.java": "reviewed"}))["errors"][0])


class JobTest(unittest.TestCase):
    """validate / approval hash / auto-post rules on a prepared job."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cov-job-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.wt = os.path.join(self.tmp, "5@22222222")
        with open(C.path_of(self.wt), "w", encoding="utf-8") as f:
            json.dump(manifest("A.java", "B.java"), f)
        self.job = {"id": "77-5", "iid": 5, "project_id": "77", "head_sha": H2, "diff_refs": {"head_sha": H2},
                    "worktree": self.wt, "title": "t", "source_branch": "f", "target_branch": "develop",
                    "coverage_sha": C.file_sha(C.path_of(self.wt))}
        self.full = {"head_sha": H2, "verdict": "COMMENT", "summary": "ok", "findings": [],
                     "coverage": [{"path": "A.java", "status": "reviewed"}, {"path": "B.java", "status": "reviewed"}]}
        self.gap = {**self.full, "coverage": [{"path": "A.java", "status": "reviewed"},
                                              {"path": "B.java", "status": "skipped", "reason": "hết thời gian"}]}

    @contextlib.contextmanager
    def env(self, mode, **kw):
        """The flag AND a job prepared under it (the mode is pinned on the job at prepare)."""
        with mock.patch.dict(os.environ, {"REVIEW_COVERAGE": mode, "REVIEW_AUTO_POST": "auto",
                                          "REVIEW_AUTO_POST_PROJECTS": "*", **kw}):
            if mode != "off":
                self.job["coverage_mode"] = mode
            yield

    def test_off_changes_nothing(self):
        old = {**self.job, "coverage_sha": None}                 # prepared with the flag off: no manifest
        with self.env("off"):
            self.assertEqual(rp.validate(old, {**self.full, "coverage": None}), [])
            self.assertEqual(C.fingerprint(old, self.full), "")
            self.assertFalse(C.evaluate(old, self.gap)["active"])

    def test_on_requires_the_list_shadow_only_reports(self):
        bad = {**self.full, "coverage": [{"path": "A.java", "status": "reviewed"}]}
        with self.env("on"):
            self.assertTrue(any("B.java missing" in e for e in rp.validate(self.job, bad)))
            self.assertEqual(rp.validate(self.job, self.full), [])
        with self.env("shadow"):
            self.assertEqual(rp.validate(self.job, bad), [])
            self.assertTrue(C.evaluate(self.job, bad)["errors"])

    def test_manifest_edited_after_prepare_is_an_error(self):
        with open(C.path_of(self.wt), "w", encoding="utf-8") as f:
            json.dump(manifest("A.java"), f)
        with self.env("on"):
            self.assertTrue(any("changed after prepare" in e for e in rp.validate(self.job, self.full)))

    def test_a_review_keeps_the_mode_it_was_prepared_with(self):
        """Flipping the flag after prepare changes nothing for that review (Codex step1 R3–R9): validate, the
        rules and approval_status see the pinned mode; the new mode applies from the next prepare."""
        bad = {**self.full, "coverage": [{"path": "A.java", "status": "reviewed"}]}
        shadow_job, on_job = {**self.job, "coverage_mode": "shadow"}, {**self.job, "coverage_mode": "on"}
        note, inline = rp.payload(shadow_job, self.gap)
        for flag in ("off", "shadow", "on"):
            with mock.patch.dict(os.environ, {"REVIEW_COVERAGE": flag, "REVIEW_AUTO_POST": "auto",
                                              "REVIEW_AUTO_POST_PROJECTS": "*"}):
                self.assertEqual(rp.validate(shadow_job, bad), [], flag)
                self.assertTrue(rr.evaluate(shadow_job, note, inline, self.gap)["auto_post"], flag)
                self.assertTrue(rp.validate(on_job, bad), flag)
                self.assertFalse(rr.evaluate(on_job, note, inline, self.gap)["auto_post"], flag)
                old = {**self.job, "coverage_sha": None}          # prepared with the flag off: never gated
                self.assertEqual(rp.validate(old, {**self.full, "coverage": None}), [], flag)

    def test_flipping_the_flag_never_voids_a_human_approval(self):
        """The hash depends on the manifest + statuses, not on the flag (Codex step1 R8 #3): a human approval
        survives off/shadow/on; a job without a manifest hashes exactly as before this feature."""
        for job in ({**self.job, "coverage_mode": "shadow"}, {**self.job, "coverage_sha": None}):
            hashes = set()
            for m in ("off", "shadow", "on"):                 # the FLAG only: the job is not prepared again
                with mock.patch.dict(os.environ, {"REVIEW_COVERAGE": m}):
                    hashes.add(rp.payload_hash(job, self.gap))
            self.assertEqual(len(hashes), 1)

    def test_a_re_prepare_under_another_mode_re_asks(self):
        """Same manifest, prepared again under on: a rules approval taken under shadow is not reused (R10 #1)."""
        self.assertNotEqual(rp.payload_hash({**self.job, "coverage_mode": "shadow"}, self.gap),
                            rp.payload_hash({**self.job, "coverage_mode": "on"}, self.gap))

    def test_agent_junk_never_crashes_the_hash_or_the_served_log(self):
        r = {**self.full, "coverage": [{"path": "A.java", "status": "reviewed"},
                                       {"path": "B.java", "status": "skipped", "reason": "x\ud800"}]}
        with open(C.served_path(self.wt), "wb") as f:
            f.write(b'{"path": "A.java", "full": true}\n\xff\xfe broken\n')
        with self.env("on"):
            self.assertTrue(rp.payload_hash(self.job, r))
            self.assertEqual(C.evaluate(self.job, r)["unverified"], [])     # A served full, B is a gap
            # what reaches a saved record (approval.rules.coverage) encodes as UTF-8 (R11 #3)
            bad = {**r, "coverage": [*r["coverage"], {"path": "Z" + chr(0xDC80), "status": "reviewed"}]}
            note, inline = rp.payload(self.job, r)
            res = rr.evaluate(self.job, note, inline, bad)
            json.dumps(res, ensure_ascii=False).encode("utf-8")

    def test_empty_path_diff_is_not_full(self):
        self.assertEqual(C.served_record("Typo.java", [], 800), {"path": "Typo.java", "full": False})

    def test_gap_blocks_auto_post_only_when_on(self):
        with self.env("on"):
            note, inline = rp.payload(self.job, self.gap)
            res = rr.evaluate(self.job, note, inline, self.gap)
            self.assertFalse(res["auto_post"])
            self.assertTrue(any("B.java skipped" in r for r in res["reasons"]))
            self.assertTrue(rr.evaluate(self.job, note, inline, self.full)["auto_post"])
        with self.env("shadow"):
            res = rr.evaluate(self.job, note, inline, self.gap)
            self.assertTrue(res["auto_post"])
            self.assertEqual(res["coverage"]["gaps"][0]["path"], "B.java")

    def test_approval_hash_changes_with_scope_not_only_text(self):
        with self.env("on"):
            h_full, h_gap = rp.payload_hash(self.job, self.full), rp.payload_hash(self.job, self.gap)
            self.assertEqual(rp.render_note(self.job, self.full, set()), rp.render_note(self.job, self.gap, set()))
            self.assertNotEqual(h_full, h_gap)
        with self.env("off"):        # a job without a manifest: same hash as before this feature
            old = {**self.job, "coverage_sha": None}
            self.assertEqual(rp.payload_hash(old, self.full), rp.payload_hash(old, self.gap))

    def test_preview_shows_the_gap(self):
        with self.env("on"):
            self.assertIn("B.java (skipped)", rp._preview({**self.job, "project_path": "g/p"}, self.gap))

    def test_reviewed_files_never_served_are_unverified_signal(self):
        with self.env("on"):
            C.record_served(self.wt, "A.java", ["diff --git a/A.java b/A.java", "+x"], 800)
            cov = C.evaluate(self.job, self.full)
            self.assertEqual(cov["unverified"], [{"path": "B.java", "served": "unknown"}])
            self.assertEqual(rp.validate(self.job, self.full), [])      # a signal, never an error

    def test_served_log_maps_a_whole_diff_by_manifest_order(self):
        m = {"files": [{"path": "A.java", "diff_lines": 2}, {"path": "B.java", "diff_lines": 2}]}
        with self.env("on"):
            C.record_served(self.wt, "", ["diff --git a/A.java b/A.java", "+x", "diff --git a/B.java b/B.java",
                                          "+y"], 3)
            self.assertEqual(C.served_signal(self.wt, m), {"A.java": "full", "B.java": "cut"})
            with open(C.served_path(self.wt), "a", encoding="utf-8") as f:
                f.write("[]\n{bad\n42\n")                       # agent-written junk: ignored, never a crash
            self.assertEqual(C.served_signal(self.wt, m), {"A.java": "full", "B.java": "cut"})
            unpaired = {"files": [{"path": "A.java", "diff_lines": None}, {"path": "B.java", "diff_lines": 2}]}
            self.assertEqual(C.served_signal(self.wt, unpaired), {})

    def test_reused_reads_prev_json(self):
        prev_m = manifest("A.java", "B.java")
        prev_m["files"][0]["id"] = _id("A.java", "9")            # A changed since, B did not
        with open(R.prev_path(self.wt), "w", encoding="utf-8") as f:
            json.dump({"round": 2, "previous": {"head_sha": "1" * 40, "round": 1}, "open": [], "replies": [],
                       "delta": {"A.java": [[1, 1]]}, **prev_of(prev_m, {"A.java": "reviewed", "B.java": "reviewed"})},
                      f)
        r = {**self.full, "previous_findings": [],
             "coverage": [{"path": "A.java", "status": "reviewed"}, {"path": "B.java", "status": "reused"}]}
        with self.env("on"):
            job = {**self.job, "prev_json_sha": R.file_sha(R.prev_path(self.wt))}
            self.assertEqual(rp.validate(job, r), [])
            r["coverage"][0]["status"] = "reused"
            self.assertTrue(any("A.java" in e and "not the one reviewed" in e for e in rp.validate(job, r)))


class ReuseFactsTest(unittest.TestCase):
    """review_rounds: a round's manifest is archived with it; the next prev.json carries it as the reuse proof."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cov-reuse-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_reuse_block_from_the_archived_manifest(self):
        path = os.path.join(self.tmp, "m.json")
        m = manifest("A.java", "B.java")
        m["files"].append({"path": "old.java", "status": "M"})                 # no id (older format): skipped
        with open(path, "w", encoding="utf-8") as f:
            json.dump(m, f)
        review = {"coverage": [{"path": "A.java", "status": "reviewed"}, {"path": "B.java", "status": "skipped"},
                               42, {"path": []}]}
        archived_sha = C.file_sha(path)
        out = R._reuse(review, path, archived_sha)
        self.assertEqual(out["prev_files"], {p: C.file_key(f) for p, f in
                                             (("A.java", m["files"][0]), ("B.java", m["files"][1]))})
        self.assertEqual(out["prev_coverage"], {"A.java": "reviewed", "B.java": "skipped"})
        self.assertIsNone(R._reuse(review, ""))                                  # older ledger entry
        self.assertIsNone(R._reuse(review, os.path.join(self.tmp, "gone.json"), "x"))
        self.assertIsNone(R._reuse(review, path, ""))                            # no recorded hash
        with open(path, "a", encoding="utf-8") as f:
            f.write(" ")                                                     # edited since it was archived
        self.assertIsNone(R._reuse(review, path, archived_sha))

    def test_load_sha_hashes_the_bytes_it_parses(self):
        path = os.path.join(self.tmp, "x.json")
        _write(path, b'{"a": 1}')
        obj, sha = R._load_sha(path)
        self.assertEqual((obj, sha), ({"a": 1}, C.file_sha(path)))
        self.assertEqual(R._load_sha(os.path.join(self.tmp, "none.json")), (None, ""))

    def test_only_a_checked_coverage_is_inherited(self):
        path = os.path.join(self.tmp, "m.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(manifest("A.java"), f)
        rv = os.path.join(self.tmp, "r.json")
        with open(rv, "w", encoding="utf-8") as f:
            json.dump({"head_sha": "1" * 40, "verdict": "COMMENT", "findings": [],
                       "coverage": [{"path": "A.java", "status": "reused"}]}, f)

        def git(*a):
            return None                                   # previous head unreachable: irrelevant to reuse

        prev = {"status": "posted", "head_sha": "1" * 40, "round": 1, "review_file": rv, "coverage_file": path,
                "coverage_file_sha": C.file_sha(path), "review_file_sha": C.file_sha(rv)}
        unchecked = R.build({"previous": {**prev, "coverage_ok": None}}, git, head=H2)
        self.assertIsNone(unchecked["reuse"])             # a "reused" that failed the check under shadow
        checked = R.build({"previous": {**prev, "coverage_ok": True}}, git, head=H2)
        self.assertEqual(checked["reuse"]["prev_coverage"], {"A.java": "reused"})
        # the archived review edited after it was archived (skipped -> reviewed): nothing is inherited (R11 #2)
        with open(rv, "w", encoding="utf-8") as f:
            json.dump({"head_sha": "1" * 40, "verdict": "COMMENT", "findings": [],
                       "coverage": [{"path": "A.java", "status": "reviewed"}]}, f)
        self.assertIsNone(R.build({"previous": {**prev, "coverage_ok": True}}, git, head=H2)["reuse"])
        self.assertIsNone(R.build({"previous": {**prev, "coverage_ok": True, "review_file_sha": None}}, git,
                                  head=H2)["reuse"])

    def test_ledger_entry_archives_the_verified_manifest(self):
        wt = os.path.join(self.tmp, "5@22222222")
        with open(C.path_of(wt), "w", encoding="utf-8") as f:
            json.dump(manifest("A.java"), f)
        job = {"diff_refs": {"head_sha": H2, "base_sha": "b" * 40}, "iid": 5, "worktree": wt,
               "coverage_sha": C.file_sha(C.path_of(wt))}
        e = R.record_posted(job, review={"verdict": "COMMENT"})
        self.assertEqual(C.file_sha(e["coverage_file"]), job["coverage_sha"])
        self.assertEqual(e["coverage_file_sha"], job["coverage_sha"])
        e2 = R.record_posted({**job, "round": 2, "coverage_sha": "tampered"}, review={})
        self.assertIsNone(e2["coverage_file"])                                  # not the verified manifest

    def test_archive_is_the_posted_review_not_the_file_after(self):
        tmp = tempfile.mkdtemp(prefix="cov-arch-")
        self.addCleanup(shutil.rmtree, tmp, True)
        wt = os.path.join(tmp, "5@22222222")
        with open(wt + ".review.json", "w", encoding="utf-8") as f:
            json.dump({"verdict": "EDITED AFTER POST"}, f)
        posted = {"verdict": "COMMENT", "coverage": [{"path": "A.java", "status": "reviewed"}]}
        e = R.record_posted({"diff_refs": {"head_sha": H2}, "iid": 5, "worktree": wt}, review=posted)
        with open(e["review_file"], encoding="utf-8") as f:
            self.assertEqual(json.load(f), posted)


class PostReadTest(unittest.TestCase):
    """post re-reads review.json: it must be the payload that was approved / passes the rules on THIS read."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cov-post-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        mock.patch.dict(os.environ, {"REVIEW_STATE_DIR": os.path.join(self.tmp, "state"), "REVIEW_ORCA": "0",
                                     "REVIEW_COVERAGE": "on", "REVIEW_AUTO_POST": "auto",
                                     "REVIEW_AUTO_POST_PROJECTS": "*"}).start()
        self.addCleanup(mock.patch.stopall)
        self.wt = os.path.join(self.tmp, "5@22222222")
        with open(C.path_of(self.wt), "w", encoding="utf-8") as f:
            json.dump(manifest("A.java", "B.java"), f)
        self.job = {"id": "77-5", "iid": 5, "project_id": "77", "head_sha": H2, "diff_refs": {"head_sha": H2},
                    "worktree": self.wt, "title": "t", "source_branch": "f", "target_branch": "develop",
                    "status": "awaiting_approval", "review_file": self.wt + ".review.json",
                    "coverage_sha": C.file_sha(C.path_of(self.wt)), "coverage_mode": "on"}
        self.full = {"head_sha": H2, "verdict": "COMMENT", "summary": "ok", "findings": [],
                     "coverage": [{"path": "A.java", "status": "reviewed"}, {"path": "B.java", "status": "reviewed"}]}
        self.gap = {**self.full, "coverage": [{"path": "A.java", "status": "reviewed"},
                                              {"path": "B.java", "status": "skipped", "reason": "x"}]}
        # approval_status read A (approved); review.json now holds B
        mock.patch.object(rp, "approval_status", return_value="approved").start()
        fresh = {"stale": False, "state": "opened", "current_head": H2, "reviewed_head": H2}
        mock.patch.dict(sys.modules, {"review_ws": mock.Mock(check_stale=lambda jid, job=None: fresh)}).start()

    def _post(self, approval, now):
        with open(self.job["review_file"], "w", encoding="utf-8") as f:
            json.dump(now, f)
        rc.save_job({**self.job, "approval": approval})
        return rp._post_locked("77-5", dry_run=True, notify=False)

    def test_human_approval_of_another_payload(self):
        a = {"via": "terminal", "decision": "approved", "payload_hash": rp.payload_hash(self.job, self.full)}
        self.assertTrue(self._post(a, self.full)["dry_run"])                  # same payload: goes on
        with self.assertRaisesRegex(PermissionError, "changed since it was approved"):
            self._post(a, self.gap)

    def test_rules_rechecked_on_this_read(self):
        a = {"via": "rules", "payload_hash": rp.payload_hash(self.job, self.full)}
        self.assertTrue(self._post(a, self.full)["auto"])
        with self.assertRaisesRegex(PermissionError, "rules no longer pass"):
            self._post(a, self.gap)

    def test_lost_response_archives_what_was_sent(self):
        import contextlib
        sent = {**self.full, "summary": "bản đã gửi"}
        with open(rp.posting_path(self.job), "w", encoding="utf-8") as f:
            json.dump(sent, f)
        a = {"via": "terminal", "decision": "approved", "payload_hash": rp.payload_hash(self.job, self.full)}
        with open(self.job["review_file"], "w", encoding="utf-8") as f:
            json.dump(self.full, f)
        rc.save_job({**self.job, "approval": a})
        gl = mock.Mock()
        with mock.patch.object(rr, "posted_note", return_value={"id": 9, "created_at": "2026-11-24T10:00:00Z"}), \
                mock.patch.object(rc, "gitlab_project", return_value=contextlib.nullcontext(gl)), \
                mock.patch.object(rp, "_save_posted") as saved:
            out = rp._post_locked("77-5", dry_run=False, notify=False)
        self.assertTrue(out["reconciled"])
        self.assertFalse(saved.call_args[0][0]["posted_rounds"][-1].get("coverage_ok"))   # never inherited
        gl.add_mr_comment.assert_not_called()
        rounds = os.path.join(self.tmp, "rounds")
        archived = [p for p in os.listdir(rounds) if p.endswith(".review.json")]
        with open(os.path.join(rounds, archived[0]), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["summary"], "bản đã gửi")             # not the draft on disk now

    def test_terminal_approval_saves_on_a_fresh_read_of_the_same_payload(self):
        """R14 #2: a prepare that pinned another mode while the human read the text is not overwritten."""
        with open(self.job["review_file"], "w", encoding="utf-8") as f:
            json.dump(self.full, f)
        rc.save_job({**self.job, "coverage_mode": "shadow"})
        shown = rc.load_job("77-5")

        def human(_prompt):                       # meanwhile a prepare pins "on" (same manifest)
            rc.save_job({**rc.load_job("77-5"), "coverage_mode": "on"})
            return "y"
        with mock.patch.object(sys.stdin, "isatty", return_value=True), mock.patch("builtins.input", human), \
                mock.patch("builtins.print"):
            with self.assertRaisesRegex(RuntimeError, "changed while you were reading"):
                rp.human_decide(shown, True)
        self.assertEqual(rc.load_job("77-5")["coverage_mode"], "on")           # not restored to shadow
        self.assertFalse((rc.load_job("77-5").get("approval") or {}).get("decision"))
        with mock.patch.object(sys.stdin, "isatty", return_value=True), mock.patch("builtins.input", lambda p: "y"), \
                mock.patch("builtins.print"):
            out = rp.human_decide(rc.load_job("77-5"), True)                 # unchanged since shown: saved
        self.assertEqual(out["decision"], "approved")
        self.assertEqual(rc.load_job("77-5")["coverage_mode"], "on")

    def test_no_worktree_no_posting_record(self):
        """It used to land as .posting.json in the cwd — the repo root (Fable audit)."""
        job = {k: v for k, v in self.job.items() if k != "worktree"}
        self.assertEqual(rp.posting_path(job), "")
        self.assertIsNone(rp._posting_review(job, H2))

    def test_reconcile_prefers_the_posting_record(self):
        self.assertIsNone(rp._posting_review(self.job, H2))
        with open(rp.posting_path(self.job), "w", encoding="utf-8") as f:
            json.dump({**self.full, "summary": "A"}, f)
        self.assertEqual(rp._posting_review(self.job, H2)["summary"], "A")
        self.assertIsNone(rp._posting_review(self.job, "9" * 40))               # another head's attempt

    def test_flag_off_keeps_the_old_pending_rules_behaviour(self):
        """Daily cap reached with the flag off: HEAD left the job waiting for the rules — so does this."""
        with open(self.job["review_file"], "w", encoding="utf-8") as f:
            json.dump(self.full, f)
        with mock.patch.dict(os.environ, {"REVIEW_COVERAGE": "off"}):
            a = {"via": "rules", "payload_hash": rp.payload_hash(self.job, self.full)}
            rc.save_job({**self.job, "approval": a})
            mock.patch.object(rp, "approval_status", return_value="pending").start()
            with mock.patch.object(rp, "_telegram_request", return_value={}) as tg:
                out = rp.sweep()
                self.assertTrue(rp.request_approval(rc.load_job("77-5")).get("reused"))
            self.assertFalse(tg.called)
            self.assertEqual(rc.load_job("77-5")["status"], "awaiting_approval")
            self.assertFalse([x for x in out["actions"] if x.get("job_id") == "77-5"])


class PrepareTest(unittest.TestCase):
    """prepare end to end on a local "GitLab" remote: manifest published with its hash on the job, served log
    written by diff; flag off = no manifest at all."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cov-prep-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        origin, self.clone = os.path.join(self.tmp, "origin"), os.path.join(self.tmp, "clone")
        os.makedirs(origin)
        g = lambda *a: _git(origin, *a)  # noqa: E731
        g("init", "-q", "-b", "develop")
        g("config", "user.email", "t@t")
        g("config", "user.name", "t")
        _write(os.path.join(origin, "A.java"), "class A {}\n")
        g("add", "-A")
        g("commit", "-qm", "base")
        self.base = g("rev-parse", "HEAD").strip()
        g("checkout", "-qb", "feature")
        _write(os.path.join(origin, "A.java"), "class A { int x; }\n")
        _write(os.path.join(origin, "B.java"), "class B {}\n")
        g("add", "-A")
        g("commit", "-qm", "head")
        self.head = g("rev-parse", "HEAD").strip()
        g("update-ref", "refs/merge-requests/5/head", self.head)
        g("checkout", "-q", "develop")
        subprocess.run(["git", "clone", "-q", origin, self.clone], check=True, capture_output=True)
        import review_ws
        self.ws = review_ws
        mock.patch.dict(os.environ, {"REVIEW_STATE_DIR": os.path.join(self.tmp, "state"), "REVIEW_ORCA": "0",
                                     "REVIEW_WS_DIR": os.path.join(self.tmp, "reviews"),
                                     "REVIEW_CODEGRAPH": "0"}).start()
        self.addCleanup(mock.patch.stopall)
        mr = {"state": "opened", "target_branch": "develop", "source_branch": "feature", "title": "t",
              "diff_refs": {"base_sha": self.base, "start_sha": self.base, "head_sha": self.head}}
        mock.patch.object(rc, "gitlab_mr", return_value=mr).start()
        mock.patch.object(review_ws, "open_repo",
                          return_value=review_ws.Repo(self.clone, bare=False, use_token=False, kind="test")).start()
        rc.save_job({"id": "77-5", "project_id": "77", "iid": 5, "status": "running", "gen": "g1"})

    def tearDown(self):
        subprocess.run(["git", "-C", self.clone, "worktree", "prune"], capture_output=True)

    def test_on(self):
        with mock.patch.dict(os.environ, {"REVIEW_COVERAGE": "on"}):
            out = self.ws.prepare("77-5")
            job = rc.load_job("77-5")
            self.assertEqual(out["coverage"]["files"], 2)
            self.assertEqual(rc.load_job("77-5")["coverage_mode"], "on")
            self.assertEqual(job["coverage_sha"], C.file_sha(C.path_of(job["worktree"])))
            m, err = C.state(job)
            self.assertIsNone(err)
            self.assertEqual(sorted(f["path"] for f in m["files"]), ["A.java", "B.java"])
            self.assertFalse([p for p in os.listdir(os.path.dirname(job["worktree"])) if ".tmp-" in p])
            self.assertEqual(m["start_sha"], self.base)
            self.ws.diff("77-5", path="A.java")
            self.assertEqual(C.served_signal(job["worktree"], m), {"A.java": "full"})
            self.ws.diff("77-5")
            self.assertEqual(C.served_signal(job["worktree"], m), {"A.java": "full", "B.java": "full"})
            r = {"head_sha": self.head, "verdict": "COMMENT", "summary": "ok", "findings": [],
                 "coverage": [{"path": "A.java", "status": "reviewed"}]}
            self.assertTrue(any("B.java missing" in e for e in rp.validate(job, r)))

    def test_same_head_other_base_is_stale_only_with_a_manifest(self):
        with mock.patch.dict(os.environ, {"REVIEW_COVERAGE": "on"}):
            self.ws.prepare("77-5")
            self.assertFalse(self.ws.check_stale("77-5")["stale"])
            moved = {**rc.gitlab_mr(), "diff_refs": {"base_sha": "f" * 40, "start_sha": self.base,
                                                     "head_sha": self.head}}
            with mock.patch.object(rc, "gitlab_mr", return_value=moved):
                self.assertTrue(self.ws.check_stale("77-5")["stale"])
                job = rc.load_job("77-5")
                job.pop("coverage_sha")
                with mock.patch.object(rc, "load_job", return_value=job):
                    self.assertFalse(self.ws.check_stale("77-5")["stale"])   # off: head-only as before
                # the poster's snapshot is what is compared — not a re-read a concurrent prepare moved
                snap = {**rc.load_job("77-5"), "diff_refs": moved["diff_refs"]}
                self.assertFalse(self.ws.check_stale("77-5", job=snap)["stale"])

    def test_same_head_new_scope_sets_the_old_draft_aside(self):
        with mock.patch.dict(os.environ, {"REVIEW_COVERAGE": "on"}):
            self.ws.prepare("77-5")
            wt = rc.load_job("77-5")["worktree"]
            with open(wt + ".review.json", "w", encoding="utf-8") as f:
                json.dump({"head_sha": self.head, "verdict": "APPROVE"}, f)
            moved = {**rc.gitlab_mr(), "diff_refs": {"base_sha": self.base, "start_sha": self.head,
                                                     "head_sha": self.head}}
            with mock.patch.object(rc, "gitlab_mr", return_value=moved):
                self.ws.prepare("77-5")
            self.assertFalse(os.path.exists(wt + ".review.json"))
            self.assertTrue([p for p in os.listdir(os.path.dirname(wt)) if ".review.json.old-scope-" in p])
            self.ws.prepare("77-5")                       # same scope again: a draft is kept
            with open(wt + ".review.json", "w", encoding="utf-8") as f:
                f.write("{}")
            self.ws.prepare("77-5")
            self.assertTrue(os.path.exists(wt + ".review.json"))

    def test_a_racing_prepare_sees_a_mode_change(self):
        job = {"id": "77-5", "coverage_sha": "x", "diff_refs": {}, "status": "running"}
        self.assertNotEqual(self.ws._seen({**job, "coverage_mode": "shadow"}),
                            self.ws._seen({**job, "coverage_mode": "on"}))         # Codex step1 R15

    def test_publish_waits_for_a_post_in_flight(self):
        real, order = rc.file_lock, []

        def spy(name, *a, **kw):
            order.append(name)
            return real(name, *a, **kw)
        with mock.patch.dict(os.environ, {"REVIEW_COVERAGE": "on"}), mock.patch.object(rc, "file_lock", spy):
            self.ws.prepare("77-5")
        i = order.index("post-77-5")
        self.assertEqual(order[i + 1:i + 3], ["approval-77-5", "queue"])   # post → approval → queue

    def test_on_off_on_at_the_same_head_sets_the_draft_aside(self):
        """on (scope A) → off (another scope) → on (scope A): the file on disk still says A, the job pin is gone —
        a draft edited in between must not count as reviewed for A (Codex step1 R6 #2)."""
        with mock.patch.dict(os.environ, {"REVIEW_COVERAGE": "on"}):
            self.ws.prepare("77-5")
        wt = rc.load_job("77-5")["worktree"]
        with mock.patch.dict(os.environ, {"REVIEW_COVERAGE": "off"}):
            self.ws.prepare("77-5")
        with open(wt + ".review.json", "w", encoding="utf-8") as f:
            f.write("{}")
        with mock.patch.dict(os.environ, {"REVIEW_COVERAGE": "on"}):
            self.ws.prepare("77-5")
        self.assertFalse(os.path.exists(wt + ".review.json"))

    def test_draft_from_before_the_flag_is_set_aside(self):
        with mock.patch.dict(os.environ, {"REVIEW_COVERAGE": "off"}):
            self.ws.prepare("77-5")
        wt = rc.load_job("77-5")["worktree"]
        with open(wt + ".review.json", "w", encoding="utf-8") as f:
            f.write("{}")
        with mock.patch.dict(os.environ, {"REVIEW_COVERAGE": "on"}):
            self.ws.prepare("77-5")
        self.assertFalse(os.path.exists(wt + ".review.json"))

    def test_new_scope_with_an_unfinished_post_needs_a_human(self):
        """Old-scope inline comments / a posting record at this head: their markers are per head, so a retry of
        the new scope could take them for its own — prepare refuses and publishes nothing (Codex step1 R4)."""
        with mock.patch.dict(os.environ, {"REVIEW_COVERAGE": "off"}):
            self.ws.prepare("77-5")
        for mark in ("inline", "posting"):
            job = rc.load_job("77-5")
            wt = job["worktree"]
            if mark == "inline":
                job["inline_done"] = {self.head: [0]}
                rc.save_job(job)
            else:
                job.pop("inline_done", None)
                rc.save_job(job)
                with open(wt + ".posting.json", "w", encoding="utf-8") as f:
                    f.write("{}")
            with mock.patch.dict(os.environ, {"REVIEW_COVERAGE": "on"}):
                with self.assertRaisesRegex(RuntimeError, "unfinished post attempt"):
                    self.ws.prepare("77-5")
            self.assertFalse(os.path.exists(C.path_of(wt)))                  # nothing published
            self.assertNotIn("coverage_sha", rc.load_job("77-5"))
            self.assertFalse([p for p in os.listdir(os.path.dirname(wt)) if ".tmp-" in p])

    def test_off(self):
        with mock.patch.dict(os.environ, {"REVIEW_COVERAGE": "off"}):
            out = self.ws.prepare("77-5")
            job = rc.load_job("77-5")
            self.assertIsNone(out["coverage"])
            self.assertNotIn("coverage_sha", job)
            self.assertFalse(os.path.exists(C.path_of(job["worktree"])))
            with open(job["ws_path"], encoding="utf-8") as f:
                self.assertNotIn("coverage", json.load(f))


if __name__ == "__main__":
    unittest.main()
