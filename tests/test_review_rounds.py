"""Review rounds (mr-review/tools/review_rounds.py): a re-review checks the previous round — every open item
gets a status, APPROVE is refused while a blocker/major is still open, and new non-blocker findings on code
unchanged since the previous round are refused (the "moving target" that kept MRs looping)."""

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(_ROOT, ".claude", "skills", "mr-review", "tools"))

import review_common as rc  # noqa: E402
import review_post as rp  # noqa: E402
import review_queue as rq  # noqa: E402
import review_rounds as R  # noqa: E402
import review_rules as rr  # noqa: E402

H1, H2 = "1" * 40, "2" * 40


def prev(open_items, delta=None, round_=2):
    return {"round": round_, "previous": {"head_sha": H1, "round": round_ - 1}, "open": open_items,
            "replies": [], "delta": delta}


ITEMS = [{"id": "R1.1", "severity": "blocker", "title": "NPE khi list rỗng", "file": "A.java", "line": 10},
         {"id": "R1.2", "severity": "minor", "title": "Tên biến", "file": "A.java", "line": 30}]


def review(**kw):
    base = {"head_sha": H2, "verdict": "COMMENT", "summary": "ok", "findings": []}
    base.update(kw)
    return base


class CheckTest(unittest.TestCase):
    def test_every_open_item_needs_a_status(self):
        errs = R.check(review(previous_findings=[{"id": "R1.1", "status": "fixed"}]), prev(ITEMS))
        self.assertTrue(any("R1.2" in e and "without a status" in e for e in errs))
        self.assertEqual(R.check(review(previous_findings=[{"id": "R1.1", "status": "fixed"},
                                                           {"id": "R1.2", "status": "fixed"}]), prev(ITEMS)), [])

    def test_unknown_duplicate_and_bad_status(self):
        errs = R.check(review(previous_findings=[{"id": "R9.9", "status": "fixed"}, {"id": "R1.1", "status": "done"},
                                                 {"id": "R1.2", "status": "fixed"}, {"id": "R1.2", "status": "fixed"}]),
                       prev(ITEMS))
        joined = " | ".join(errs)
        self.assertIn("R9.9", joined)
        self.assertIn("status must be one of", joined)
        self.assertIn("given twice", joined)

    def test_withdrawn_and_disputed_need_a_reason(self):
        errs = R.check(review(previous_findings=[{"id": "R1.1", "status": "disputed"},
                                                 {"id": "R1.2", "status": "withdrawn", "note": " "}]), prev(ITEMS))
        self.assertEqual(sum("note is required" in e for e in errs), 2)

    def test_approve_refused_while_blocker_open_or_disputed(self):
        for st in ("not_fixed", "disputed"):
            r = review(verdict="APPROVE", previous_findings=[{"id": "R1.1", "status": st, "note": "x"},
                                                             {"id": "R1.2", "status": "fixed"}])
            self.assertTrue(any("APPROVE" in e for e in R.check(r, prev(ITEMS))), st)
        r = review(verdict="APPROVE", previous_findings=[{"id": "R1.1", "status": "fixed"},
                                                         {"id": "R1.2", "status": "not_fixed"}])  # a minor may stay
        self.assertEqual(R.check(r, prev(ITEMS)), [])

    def test_new_findings_only_on_changed_lines_unless_blocker(self):
        delta = {"A.java": [[50, 60]]}
        pfs = [{"id": "R1.1", "status": "fixed"}, {"id": "R1.2", "status": "fixed"}]
        on_old = {"severity": "major", "title": "x", "file": "A.java", "line": 20}
        near = {"severity": "minor", "title": "y", "file": "A.java", "line": 62}           # within slack
        other_file = {"severity": "nit", "title": "z", "file": "B.java", "line": 5}
        blocker_old = {"severity": "blocker", "title": "w", "file": "A.java", "line": 20}
        general = {"severity": "major", "title": "MR thiếu test"}                           # no file: allowed
        errs = R.check(review(previous_findings=pfs, findings=[on_old, near, other_file, blocker_old, general]),
                       prev(ITEMS, delta))
        self.assertEqual(len(errs), 2)
        self.assertTrue(errs[0].startswith("findings[0]") and errs[1].startswith("findings[2]"))
        # old head unreachable: no delta, no restriction
        self.assertEqual(R.check(review(previous_findings=pfs, findings=[on_old]), prev(ITEMS, None)), [])

    def test_escape_hatches_are_closed_and_caused_by_opens_one(self):
        delta = {"A.java": [[50, 60]], "Gone.java": []}
        pfs = [{"id": "R1.1", "status": "fixed"}, {"id": "R1.2", "status": "fixed"}]
        cases = [({"severity": "major", "title": "a", "file": "A.java", "line": 5, "side": "old"}, False),
                 ({"severity": "major", "title": "b", "file": "C.java", "side": "old"}, True),   # file unchanged
                 ({"severity": "major", "title": "c", "file": "C.java"}, True),                  # no line, unchanged file
                 ({"severity": "major", "title": "d", "file": "A.java"}, False),                 # no line, changed file
                 ({"severity": "major", "title": "e", "file": "Gone.java", "line": 3, "side": "old"}, False),
                 ({"severity": "major", "title": "f", "file": "Caller.java", "line": 9, "caused_by": "A.java:55"}, False),
                 ({"severity": "major", "title": "g", "file": "Caller.java", "line": 9, "caused_by": "A.java:5"}, True),
                 ({"severity": "major", "title": "h", "file": "Caller.java", "line": 9, "caused_by": 7}, True),
                 ({"severity": "major", "title": "i", "file": "Caller.java", "line": 9, "caused_by": "A.java:48"}, True),
                 ({"severity": "major", "title": "j", "file": ["A.java"], "line": 55}, False)]   # bad type: shape error elsewhere
        for f, refused in cases:
            errs = R.check(review(previous_findings=pfs, findings=[f]), prev(ITEMS, delta))
            self.assertEqual(bool(errs), refused, f)

    def test_bad_types_are_errors_not_crashes(self):
        for pfs in (42, "x", [[1]], [{"id": [], "status": "fixed"}], [{"id": "R1.1", "status": ["fixed"]}],
                    [{"id": "R1.1", "status": "fixed", "note": 5}]):
            errs = R.check(review(previous_findings=pfs), prev(ITEMS))
            self.assertTrue(errs, pfs)
        job = {"head_sha": H2, "diff_refs": {"head_sha": H2}}
        self.assertTrue(rp.validate(job, review(previous_findings=42)))       # _texts survives it too

    def test_first_round(self):
        self.assertEqual(R.check(review(), None), [])
        self.assertEqual(R.check(review(previous_findings=[]), None), [])
        self.assertTrue(R.check(review(previous_findings=[{"id": "R1.1", "status": "fixed"}]), None))
        self.assertTrue(R.check(review(previous_findings={}), None))


class CarryTest(unittest.TestCase):
    def test_open_items_carry_unfixed_and_add_new_non_nits(self):
        prev_prev = {"open": [{"id": "R1.1", "severity": "major", "title": "a"},
                              {"id": "R1.2", "severity": "minor", "title": "b"},
                              {"id": "R1.3", "severity": "major", "title": "c"}]}
        prev_review = {"previous_findings": [{"id": "R1.1", "status": "not_fixed", "note": "vẫn"},
                                             {"id": "R1.2", "status": "fixed"},
                                             {"id": "R1.3", "status": "disputed", "note": "dev không đồng ý"}],
                       "findings": [{"severity": "nit", "title": "n"}, {"severity": "major", "title": "m",
                                                                         "file": "X.java", "line": 3}]}
        items = R.open_items(prev_review, prev_prev, 2)
        self.assertEqual([i["id"] for i in items], ["R1.1", "R1.3", "R2.2"])     # nit R2.1 never carried
        self.assertEqual(items[1]["last_status"], "disputed")

    def test_previous_of_keeps_the_last_posted_round(self):
        posted = {"status": "posted", "head_sha": H1, "verdict": "REQUEST_CHANGES", "worktree": "/w/5@11111111",
                  "posted_at": 100, "round": 2}
        p = R.previous_of(posted)
        self.assertEqual((p["review_file"], p["round"], R.round_of(p)), ("/w/5@11111111.review.json", 2, 3))
        cancelled = {"status": "cancelled", "head_sha": H2, "previous": p}
        self.assertEqual(R.previous_of(cancelled), p)                             # the member never saw it
        self.assertEqual(R.round_of(R.previous_of({"status": "failed", "head_sha": H2})), 1)
        self.assertIsNone(R.previous_of(None))

    def test_changed_lines_header_state_and_quoted_paths(self):
        spaced = R.changed_lines('diff --git a/ s.java b/ s.java\n--- a/ s.java\t\n+++ b/ s.java\t\n@@ -1 +1 @@\n-a\n+b\n')
        self.assertEqual(spaced, {" s.java": [[1, 1]]})          # names keep their spaces; git's tab is dropped
        diff = ('diff --git a/A.java b/A.java\n--- a/A.java\n+++ b/A.java\n@@ -1,0 +2,2 @@\n+++ x is code\n+y\n'
                '@@ -9,0 +12,1 @@\n+z\n'
                'diff --git "a/t\\303\\252n.java" "b/t\\303\\252n.java"\n--- "a/t\\303\\252n.java"\n'
                '+++ "b/t\\303\\252n.java"\n@@ -1 +1 @@\n-a\n+b\n'
                'diff --git a/Del.java b/Del.java\n--- a/Del.java\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-a\n-b\n'
                'diff --git a/bin.png b/bin.png\nBinary files a/bin.png and b/bin.png differ\n')
        self.assertEqual(R.changed_lines(diff), {"A.java": [[2, 3], [12, 12]], "tên.java": [[1, 1]], "Del.java": []})

    def test_missing_older_facts_keep_open_items(self):
        prev_review = {"previous_findings": [{"id": "R1.4", "status": "not_fixed", "note": "vẫn sai"},
                                             {"id": "R1.5", "status": "fixed"}], "findings": []}
        items = R.open_items(prev_review, None, 2)
        self.assertEqual([(i["id"], i.get("history_gap")) for i in items], [("R1.4", True)])

    def test_changed_lines(self):
        diff = ("diff --git a/A.java b/A.java\n--- a/A.java\n+++ b/A.java\n@@ -3,2 +3,4 @@\n+x\n"
                "@@ -20 +22,0 @@\n-y\ndiff --git a/Old.java b/Old.java\n--- a/Old.java\n+++ /dev/null\n"
                "@@ -1,3 +0,0 @@\n")
        self.assertEqual(R.changed_lines(diff), {"A.java": [[3, 6], [22, 22]], "Old.java": []})

    def test_replies_skip_system_ours_and_older(self):
        notes = [{"system": True, "body": "added 1 commit", "created_at": "2026-11-24T10:00:00Z"},
                 {"body": rr.MARKER_FMT.format(iid=5, head="a" * 12) + " review", "created_at": "2026-11-24T10:00:00Z"},
                 {"body": "cũ", "created_at": "2026-11-20T10:00:00Z"},
                 {"body": "không đồng ý vì list không bao giờ rỗng", "author": {"username": "dev"},
                  "created_at": "2026-11-24T10:00:00Z", "position": {"new_path": "A.java", "new_line": 10}}]
        since = R._epoch("2026-11-23T00:00:00Z")
        out = R.replies(notes, since, rr.MARKER_RE)
        self.assertEqual([(r["author"], r["file"], r["line"]) for r in out], [("dev", "A.java", 10)])


def _git(cwd, *args):
    return subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, check=True).stdout


class BuildTest(unittest.TestCase):
    """Real git: round 1 posted at c1, member pushes c2 → prev.json lists open items + changed lines."""

    def test_build_from_a_posted_round(self):
        tmp = tempfile.mkdtemp(prefix="rounds-")
        repo = os.path.join(tmp, "r")
        os.makedirs(repo)
        _git(repo, "init", "-q")
        _git(repo, "config", "user.email", "t@t")
        _git(repo, "config", "user.name", "t")
        with open(os.path.join(repo, "A.java"), "w") as f:
            f.write("".join(f"line{i}\n" for i in range(1, 41)))
        _git(repo, "add", ".")
        _git(repo, "commit", "-qm", "c1")
        c1 = _git(repo, "rev-parse", "HEAD").strip()
        with open(os.path.join(repo, "A.java")) as f:
            lines = f.read().splitlines()
        lines[9] = "fixed10"
        with open(os.path.join(repo, "A.java"), "w") as f:
            f.write("\n".join(lines) + "\n")
        _git(repo, "commit", "-qam", "c2")
        c2 = _git(repo, "rev-parse", "HEAD").strip()
        wt1 = os.path.join(tmp, "5@" + c1[:8])
        with open(wt1 + ".review.json", "w", encoding="utf-8") as f:
            json.dump({"head_sha": c1, "verdict": "REQUEST_CHANGES",
                       "findings": [{"severity": "blocker", "title": "NPE", "file": "A.java", "line": 10},
                                    {"severity": "nit", "title": "space"}]}, f)
        job = {"head_sha": c2, "diff_refs": {"head_sha": c2},
               "previous": {"status": "posted", "head_sha": c1, "review_file": wt1 + ".review.json",
                            "worktree": wt1, "posted_at": 0, "round": 1}}

        def git(*a):
            try:
                return _git(repo, *a)
            except subprocess.CalledProcessError:
                return None
        data = R.build(job, git, lambda: [{"body": "đã sửa", "author": {"username": "dev"},
                                           "created_at": "2026-11-24T10:00:00Z"}], rr.MARKER_RE)
        self.assertEqual(data["round"], 2)
        self.assertEqual([i["id"] for i in data["open"]], ["R1.1"])
        self.assertEqual(data["delta"], {"A.java": [[10, 10]]})
        self.assertEqual(data["replies"][0]["body"], "đã sửa")
        # notes unreadable: replies marked, round still built
        data = R.build(job, git, mock.Mock(side_effect=RuntimeError("401")), rr.MARKER_RE)
        self.assertIn("401", data["replies_error"])
        # old head gone (force-push): no delta
        gone = {**job, "previous": {**job["previous"], "head_sha": "f" * 40}}
        with open(wt1 + ".review.json", "w", encoding="utf-8") as f:
            json.dump({"head_sha": "f" * 40, "findings": []}, f)
        self.assertIsNone(R.build(gone, git)["delta"])
        # first round / never posted
        self.assertIsNone(R.build({"previous": {"status": "cancelled"}}, git))
        # same head asked again: a fresh full review, not a round with an empty delta
        with open(wt1 + ".review.json", "w", encoding="utf-8") as f:
            json.dump({"head_sha": c1, "findings": []}, f)
        self.assertIsNone(R.build({**job, "head_sha": c1, "diff_refs": {"head_sha": c1}}, git))
        # requeued job: diff_refs still the old head, the head being prepared wins
        stale = {**job, "head_sha": c1, "diff_refs": {"head_sha": c1}}
        self.assertEqual(R.build(stale, git, head=c2)["delta"], {"A.java": [[10, 10]]})
        # a pure rename has no hunk: the file still counts as changed
        _git(repo, "mv", "A.java", "B.java")
        _git(repo, "commit", "-qm", "c3")
        c3 = _git(repo, "rev-parse", "HEAD").strip()
        self.assertIn("B.java", R.build(stale, git, head=c3)["delta"])


class PostIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rounds-post-")
        self.wt = os.path.join(self.tmp, "5@22222222")
        with open(self.wt + ".prev.json", "w", encoding="utf-8") as f:
            json.dump(prev(ITEMS, {"A.java": [[50, 60]]}), f)
        self.job = {"id": "77-5", "iid": 5, "project_id": "77", "head_sha": H2, "diff_refs": {"head_sha": H2},
                    "worktree": self.wt, "title": "t", "source_branch": "f", "target_branch": "develop",
                    "prev_json_sha": R.file_sha(self.wt + ".prev.json")}

    def test_prev_json_without_the_prepare_hash_is_ignored(self):
        # a prev.json at the path that this prepare did not build (another round of the same head) never applies
        job = {**self.job, "prev_json_sha": None}
        self.assertEqual(rp.validate(job, review()), [])

    def test_validate_uses_prev_json(self):
        r = review(previous_findings=[{"id": "R1.1", "status": "fixed"}])
        self.assertTrue(any("R1.2" in e for e in rp.validate(self.job, r)))

    def test_note_shows_previous_round_and_never_says_clean_while_open(self):
        r = review(previous_findings=[{"id": "R1.1", "status": "not_fixed", "note": "vẫn NPE"},
                                      {"id": "R1.2", "status": "withdrawn", "note": "dev đúng"}])
        self.assertEqual(rp.validate(self.job, r), [])
        with mock.patch.dict(os.environ, {"REVIEW_NOTE_STYLE": "full"}), \
                mock.patch.object(rc, "config", {"REVIEW_NOTE_STYLE": "full"}):
            note = rp.render_note(self.job, r, set())
        self.assertIn("### Đối chiếu vòng trước", note)
        self.assertIn("| R1.1 |", note)
        self.assertIn("❌ Chưa sửa", note)
        self.assertIn("↩️ Rút lại", note)
        self.assertIn("còn 1 mục chưa xong", note)                 # Critical section does not say "no issues"
        with mock.patch.object(rc, "config", {"REVIEW_NOTE_STYLE": "compact"}):
            compact = rp.render_note(self.job, r, set())
        self.assertIn("`R1.1`", compact)
        self.assertNotIn("Không có vấn đề cần sửa", compact)


class TamperTest(PostIntegrationTest):
    def test_deleted_or_edited_prev_json_is_an_error_not_a_first_round(self):
        job = {**self.job, "prev_json_sha": R.file_sha(self.wt + ".prev.json")}
        ok = review(previous_findings=[{"id": "R1.1", "status": "fixed"}, {"id": "R1.2", "status": "fixed"}])
        self.assertEqual(rp.validate(job, ok), [])
        with open(self.wt + ".prev.json", "w", encoding="utf-8") as f:
            json.dump(prev([]), f)                                   # "nothing open" — forged
        self.assertTrue(any("changed after prepare" in e for e in rp.validate(job, review())))
        os.remove(self.wt + ".prev.json")
        self.assertTrue(any("changed after prepare" in e for e in rp.validate(job, review())))

    def test_long_notes_are_bounded_in_the_note(self):
        r = review(previous_findings=[{"id": "R1.1", "status": "disputed", "note": "x " * 2000},
                                      {"id": "R1.2", "status": "fixed"}])
        with mock.patch.object(rc, "config", {"REVIEW_NOTE_STYLE": "full"}):
            note = rp.render_note(self.job, r, set())
        row = next(ln for ln in note.splitlines() if ln.startswith("| R1.1 |"))
        self.assertLess(len(row), 600)


class StaleReconcileTest(unittest.TestCase):
    """Posted, response lost, member pushed: the requeued round must compare against what was posted."""

    def test_requeue_after_a_lost_post_keeps_that_round(self):
        import contextlib
        tmp = tempfile.mkdtemp(prefix="rounds-stale-")
        mock.patch.dict(os.environ, {"REVIEW_STATE_DIR": tmp, "REVIEW_ORCA": "0"}).start()
        self.addCleanup(mock.patch.stopall)
        job = {"id": "77-5", "project_id": "77", "iid": 5, "head_sha": H1, "diff_refs": {"head_sha": H1},
               "worktree": os.path.join(tmp, "5@11111111"), "status": "approved", "round": 1}
        rc.save_job(job)
        gl = mock.Mock()
        gl._api.side_effect = lambda p: p
        gl._request.return_value = [{"body": rr.marker(job) + " posted"}]
        stale = {"stale": True, "state": "opened", "current_head": H2, "reviewed_head": H1}
        with mock.patch.object(rp, "approval_status", return_value="approved"), \
                mock.patch.object(rp, "load_review", return_value=review(head_sha=H1, verdict="REQUEST_CHANGES")), \
                mock.patch.object(rc, "gitlab_project", return_value=contextlib.nullcontext(gl)), \
                mock.patch.dict(sys.modules, {"review_ws": mock.Mock(check_stale=lambda j, job=None: stale)}):
            out = rp.post("77-5", notify=False)
        self.assertTrue(out["stale"])
        j = rc.load_job("77-5")
        self.assertEqual((j["status"], j["head_sha"], j["round"]), ("queued", H2, 2))
        # archived: the review that was sent (here the draft read for the post — no posting record)
        with open(j["previous"]["review_file"], encoding="utf-8") as f:
            self.assertEqual(json.load(f)["verdict"], "REQUEST_CHANGES")
        self.assertIn(os.path.join(tmp, "rounds"), j["previous"]["review_file"])
        gl.add_mr_comment.assert_not_called()


class LedgerTest(unittest.TestCase):
    """History lives in the posted-rounds ledger + immutable archived copies, never in per-head live files."""

    def test_archive_survives_a_later_overwrite_of_the_live_file(self):
        tmp = tempfile.mkdtemp(prefix="rounds-ledger-")
        wt = os.path.join(tmp, "5@11111111")
        with open(wt + ".review.json", "w", encoding="utf-8") as f:
            json.dump({"head_sha": H1, "verdict": "REQUEST_CHANGES", "findings": [{"severity": "major", "title": "A"}]}, f)
        with open(wt + ".prev.json", "w", encoding="utf-8") as f:
            json.dump(prev([]), f)
        job = {"iid": 5, "head_sha": H1, "diff_refs": {"head_sha": H1}, "worktree": wt, "round": 2,
               "review_file": wt + ".review.json", "prev_json_sha": R.file_sha(wt + ".prev.json")}
        e = R.record_posted(job, 100, "REQUEST_CHANGES", 7)
        self.assertEqual((e["round"], e["note_id"], e["posted_at"]), (2, 7, 100))
        self.assertNotEqual(e["review_file"], wt + ".review.json")
        self.assertTrue(e["prev_file"] and os.path.exists(e["prev_file"]))
        with open(wt + ".review.json", "w", encoding="utf-8") as f:          # a later draft at the same head
            json.dump({"head_sha": H1, "verdict": "APPROVE", "findings": []}, f)
        with open(e["review_file"], encoding="utf-8") as f:
            self.assertEqual(json.load(f)["verdict"], "REQUEST_CHANGES")
        # a prev.json that does not match the recorded hash is not archived as this round's facts
        e2 = R.record_posted({**job, "prev_json_sha": "nope"}, 100, "COMMENT")
        self.assertIsNone(e2["prev_file"])

    def test_ledger_of_legacy_and_dedupe(self):
        a = {"status": "posted", "head_sha": H1, "round": 1}
        b = {"status": "posted", "head_sha": H2, "round": 2}
        old = {"status": "failed", "head_sha": "3" * 40, "posted_rounds": [a, b]}
        self.assertEqual([e["head_sha"] for e in R.ledger_of(old)], [H1, H2])
        self.assertEqual(R.posted_at_head(R.ledger_of(old), H1), a)            # two rounds back is still found
        self.assertEqual(R.previous_of(old), b)
        legacy = {"status": "posted", "head_sha": H2, "worktree": "/w/x", "previous": a}
        self.assertEqual([e["head_sha"] for e in R.ledger_of(legacy)], [H1, H2])
        self.assertEqual([e["round"] for e in R.with_entry([a, b], {**a, "round": 9})], [2, 9])


class KnownPreviousTest(unittest.TestCase):
    """prepare runs BEFORE post when the head moved: a lost POST of the old head is adopted there."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rounds-known-")
        mock.patch.dict(os.environ, {"REVIEW_STATE_DIR": self.tmp, "REVIEW_ORCA": "0"}).start()
        self.addCleanup(mock.patch.stopall)
        import review_ws
        self.ws = review_ws
        self.old_wt = os.path.join(self.tmp, "5@11111111")
        with open(self.old_wt + ".review.json", "w", encoding="utf-8") as f:
            json.dump({"head_sha": H1, "verdict": "REQUEST_CHANGES", "findings": []}, f)
        self.job = {"id": "77-5", "project_id": "77", "iid": 5, "head_sha": H1, "diff_refs": {"head_sha": H1},
                    "worktree": self.old_wt, "status": "running", "round": 1,
                    "approval": {"via": "rules", "decision": "approved"}}

    def test_prepare_refuses_a_head_whose_review_is_posted(self):
        job = {**self.job, "approval": None, "status": "running",
               "previous": {"status": "posted", "head_sha": H2, "review_file": "x", "worktree": "y", "round": 1}}
        mr = {"state": "opened", "diff_refs": {"base_sha": "b" * 40, "start_sha": "c" * 40, "head_sha": H2}}
        with mock.patch.object(rc, "load_job", return_value=job), mock.patch.object(rc, "gitlab_mr", return_value=mr), \
                mock.patch.object(self.ws, "open_repo", side_effect=AssertionError("must stop before the repo")):
            with self.assertRaises(self.ws.AlreadyReviewed):
                self.ws.prepare("77-5")

    def test_round_found_posted_is_saved_before_already_reviewed(self):
        """A posted; B posted but the response was lost; MR force-pushed back to A → B must not be lost."""
        import contextlib
        a = {"status": "posted", "head_sha": H2, "round": 1, "verdict": "COMMENT", "review_file": "a", "prev_file": None}
        job = {**self.job, "gen": "g1", "previous": a, "posted_rounds": [a], "round": 2}   # job = round B at H1
        rc.save_job(job)
        mr = {"state": "opened", "diff_refs": {"base_sha": "b" * 40, "start_sha": "c" * 40, "head_sha": H2}}
        with mock.patch.object(rc, "gitlab_mr", return_value=mr), \
                mock.patch.object(rc, "gitlab_project", return_value=contextlib.nullcontext(mock.Mock())), \
                mock.patch.object(rr, "posted_note", return_value={"id": 3, "created_at": "2026-11-24T10:00:00Z"}), \
                mock.patch.object(self.ws, "open_repo", side_effect=AssertionError("must stop before the repo")):
            with self.assertRaises(self.ws.AlreadyReviewed):
                self.ws.prepare("77-5")
        saved = rc.load_job("77-5")
        self.assertEqual([e["head_sha"] for e in saved["posted_rounds"]], [H2, H1])   # B recorded
        self.assertEqual(saved["previous"]["head_sha"], H1)

    def test_ledger_save_refuses_a_job_that_changed(self):
        job = {**self.job, "gen": "g1"}
        rc.save_job(job)
        seen = self.ws._seen(rc.load_job("77-5"))
        changed = {**rc.load_job("77-5"), "posted_rounds": [{"status": "posted", "head_sha": H2}]}
        rc.save_job(changed)                                       # a sweeper recorded a round meanwhile
        with self.assertRaises(RuntimeError):
            self.ws._save_ledger("77-5", "g1", seen, {}, [])
        self.assertEqual(len(rc.load_job("77-5")["posted_rounds"]), 1)

    def test_unapproved_draft_is_never_looked_up(self):
        import contextlib
        with mock.patch.object(rc, "gitlab_project", return_value=contextlib.nullcontext(mock.Mock())), \
                mock.patch.object(rr, "posted_note") as pn:
            self.ws._known_previous({**self.job, "approval": None}, "77", 5, H2)
        pn.assert_not_called()

    def test_lookup_failure_stops_the_prepare(self):
        with mock.patch.object(rc, "gitlab_project", side_effect=RuntimeError("502")):
            with self.assertRaises(RuntimeError):
                self.ws._known_previous(self.job, "77", 5, H2)

    def test_lost_post_of_the_old_head_becomes_the_previous_round(self):
        import contextlib
        note = {"body": "x", "created_at": "2026-11-24T10:00:00Z"}
        with mock.patch.object(rc, "gitlab_project", return_value=contextlib.nullcontext(mock.Mock())), \
                mock.patch.object(rr, "posted_note", return_value=note) as pn:
            prev_, ledger = self.ws._known_previous(self.job, "77", 5, H2)
        self.assertEqual([e["head_sha"] for e in ledger], [H1])               # recorded in the ledger too
        self.assertTrue(os.path.exists(prev_["review_file"]))                 # archived copy …
        self.assertIn(os.path.join("rounds", "5-r1-"), prev_["review_file"])  # … under rounds/
        self.assertEqual(pn.call_args[0][1]["diff_refs"]["head_sha"], H1)      # looked up for the OLD head
        self.assertEqual((prev_["status"], prev_["head_sha"], prev_["verdict"]), ("posted", H1, "REQUEST_CHANGES"))
        self.assertEqual(prev_["posted_at"], int(R._epoch(note["created_at"])))  # the note's time, not "now"
        self.assertEqual(R.round_of(prev_), 2)

    def test_not_posted_keeps_previous_and_legacy_paths_are_derived(self):
        import contextlib
        legacy = {**self.job, "previous": {"status": "posted", "head_sha": "9" * 40, "verdict": "COMMENT"}}
        with mock.patch.object(rc, "gitlab_project", return_value=contextlib.nullcontext(mock.Mock())), \
                mock.patch.object(rr, "posted_note", return_value=None):
            prev_, ledger = self.ws._known_previous(legacy, "77", 5, H2)
        self.assertEqual([e["head_sha"] for e in ledger], ["9" * 40])
        self.assertTrue(prev_["review_file"].endswith(os.path.join("77", "5@99999999") + ".review.json"))


class QueueRoundTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rounds-q-")
        mock.patch.dict(os.environ, {"REVIEW_STATE_DIR": self.tmp, "REVIEW_ORCA": "0"}).start()
        mock.patch.object(rc, "resolve_project_id", return_value=("77", "grp/proj")).start()
        self.head = H1
        mock.patch.object(rc, "gitlab_mr", side_effect=lambda pid, iid: {
            "iid": iid, "state": "opened", "title": "t", "web_url": "u", "source_branch": "f",
            "target_branch": "develop", "author": {"username": "dev"}, "sha": self.head,
            "diff_refs": {"head_sha": self.head}, "references": {"full": "grp/proj!5"}}).start()
        self.addCleanup(mock.patch.stopall)

    def test_same_head_after_posted_review_opens_no_round(self):
        j = rq.add_job("proj", 5)["job"]
        j.update(status="posted", worktree="/w/5@11111111", posted_at=123)      # no verdict on the job: still skipped
        rc.save_job(j)
        out = rq.add_job("proj", 5, source="fchat")
        self.assertTrue(out.get("same_head"))
        self.assertEqual(rc.load_job(j["id"])["status"], "posted")

    def test_force_push_back_to_the_posted_commit_opens_no_round(self):
        j = rq.add_job("proj", 5)["job"]
        j.update(status="posted", worktree="/w/5@11111111", posted_at=123)
        rc.save_job(j)
        self.head = H2
        j2 = rq.add_job("proj", 5)["job"]                    # round 2 for H2 …
        j2.update(status="failed")                             # … fails
        rc.save_job(j2)
        self.head = H1                                         # MR force-pushed back to the posted commit
        out = rq.add_job("proj", 5)
        self.assertTrue(out.get("same_head"))
        # the answer describes the POSTED round at this head, not the failed one (the FChat card shows it)
        self.assertEqual((out["job"]["head_sha"], out["job"]["status"]), (H1, "posted"))
        self.assertNotEqual(j2["gen"], j["gen"])                # each round has its own identity

    def test_back_to_a_commit_posted_two_rounds_ago_while_a_round_runs(self):
        j = rq.add_job("proj", 5, requester="A")["job"]
        j.update(status="posted", worktree="/w/5@11111111", posted_at=1, verdict="REQUEST_CHANGES")
        rc.save_job(j)
        self.head = H2
        j2 = rq.add_job("proj", 5)["job"]
        self.assertEqual([e["head_sha"] for e in j2["posted_rounds"]], [H1])   # ledger inherited
        j2.update(status="posted", worktree="/w/5@22222222", posted_at=2, verdict="APPROVE",
                  posted_rounds=j2["posted_rounds"] + [{"status": "posted", "head_sha": H2, "round": 2,
                                                         "verdict": "APPROVE"}])
        rc.save_job(j2)
        self.head = "3" * 40
        j3 = rq.add_job("proj", 5)["job"]                                      # round 3 running at H3 …
        self.head = H1                                                         # … MR force-pushed back to H1
        out = rq.add_job("proj", 5, requester="Lan")
        self.assertTrue(out["same_head"])
        self.assertEqual((out["job"]["head_sha"], out["job"]["verdict"], out["job"]["rerequested_head"]),
                         (H1, "REQUEST_CHANGES", None))
        cur = rc.load_job(j3["id"])
        self.assertNotIn("Lan", [r["by"] for r in cur["requests"]])            # not merged into round 3

    def test_new_head_after_posted_review_is_round_two_with_its_file(self):
        j = rq.add_job("proj", 5)["job"]
        self.assertEqual(j["round"], 1)
        j.update(status="posted", worktree="/w/5@11111111", posted_at=123, verdict="REQUEST_CHANGES")
        rc.save_job(j)
        self.head = H2
        j2 = rq.add_job("proj", 5)["job"]
        self.assertEqual(j2["round"], 2)
        self.assertEqual(j2["previous"]["review_file"], "/w/5@11111111.review.json")
        self.assertEqual(j2["previous"]["posted_at"], 123)


if __name__ == "__main__":
    unittest.main()
