#!/usr/bin/env python3
"""Stdlib unittest for the mr-review pipeline (queue / post gate), the PreToolUse guard
and agent_runner role routing. GitLab and Orca are faked — no network.
    python -m unittest discover -s tests
"""

import html
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
import urllib.parse
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, ".."))
sys.path.insert(0, os.path.join(_ROOT, ".claude", "skills", "mr-review", "tools"))
sys.path.insert(0, os.path.join(_ROOT, ".claude", "skills", "auto-dev", "tools"))

import review_common as rc  # noqa: E402
import review_post as rp  # noqa: E402
import review_queue as rq  # noqa: E402

HEAD = "a" * 40


def fake_mr(iid, state="opened", head=HEAD):
    return {"iid": iid, "state": state, "title": f"MR {iid}", "web_url": f"https://g/x/-/merge_requests/{iid}",
            "source_branch": "feat/a", "target_branch": "develop", "author": {"username": "dev"},
            "diff_refs": {"base_sha": "b" * 40, "start_sha": "c" * 40, "head_sha": head}, "sha": head,
            "references": {"full": "grp/proj!%d" % iid}}


class QueueTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rq-test-")
        self.env = mock.patch.dict(os.environ, {"REVIEW_STATE_DIR": self.tmp, "REVIEW_ORCA": "0",
                                                "REVIEW_MAX_PARALLEL": "2"})
        self.env.start()
        self.p1 = mock.patch.object(rc, "resolve_project_id", return_value=("77", "grp/proj"))
        self.p2 = mock.patch.object(rc, "gitlab_mr", side_effect=lambda pid, iid: fake_mr(iid))
        self.p1.start()
        self.p2.start()

    def tearDown(self):
        mock.patch.stopall()

    def test_duplicate_requests_merge_into_one_job(self):
        a = rq.add_job("proj", 5, requester="A")
        b = rq.add_job("proj", 5, requester="B", priority=1)
        self.assertFalse(a["duplicate"])
        self.assertTrue(b["duplicate"])
        self.assertEqual(b["job"]["priority"], 1)
        self.assertEqual([r["by"] for r in b["job"]["requests"]], ["A", "B"])

    def test_closed_mr_rejected(self):
        with mock.patch.object(rc, "gitlab_mr", return_value=fake_mr(9, state="merged")):
            with self.assertRaises(ValueError):
                rq.add_job("proj", 9)

    def test_claim_respects_priority_and_capacity(self):
        rq.add_job("proj", 1, priority=5)
        rq.add_job("proj", 2, priority=0)
        rq.add_job("proj", 3, priority=5)
        self.assertEqual(rq.claim_next("w")["iid"], 2)
        self.assertEqual(rq.claim_next("w")["iid"], 1)
        self.assertEqual(rq.claim_next("w"), {})          # capacity 2 reached
        self.assertFalse(rq.pending()["can_start"])

    def test_dead_worker_is_requeued_then_failed(self):
        rq.add_job("proj", 4)
        job = rq.claim_next("w")
        for expected in ("queued", "failed"):
            job["status"], job["claimed_at"] = "running", 0
            rc.save_job(job)
            rq.pending()
            job = rc.load_job(job["id"])
            self.assertEqual(job["status"], expected)


class ParseTest(unittest.TestCase):
    def test_url_and_bang(self):
        with mock.patch.object(rc, "registry", return_value={"etask": {}}):
            self.assertEqual(rc.parse_mr_refs("xem https://git.x/a/b/c/-/merge_requests/12 nhé"),
                             [{"project": "a/b/c", "iid": 12, "host": "git.x"}])
            self.assertEqual(rc.parse_mr_refs("review etask !34"), [{"project": "etask", "iid": 34}])
            self.assertEqual(rc.parse_mr_refs("review MR 34", default_project="etask"),
                             [{"project": "etask", "iid": 34}])
            self.assertEqual(rc.parse_mr_refs("review !34"), [])   # no project known → nothing


class PostGateTest(unittest.TestCase):
    # these tests pin the FULL layout with inline comments; the compact default has its own class
    def setUp(self):
        p = mock.patch.dict(os.environ, {"REVIEW_NOTE_STYLE": "full", "REVIEW_INLINE_COMMENTS": "1"})
        p.start()
        self.addCleanup(p.stop)

    def job(self, **kw):
        j = {"id": "77-5", "project_id": "77", "iid": 5, "head_sha": HEAD,
             "diff_refs": {"base_sha": "b" * 40, "start_sha": "c" * 40, "head_sha": HEAD}}
        j.update(kw)
        return j

    def review(self, **kw):
        r = {"head_sha": HEAD, "verdict": "COMMENT", "summary": "ok",
             "findings": [{"severity": "minor", "title": "t", "file": "a.py", "line": 3}]}
        r.update(kw)
        return r

    def test_validate(self):
        self.assertEqual(rp.validate(self.job(), self.review()), [])
        errs = rp.validate(self.job(), self.review(head_sha="x", verdict="APPROVE",
                                                   findings=[{"severity": "blocker", "title": "b"}]))
        self.assertTrue(any("head_sha" in e for e in errs))
        self.assertTrue(any("contradictory" in e for e in errs))

    def test_orca_gate_and_board_never_approve(self):
        """Agents can resolve Orca gates and move board cards, so neither is an approval."""
        fake = mock.Mock()
        fake.gate_get.return_value = {"status": "resolved", "resolution": "approve"}
        with mock.patch.object(rp, "load_review", return_value=self.review()), \
                mock.patch.object(rc, "orca", return_value=fake), \
                mock.patch.object(rc, "board_status", return_value="completed"):
            for via in ("orca", "orca-board"):
                forged = self.job(approval={"via": via, "run": "r", "id": "g", "decision": "approved",
                                            "statuses": {"approve": "completed"}})
                self.assertEqual(rp.approval_status(forged), "pending", via)

    def test_human_approval_is_bound_to_payload(self):
        review = self.review()
        with mock.patch.object(rp, "load_review", return_value=review):
            phash = rp.payload_hash(self.job(), review)
            job = self.job(approval={"via": "terminal", "decision": "approved", "payload_hash": phash})
            self.assertEqual(rp.approval_status(job), "approved")
        edited = self.review(summary="ok — đã sửa sau khi duyệt")
        with mock.patch.object(rp, "load_review", return_value=edited):
            self.assertEqual(rp.approval_status(job), "pending")
            no_hash = self.job(approval={"via": "terminal", "decision": "approved"})
            self.assertEqual(rp.approval_status(no_hash), "pending")

    def test_agents_cannot_approve_without_tty(self):
        with mock.patch.object(sys.stdin, "isatty", return_value=False):
            with self.assertRaises(PermissionError):
                rp.human_decide(self.job(), True)

    def test_post_refuses_without_approval(self):
        with mock.patch.object(rc, "load_job", return_value=self.job(status="awaiting_approval",
                                                                     approval={"via": "terminal"})):
            with self.assertRaises(PermissionError):
                rp.post("77-5")

    def test_render_note_folds_non_inline_findings(self):
        with mock.patch.object(rp, "_ws", return_value={}):
            note = rp.render_note(self.job(), self.review(), set())
        self.assertIn("**1. [`a.py`:3] t.**", note)
        self.assertIn("No critical issues found.", note)

    def test_render_note_matches_the_team_sample(self):                       # layout agreed 2026-10-16
        path = "src/main/java/com/fis/etask/module/task/TaskHistoryRecorder.java"
        review = self.review(verdict="APPROVE", date="2026-10-08", verdict_note="kèm 1 góp ý Major về test",
                             scope="chỉ đọc code (diff + source nhánh tại 2a981f75). Chưa chạy `mvn compile`/test.",
                             positives=["Tái sử dụng batchInsert."],
                             compliance={"Testing": {"status": "warn", "note": "Chưa có test (xem Major 1)"},
                                         "Security": {"status": "ok", "note": "a|b"}},
                             findings=[{"severity": "major", "title": "Chưa có unit test cho `recordTaskCreationBatch`",
                                        "body": "method mới có nhiều nhánh.", "suggestion": "thêm test tối thiểu.",
                                        "file": path, "line": 87},
                                       {"severity": "minor", "title": "Gom nhóm theo `projectId` là không cần thiết",
                                        "body": "batchInsert đã bind project_id.", "suggestion": "Gọi thẳng batchInsert.",
                                        "file": path, "line": 125},
                                       {"severity": "nit", "title": "MR chưa có description", "body": "Nên ghi ngắn mục đích."}])
        ws = {"files": [{"path": path, "added": 85, "removed": 40},
                        {"path": "src/main/java/com/fis/etask/module/integration/MassImportTasks.java",
                         "added": 4, "removed": 4}]}
        job = self.job(iid=1153, title="UPDATE import ai log history", source_branch="Update-Import",
                       target_branch="merge/rfc")
        with mock.patch.object(rp, "_ws", return_value=ws):
            note = rp.render_note(job, review, set())
        expect = [
            "## Code Review Summary\n\n**MR:** !1153 - UPDATE import ai log history\n**Reviewer:** AI Agent\n"
            "**Date:** 2026-10-08\n**Commit:** `aaaaaaaa` (source `Update-Import` -> target `merge/rfc`)\n"
            "**Overall Verdict:** 🟢 APPROVE (kèm 1 góp ý Major về test)\n\n> Phạm vi: chỉ đọc code",
            "---\n\n### Overview\n\nok\n\n---\n\n### 🔴 Critical Issues (must fix before merge)\n\n"
            "No critical issues found.\n\n---",
            "### 🟡 Major Suggestions (strongly recommended)\n\n**1. [`TaskHistoryRecorder.java`:87] Chưa có unit test "
            "cho `recordTaskCreationBatch`.**  \nWhy: method mới có nhiều nhánh.  \nSuggestion: thêm test tối thiểu.\n\n---",
            "### 🟢 Minor / Nitpicks\n\n**1. [`TaskHistoryRecorder.java`:125] Gom nhóm theo `projectId` là không cần "
            "thiết.** batchInsert đã bind project_id. Gọi thẳng batchInsert.\n\n**2. MR chưa có description.** "
            "Nên ghi ngắn mục đích.\n\n---",
            "### Standards Compliance\n\n| Category | Status | Notes |",
            "| Security | ✅ | a\\|b |", "| Testing | 🟡 | Chưa có test (xem Major 1) |", "| Correctness | — |  |",
            "### Positive Observations\n\n- Tái sử dụng batchInsert.\n\n---\n\n### Files Reviewed",
            "| `module/task/TaskHistoryRecorder.java` | +85 / -40 | 2 |", "| `module/integration/MassImportTasks.java` | +4 / -4 | 0 |"]
        for e in expect:
            self.assertIn(e, note)
        self.assertTrue(note.startswith(rr.marker(job)))
        self.assertNotIn("AI review ·", note)                                  # no footer in the team layout

    def test_file_labels_stay_unique(self):                                   # Codex layout R1 #2
        rel = rp._files_rel(["one/src/main/java/com/acme/Foo.java", "two/src/main/java/com/acme/Foo.java",
                             "one/src/main/java/com/acme/Bar.java"])
        self.assertEqual(len(set(rel.values())), 3)
        self.assertEqual(rel["one/src/main/java/com/acme/Foo.java"], "one/src/main/java/com/acme/Foo.java")
        self.assertEqual(rp._files_rel(["a\\b\\C.java"]), {"a\\b\\C.java": "a/b/C.java"})
        self.assertEqual(rp._files_rel(["README.md"]), {"README.md": "README.md"})

    def test_why_and_suggestion_are_separate_lines(self):                     # Codex layout R1 #1
        f = [{"severity": "blocker", "title": "t", "body": "b", "suggestion": "```java\nx();\n```"}]
        with mock.patch.object(rp, "_ws", return_value={}):
            note = rp.render_note(self.job(), self.review(findings=f), set())
        self.assertIn("**1. t.**  \nWhy: b  \nSuggestion:\n\n```java\nx();\n```", note)
        f = [{"severity": "major", "title": "t", "body": "```\nlog\n```", "suggestion": "s"}]
        with mock.patch.object(rp, "_ws", return_value={}):
            note = rp.render_note(self.job(), self.review(findings=f), set())
        self.assertIn("**1. t.**  \nWhy:\n\n```\nlog\n```\n\nSuggestion: s", note)

    def test_inline_finding_is_listed_once_in_note(self):
        review = self.review(findings=[{"severity": "major", "title": "t", "body": "secret why", "file": "a.py",
                                        "line": 3}])
        with mock.patch.object(rp, "_ws", return_value={}):
            note = rp.render_note(self.job(), review, {0})
        self.assertIn("chi tiết ở comment inline", note)
        self.assertNotIn("secret why", note)

    def test_defang_neutralises_every_at_except_in_fences(self):
        wj = rp.WORD_JOINER
        self.assertEqual(rp.defang("thiếu dòng trước @Test và @dev"), f"thiếu dòng trước @{wj}Test và @{wj}dev")
        self.assertEqual(rp.defang(rp.defang("@x")), f"@{wj}x")                 # idempotent
        self.assertEqual(rp.defang("x `@Override` y"), f"x `@{wj}Override` y")
        fenced = "```java\n@Test\nvoid a(){}\n```"
        self.assertEqual(rp.defang(fenced), fenced)                       # copied code stays byte-exact
        self.assertEqual(rp.defang("mail a@b.com"), f"mail a@{wj}b.com")   # neutralised too: x_@all is a ping

    def test_defang_survives_markdown_tricks(self):                         # Codex fmt R1 #1
        tricks = ["\\`@all\\`", "text ``` @all", "``@all`", "x`@all", "<code>@all</code>",
                  "- ```\n  @all\n  ```", "> ```\n> @all\n> ```", "~~~\n@all\n~~", "    ```\n@all\n```",
                  # Codex fmt R2 #1 + the same family: constructs that end/negate a fence in GitLab
                  "hello\u000b```\n@all\n```", "- item\n  ```\n  code\noutside\n@all\n```",
                  "```\n@x\r```\n@all", "<!-- ai-review\n```\n-->\n@all\n```",
                  "[a]: /u \"\n```\n\"\n@all\n```", ">>>\n```\n@x\n>>>\n@all\n```",
                  "[a\\]]: /u \"\n```\n\"\n@all\n```", "[a\nb]: /u \"\n```\n\"\n@all\n```",   # Codex fmt R3 #1
                  "$$\n```\n$$\n@all\n```",
                  "notify &#64;all and @&#97;ll", "&#x40;all &#X40;here", "&commat;all",    # Codex fmt R4 #1
                  "_@all_", "__@all__", "*@all*", "x_@all", "~~@all~~", "\\@all", "@al\\_l",  # R5 #1
                  "ping @\\_ops", "@\\-x", "@ all",                                            # R6 #1
                  "[please review](%40victim)", "[r]: %40victim\n\n[x][r]",                    # R7 #1
                  '```json:table\n{"markdown": true, "items": [{"reviewer": "@all"}]}\n```',  # R8 #1
                  "```mermaid\nA-->B: @all\n```", "```suggestion:-0+0\n@all\n```", "```math\n@all\n```",
                  "```java\n@Override\n```\n```json:table\n@x\n```",
                  '```json table\n{"markdown": true, "items": [{"reviewer": "@all"}]}\n```',   # R9 #1
                  'see <a href="&#64victim">review</a>', "x &#0000000064;victim", "&#x40victim",   # R10 #1
                  "intro\n  ```java\ncode\n```\n@all\n```", "- a\n\n    ```java\n    c\n```\n@all\n```",
                  "> ```\n```\n@all\n```", " ~~~\n~~~\n@all\n~~~",                                # R11 #1
                  "intro\n" + "~" * 256 + "\ncode\n" + "~" * 255 + "\n@all\n" + "~" * 256,      # R12 #1
                  "`" * 300 + "\nc\n" + "`" * 255 + "\n@all\n" + "`" * 300]
        for t in tricks:
            out = rp.defang(t)
            self.assertFalse([r for r in rr.content_problems({"author": "dev"}, [out]) if "mention" in r], (t, out))
            # crude model of GitLab's DOM text: entities decoded once, markdown punctuation stripped
            dom = re.sub(r"[*_~`\\]", "", urllib.parse.unquote(html.unescape(rr.outside_fences(out))))
            for m in re.finditer(r"@(?!" + rp.WORD_JOINER + ")", dom):
                self.fail(f"live mention left in {out!r}: …{dom[m.start():m.start() + 8]}")
            probs = rr.content_problems({"author": "dev"}, [out])
            self.assertNotIn("HTML character reference in the text", probs, (t, out))
            self.assertNotIn("percent-encoded @ in the text", probs, (t, out))
        self.assertIn("percent-encoded @ in the text", rr.content_problems({}, ["[x](%40v)"]))
        self.assertEqual(rp.defang("a &amp; b, x &lt; y, R&D, a & b"), "a &amp; b, x &amp;lt; y, R&amp;D, a & b")
        self.assertIn("inline HTML with attributes in the text",
                      rr.content_problems({}, ['see <a href="x">r</a>']))
        self.assertEqual([p for p in rr.content_problems({}, ["`List<String>` và `<T extends Foo>`"]) if "HTML" in p], [])
        self.assertIn("HTML character reference in the text", rr.content_problems({}, ["ping &#64;all"]))

    def test_validate_new_optional_fields(self):
        self.assertEqual(rp.validate(self.job(), self.review(compliance={"Testing": {"status": "ok"}},
                                                             positives=["x"])), [])
        errs = rp.validate(self.job(), self.review(compliance={"Speed": {"status": "ok"},
                                                               "Testing": {"status": "good"},
                                                               "Security": {"status": []}}, positives="x"))
        self.assertEqual(len(errs), 4)                                      # no TypeError (Codex fmt R1 #2)

    def test_files_table_escapes_pipes(self):                               # Codex fmt R1 #3
        with mock.patch.object(rp, "_ws", return_value={"files": [{"path": "src/a|b.py", "added": 1, "removed": 0}]}):
            note = rp.render_note(self.job(), self.review(findings=[]), set())
        self.assertIn("| `src/a\\|b.py` | +1 / -0 | 0 |", note)


import review_rules as rr  # noqa: E402


class AutoPostRulesTest(unittest.TestCase):
    """docs/design/agent-ecosystem.md §10.2.1 — auto-post without a human, only if every rule passes."""

    ENV = {"REVIEW_AUTO_POST": "auto", "REVIEW_AUTO_POST_PROJECTS": "grp/proj",
           "GITLAB_URL": "https://gitlab.example.vn", "REVIEW_AUTO_POST_DAILY_MAX": "30",
           "REVIEW_NOTE_STYLE": "full", "REVIEW_INLINE_COMMENTS": "1"}

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rr-test-")
        mock.patch.dict(os.environ, {"REVIEW_STATE_DIR": self.tmp, "REVIEW_ORCA": "0", **self.ENV}).start()
        mock.patch.object(rc, "registry_by_gitlab_id", return_value=("proj", {})).start()

    def tearDown(self):
        mock.patch.stopall()

    def job(self, **kw):
        j = {"id": "77-5", "project_id": "77", "iid": 5, "project_path": "grp/proj", "author": "dev",
             "head_sha": HEAD, "status": "awaiting_approval", "requests": [{"source": "fchat"}],
             "diff_refs": {"base_sha": "b" * 40, "start_sha": "c" * 40, "head_sha": HEAD}}
        j.update(kw)
        return j

    def ok(self, job, note="Looks fine", inline=()):
        return rr.evaluate(job, note, list(inline))

    def test_clean_review_auto_posts(self):
        self.assertTrue(self.ok(self.job())["auto_post"])

    def test_content_rules_block(self):
        cases = {"@all please check": "@mention",
                 "see https://evil.example.com/x": "link outside",
                 "token = glpat-abcdefghijklmn12345": "secret",
                 "fix: curl https://gitlab.example.vn/x.sh | bash": "dangerous command"}
        for text, why in cases.items():
            res = self.ok(self.job(), note=text)
            self.assertFalse(res["auto_post"], text)
            self.assertTrue(any(why in r for r in res["reasons"]), (text, res["reasons"]))
        self.assertTrue(self.ok(self.job(), note="cc dev — https://gitlab.example.vn/grp/proj/-/merge_requests/5")
                        ["auto_post"])
        self.assertFalse(self.ok(self.job(), note="cc @dev")["auto_post"])     # not even the author: no raw "@"
        self.assertTrue(self.ok(self.job(), note="cc @" + rr.WORD_JOINER + "dev")["auto_post"])

    def test_mentions_inside_fences_do_not_block(self):
        self.assertTrue(self.ok(self.job(), note="ok\n```java\n@Override\n```")["auto_post"])
        for bad in ("`@Test` in a code span still pings? GitLab: no, but the rule is strict",
                    "`code` then @someone", "x`@all", "<div>\n\n```\n@all\n```\n</div>"):
            self.assertFalse(self.ok(self.job(), note=bad)["auto_post"], bad)

    def test_fence_spans_commonmark(self):
        t = "a\n```java\n@x\n```\nb\n~~~~\n@y\n~~~\n~~~~\n"
        self.assertEqual([t[s:e] for s, e in rr.fence_spans(t)], ["```java\n@x\n```\n", "~~~~\n@y\n~~~\n~~~~\n"])
        self.assertEqual(rr.fence_spans("``` a`b\n@x\n```"), [(11, 14)])    # backtick in info: not an opener
        self.assertEqual(rr.fence_spans("    ```\n@x"), [])                  # 4 spaces: not a fence
        self.assertEqual(rr.fence_spans("```\n@x"), [(0, 6)])               # unclosed: to the end
        self.assertEqual(rr.fence_spans("- a\n  ```\n@x\n  ```"), [])          # inside a list: not trusted
        self.assertEqual(rr.fence_spans("```Java\n@x\n```"), [(0, 14)])        # plain language: trusted
        for params in ("```Java title", "```json table", "``` json  table", "```java\tx"):   # Codex fmt R9 #1
            self.assertEqual(rr.fence_spans(params + "\n@x\n```"), [], params)
        self.assertEqual(rr.fence_spans("```java\n@x\n```\n```json:table\n@y\n```"), [])   # one transforming fence
        self.assertEqual(rr.fence_spans("<!-- ai-review:1:ab -->\n```\n@x\n```"), [(24, 34)])   # closed marker ok
        for unsafe in ("<!-- ai-review\n```\n@x\n```", "a\r\n```\n@x\n```", "[r]: /x\n```\n@x\n```"):
            self.assertEqual(rr.fence_spans(unsafe), [], unsafe)

    def test_payload_hash_ignores_file_metadata_and_cells_have_no_cr(self):   # Codex fmt R14 #1, #2
        review = self._review(compliance={"Correctness": {"status": "ok", "note": "first\rsecond\r\nthird"}})
        with mock.patch.object(rp, "_ws", return_value={}), \
             mock.patch("os.path.getmtime", side_effect=[0, 86400 * 3] * 4):
            self.assertEqual(rp.payload_hash(self.job(), review), rp.payload_hash(self.job(), review))
            note = rp.render_note(self.job(), review, set())
        self.assertNotIn("**Date:**", note)
        self.assertIn("| Correctness | ✅ | first second third |", note)
        self.assertEqual(len(rp.validate(self.job(), self._review(date="15/10/2026"))), 1)

    def test_code_only_suggestion_starts_a_real_fence(self):                  # Codex fmt R13 #1
        f = {"severity": "minor", "title": "t", "file": "a.py", "line": 3,
             "suggestion": "```java\n@Override\nvoid f() {}\n```"}
        review = {"head_sha": HEAD, "verdict": "COMMENT", "summary": "ok", "findings": [f]}
        with mock.patch.object(rp, "_ws", return_value={}):
            note, inline = rp.payload(self.job(), review)
        for text in (note, inline[0]):
            self.assertRegex(text, r"Suggestion:(\*\*)?\n\n```java\n@Override\nvoid f\(\) \{\}\n```")
        self.assertEqual(rp._labelled("- **Why:**", " one line "), "- **Why:** one line")

    def test_rendered_note_keeps_fenced_code_exact(self):
        review = {"head_sha": HEAD, "verdict": "COMMENT", "summary": "thiếu @Override",
                  "findings": [{"severity": "minor", "title": "t", "suggestion": "dùng:\n```java\n@Override\nvoid a(){}\n```"}]}
        job = self.job()
        with mock.patch.object(rp, "_ws", return_value={}):
            note = rp.render_note(job, review, set())
        self.assertIn("\n```java\n@Override\n", note)                    # code byte-exact
        self.assertIn("thiếu @" + rp.WORD_JOINER + "Override", note)       # prose neutralised
        self.assertEqual([r for r in rr.content_problems(job, [note]) if "mention" in r or "HTML" in r], [])

    def test_scope_mode_and_source(self):
        self.assertFalse(self.ok(self.job(project_path="other/repo", project_id="9"))["auto_post"])
        self.assertFalse(self.ok(self.job(requests=[{"source": "unknown_bot"}]))["auto_post"])
        with mock.patch.dict(os.environ, {"REVIEW_AUTO_POST": "shadow"}):
            res = self.ok(self.job())
            self.assertTrue(res["rules_passed"])
            self.assertFalse(res["auto_post"])
        with mock.patch.dict(os.environ, {"REVIEW_AUTO_POST": "off"}):
            self.assertFalse(self.ok(self.job())["rules_passed"])

    def test_daily_cap(self):
        posted = [dict(self.job(), id=f"77-{i}", status="posted", auto_posted=True, posted_at=rc.now())
                  for i in range(2)]
        with mock.patch.dict(os.environ, {"REVIEW_AUTO_POST_DAILY_MAX": "2"}), \
                mock.patch.object(rc, "all_jobs", return_value=posted):
            self.assertIn("daily auto-post cap reached", self.ok(self.job())["reasons"])

    def _review(self, **kw):
        r = {"head_sha": HEAD, "verdict": "COMMENT", "summary": "ok",
             "findings": [{"severity": "minor", "title": "t", "file": "a.py", "line": 3}]}
        r.update(kw)
        return r

    def test_rules_approval_rechecked_at_post_time(self):
        job = self.job(approval={"via": "rules"})
        with mock.patch.object(rp, "load_review", return_value=self._review()):
            self.assertEqual(rp.approval_status(job), "approved")
        with mock.patch.object(rp, "load_review", return_value=self._review(summary="see https://evil.example.com/x")):
            self.assertEqual(rp.approval_status(job), "pending")
        with mock.patch.object(rp, "load_review", return_value=self._review(summary="ping @all")):
            self.assertEqual(rp.approval_status(job), "approved")      # rendered as `@all`: pings nobody

    def _post_with(self, gl, job):
        import contextlib
        rc.save_job(job)
        stale = {"stale": False}
        with mock.patch.object(rp, "load_review", return_value=self._review()), \
                mock.patch.object(rc, "gitlab_project", return_value=contextlib.nullcontext(gl)), \
                mock.patch.object(rp, "_ws", return_value={}), \
                mock.patch.dict(sys.modules, {"review_ws": mock.Mock(check_stale=lambda j: stale,
                                                                     ledger_add=lambda *a: None)}):
            return rp.post(job["id"], notify=False)

    def test_reconcile_skips_when_marker_already_on_mr(self):
        gl = mock.Mock()
        gl._api.side_effect = lambda p: p
        gl._request.return_value = [{"body": rr.marker(self.job()) + "\n### old"}]
        out = self._post_with(gl, self.job(approval={"via": "rules"}))
        self.assertTrue(out.get("reconciled"))
        gl.add_mr_comment.assert_not_called()

    def test_inline_not_reposted_after_crash(self):
        gl = mock.Mock()
        gl._api.side_effect = lambda p: p
        gl._request.side_effect = lambda url, **kw: [] if "notes" in url else {"id": 1}
        gl.add_mr_comment.return_value = {"id": 9}
        job = self.job(approval={"via": "rules"}, inline_done={HEAD: [0]})
        out = self._post_with(gl, job)
        self.assertTrue(out["posted"])
        posted_inline = [c for c in gl._request.call_args_list if "discussions" in str(c)]
        self.assertEqual(posted_inline, [])                 # finding 0 was already posted inline
        note = gl.add_mr_comment.call_args[0][1]
        self.assertIn(rr.marker(job), note)
        self.assertIn("## Code Review Summary", note)

    def test_uncertain_inline_stops_then_reconciles_by_marker(self):          # Codex R.M4 R1 #2
        gl = mock.Mock()
        gl._api.side_effect = lambda p: p
        state = {"posted": []}

        def req(url, method="GET", data=None):
            if "notes" in url:
                return []
            if method == "POST":
                state["posted"].append(data["body"])
                return {"error": True, "status": None, "uncertain": True, "message": "timed out"}
            return [{"notes": [{"body": b} for b in state["posted"]]}]      # GitLab DID create it
        gl._request.side_effect = req
        gl.add_mr_comment.return_value = {"id": 9}
        job = self.job(approval={"via": "rules"})
        with self.assertRaises(RuntimeError):
            self._post_with(gl, job)
        self.assertTrue(rc.load_job(job["id"])["inline_uncertain"][HEAD])
        gl._request.side_effect = lambda url, method="GET", data=None: (
            [] if "notes" in url else [{"notes": [{"body": b} for b in state["posted"]]}] if method == "GET"
            else self.fail("inline must not be posted twice"))
        out = self._post_with(gl, rc.load_job(job["id"]))
        self.assertTrue(out["posted"])
        self.assertEqual(out["inline"], 1)                                   # recognised as already posted
        self.assertEqual(len(state["posted"]), 1)

    def test_reconcile_reads_every_page_and_any_index(self):                  # Codex R.M4 R2
        job = self.job()
        pages = {1: [{"notes": [{"body": "unrelated"}]}] * 100,
                 2: [{"notes": [{"body": "x\n\n" + rp.inline_marker(job, 250)}]}]}
        gl = mock.Mock()
        gl._api.side_effect = lambda p: p
        gl._request.side_effect = lambda url, **kw: pages.get(int(url.rsplit("page=", 1)[1]), [])
        self.assertEqual(rp.inline_already_posted(gl, job), {250})
        gl._request.side_effect = lambda url, **kw: (pages[1] if url.endswith("page=1") else
                                                     {"error": True, "status": 500, "message": "boom"})
        with self.assertRaises(RuntimeError):                                  # a partial read is not "absent"
            rp.inline_already_posted(gl, job)

    def test_unreadable_notes_never_mean_not_posted(self):                     # Codex R.M4 R1 #1
        gl = mock.Mock()
        gl._api.side_effect = lambda p: p
        gl._request.return_value = {"error": True, "status": "bad-body", "message": "empty response body"}
        with self.assertRaises(RuntimeError):
            self._post_with(gl, self.job(approval={"via": "rules"}))
        gl.add_mr_comment.assert_not_called()

    def test_note_without_id_is_not_posted(self):                              # Codex R.M4 R1 #1
        gl = mock.Mock()
        gl._api.side_effect = lambda p: p
        gl._request.side_effect = lambda url, **kw: [] if "notes" in url else {"id": 1}
        gl.add_mr_comment.return_value = {}
        job = self.job(approval={"via": "rules"})
        with self.assertRaises(RuntimeError):
            self._post_with(gl, job)
        self.assertNotEqual(rc.load_job(job["id"]).get("status"), "posted")


class CompactNoteTest(unittest.TestCase):
    """REVIEW_NOTE_STYLE=compact (opt-in) and inline comments off by default."""

    def setUp(self):
        p = mock.patch.dict(os.environ)
        p.start()
        self.addCleanup(p.stop)
        os.environ.pop("REVIEW_INLINE_COMMENTS", None)                       # default: off
        os.environ["REVIEW_NOTE_STYLE"] = "compact"                          # not the default since 2026-10-16
        w = mock.patch.object(rp, "_ws", return_value={"commits": {"new": 2, "total": 5},
                                                       "files": [{"path": "a.py", "added": 1, "removed": 0}]})
        w.start()
        self.addCleanup(w.stop)

    def job(self):
        return {"id": "77-5", "project_id": "77", "iid": 5, "author": "dev", "source_branch": "f",
                "target_branch": "dev", "diff_refs": {"head_sha": HEAD}}

    def review(self, findings):
        return {"head_sha": HEAD, "verdict": "REQUEST_CHANGES", "summary": "Tóm tắt ngắn.", "findings": findings,
                "compliance": {"Testing": {"status": "fail", "note": "x"}}, "positives": ["tốt"],
                "tests": [{"cmd": "mvn test", "result": "fail"}, {"cmd": "lint", "result": "pass"}]}

    def test_compact_is_short_and_has_no_tables(self):
        f = [{"severity": "blocker", "title": "Sai truy vấn", "body": "Trả task.", "suggestion": "Dùng port.",
              "file": "A.java", "line": 9},
             {"severity": "nit", "title": "Thiếu dòng trống", "body": "dài dòng", "suggestion": "thêm"}]
        note = rp.render_note(self.job(), self.review(f), set())
        self.assertTrue(note.startswith(rr.marker(self.job())))
        self.assertIn("### 🤖 Code Review — 🔴 REQUEST_CHANGES", note)
        self.assertIn("1. [`A.java`:9] **Sai truy vấn** — Trả task.\n   ↳ _Sửa:_ Dùng port.", note)
        self.assertIn("2. Thiếu dòng trống", note)
        self.assertNotIn("dài dòng", note)                                   # nit: title only
        for gone in ("Standards Compliance", "Positive Observations", "Files Reviewed", "🟡 Nên sửa", "lint"):
            self.assertNotIn(gone, note)                                      # no tables, no empty section
        self.assertIn("**Test lỗi:** `mvn test`", note)
        self.assertLess(len(note.splitlines()), 20)

    def test_inline_is_off_by_default_and_on_when_set(self):
        f = [{"severity": "major", "title": "t", "file": "a.py", "line": 3}]
        self.assertEqual(rp.inline_candidates(self.review(f)), [])
        self.assertEqual(rp.payload(self.job(), self.review(f))[1], [])
        with mock.patch.dict(os.environ, {"REVIEW_INLINE_COMMENTS": "1"}):
            self.assertEqual(rp.inline_candidates(self.review(f)), [0])

    def test_compact_code_suggestion_keeps_a_real_fence_and_no_ping(self):
        f = [{"severity": "major", "title": "Thiếu @Override", "body": "x",
              "suggestion": "```java\n@Override\nvoid a(){}\n```"}]
        note = rp.render_note(self.job(), self.review(f), set())
        self.assertIn("↳ _Sửa:_\n\n```java\n@Override\n", note)
        self.assertIn("Thiếu @" + rp.WORD_JOINER + "Override", note)
        self.assertEqual([r for r in rr.content_problems(self.job(), [note]) if "mention" in r or "HTML" in r], [])

    def test_long_prose_is_clipped_code_never(self):
        long = "Câu một rất dài " + "x " * 80 + ". Câu hai " + "y " * 100
        out = rp._clip(long)
        self.assertLessEqual(len(out), rp.COMPACT_MAX + 2)
        self.assertTrue(out.endswith(" …"))
        self.assertEqual(rp._clip("ngắn."), "ngắn.")
        code = "```java\n" + "a();\n" * 200 + "```"
        self.assertEqual(rp._clip(code), code)
        self.assertEqual(rp._clip("dùng `" + "z" * 300 + "` nhé").count("`") % 2, 0)   # no open code span
        f = [{"severity": "major", "title": "t", "body": long, "file": "src/main/java/a/b/C.java", "line": 3}]
        note = rp.render_note(self.job(), self.review(f), set())
        self.assertIn("1. [`C.java`:3] **t**", note)
        self.assertNotIn("src/main/java", note)

    def test_clip_never_cuts_inside_spans_links_or_bold(self):                 # Codex compact R1 #2
        for text in ("Use ``a " + "x " * 150 + "`` then more " + "w " * 50,
                     "Xem [tài liệu " + "d " * 150 + "](https://gitlab.example.vn/x) " + "w " * 50,
                     "Gọi https://gitlab.example.vn/" + "p" * 300 + " rồi " + "w " * 50,
                     "**rất quan trọng " + "b " * 150 + "** và " + "w " * 50):
            out = rp._clip(text)
            body = out[:-2]
            for s, e in rp._protected(text):
                self.assertFalse(s < len(body) < e, (text[:30], out[-40:]))
            self.assertEqual(body.count("**") % 2, 0)
        self.assertEqual(rp._clip("x" * 400), "x" * 400)                       # no break point: whole
        out = rp._clip("Use `**` literally. " + "word " * 80)                   # Codex compact R2 #1
        self.assertTrue(out.startswith("Use `**` literally."), out[:40])
        self.assertEqual(out[:-2].count("`") % 2, 0)
        out = rp._clip("Dùng `a**b` và **đậm " + "w " * 150)
        self.assertTrue(out.startswith("Dùng `a**b` và **đậm"), out[:40])
        self.assertTrue(out.endswith("** …"))                                   # closed, content kept
        out = rp._clip("**Never retry this operation because it charges twice. " + "Charges are final. " * 15 + "**")
        self.assertTrue(out.startswith("**Never retry this operation"), out[:40])  # Codex compact R3 #1
        self.assertTrue(out.endswith("** …"))
        self.assertGreater(len(out), 100)

    def test_short_names_are_unique(self):                                     # Codex compact R1 #3
        names = rp._short_names(["api/models/user.py", "worker/models/user.py", "src/a/C.java", "x/D.java"])
        self.assertEqual(names["api/models/user.py"], "api/models/user.py")
        self.assertEqual(names["worker/models/user.py"], "worker/models/user.py")
        self.assertEqual(names["src/a/C.java"], "C.java")
        names = rp._short_names(["a/models/user.py", "a/views/user.py"])
        self.assertEqual(set(names.values()), {"models/user.py", "views/user.py"})

    def test_inline_done_is_ignored_when_inline_is_off(self):                  # Codex compact R1 #1
        import contextlib
        f = [{"severity": "major", "title": "t", "body": "chi tiết", "file": "a.py", "line": 3}]
        job = {**self.job(), "inline_done": {HEAD: [0]}, "approval": {"via": "rules"}, "status": "awaiting_approval"}
        gl = mock.Mock()
        gl._api.side_effect = lambda p: p
        gl._request.return_value = []                                         # no marker on the MR yet
        gl.add_mr_comment.return_value = {"id": 9}
        ws = mock.Mock(check_stale=lambda j: {"stale": False}, ledger_add=lambda *a: None)
        with mock.patch.object(rc, "load_job", return_value=job), mock.patch.object(rc, "save_job"),              mock.patch.object(rc, "log_event"), mock.patch.object(rc, "board_card"),              mock.patch.object(rp, "approval_status", return_value="approved"),              mock.patch.object(rp, "load_review", return_value=self.review(f)),              mock.patch.object(rc, "gitlab_project", return_value=contextlib.nullcontext(gl)),              mock.patch.dict(sys.modules, {"review_ws": ws}):
            out = rp._post_locked("77-5", dry_run=False, notify=False)
        self.assertTrue(out["posted"])
        note = gl.add_mr_comment.call_args[0][1]
        self.assertIn("chi tiết", note)                                       # the finding in full
        self.assertNotIn("xem comment inline", note)

    def test_style_full_and_unknown_value(self):
        with mock.patch.dict(os.environ, {"REVIEW_NOTE_STYLE": "full"}):
            self.assertIn("## Code Review Summary", rp.render_note(self.job(), self.review([]), set()))
        with mock.patch.dict(os.environ, {"REVIEW_NOTE_STYLE": "weird"}):
            self.assertEqual(rp.note_style(), "full")
        os.environ.pop("REVIEW_NOTE_STYLE")
        self.assertEqual(rp.note_style(), "full")                              # the default


class NotifyTest(unittest.TestCase):
    """Requester notices after a post: per-recipient state saved after every step, seeded in the same save
    as "posted", failures retried with per-recipient backoff, unconfirmed/uncertain never auto re-sent."""

    OK = '{"sent": true, "confirmed": true}'

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="notify-test-")
        p = mock.patch.dict(os.environ, {"REVIEW_STATE_DIR": self.tmp, "REVIEW_ORCA": "0",
                                         "FCHAT_BEARER_TOKEN": "stale-token-of-the-parent",
                                         "REVIEW_CLOSE_WORKER_TERMINAL": "0"})
        p.start()
        self.addCleanup(p.stop)
        self.review = {"head_sha": HEAD, "verdict": "COMMENT", "summary": "ok",
                       "findings": [{"severity": "major", "title": "t"}]}
        r = mock.patch.object(rp, "load_review", return_value=self.review)
        r.start()
        self.addCleanup(r.stop)

    def job(self, people=("U1",), **kw):
        j = {"id": "77-5", "project_id": "77", "iid": 5, "project_path": "g/p", "web_url": "https://x/5",
             "status": "posted", "head_sha": HEAD, "review_file": "r.json",
             "requests": [{"source": "fchat", "group": "G1", "group_type": "SUPER_PRIVATE", "by": f"P{u}", "by_id": u}
                          for u in people] + [{"source": "fchat", "group": "G1", "by": "PU1", "by_id": "U1"},
                                              {"source": "manual"}]}
        j.update(kw)
        rp.seed_notices(j)
        rc.save_job(j)
        return j

    def res(self, code, out="", err=""):
        return mock.Mock(returncode=code, stdout=out, stderr=err)

    def notices(self):
        return rc.load_job("77-5")["notices"]

    def test_sent_once_with_fresh_credentials(self):
        self.job()
        with mock.patch.object(rp.subprocess, "run", return_value=self.res(0, self.OK)) as run:
            out = rp.deliver_notices("77-5", self.review)
            self.assertEqual([x["state"] for x in out], ["sent"])            # one notice per person
            self.assertNotIn("FCHAT_BEARER_TOKEN", run.call_args.kwargs["env"])
            self.assertEqual(rp.deliver_notices("77-5", self.review), [])      # never twice
        self.assertEqual(self.notices()["G1|U1"]["state"], "sent")

    def test_unconfirmed_is_not_sent_and_never_auto_resent(self):              # Codex notify R1 #1
        self.job()
        with mock.patch.object(rp.subprocess, "run", return_value=self.res(0, '{"sent": true, "confirmed": false}')):
            rp.deliver_notices("77-5", self.review)
        self.assertEqual(self.notices()["G1|U1"]["state"], "unconfirmed")
        with mock.patch.object(rp.subprocess, "run") as run, \
             mock.patch.object(rc, "now", return_value=rc.now() + 100000):
            rp.sweep()
            run.assert_not_called()
        with mock.patch.object(rp.subprocess, "run", return_value=self.res(0, self.OK)):
            out = rp.notify_again("77-5")                                     # a human decides
        self.assertEqual([x["state"] for x in out["notified"]], ["sent"])

    def test_pending_is_seeded_with_posted_and_delivered_by_sweep(self):        # Codex notify R1 #2
        self.job()                                                            # posted, nothing sent (crash)
        self.assertEqual(self.notices()["G1|U1"]["state"], "pending")
        with mock.patch.object(rp.subprocess, "run", return_value=self.res(0, self.OK)):
            acts = rp.sweep()["actions"]
        self.assertEqual(acts[0]["notices"], [{"state": "sent", "error": ""}])

    def test_dead_sender_becomes_uncertain_not_resent(self):                   # Codex notify R1 #3
        self.job()
        j = rc.load_job("77-5")
        j["notices"]["G1|U1"].update(state="sending", at=rc.now() - rp.NOTIFY_SENDING_STALE_S - 5, attempts=1)
        rc.save_job(j)
        with mock.patch.object(rp.subprocess, "run") as run:
            rp.sweep()
            run.assert_not_called()
        self.assertEqual(self.notices()["G1|U1"]["state"], "uncertain")

    def test_backoff_and_max_attempts_are_per_recipient(self):                 # Codex notify R1 #4
        self.job(people=("U1", "U2"))
        j = rc.load_job("77-5")
        j["notices"]["G1|U1"].update(state="failed", attempts=rp.NOTIFY_MAX_ATTEMPTS, at=0)   # given up
        j["notices"]["G1|U2"].update(state="failed", attempts=1, at=0)                       # due
        rc.save_job(j)
        with mock.patch.object(rp.subprocess, "run", return_value=self.res(0, self.OK)) as run:
            rp.sweep()
        self.assertEqual(run.call_count, 1)
        self.assertIn("U2", run.call_args.args[0])
        n = self.notices()
        self.assertEqual((n["G1|U1"]["state"], n["G1|U2"]["state"]), ("failed", "sent"))

    def test_failure_reason_is_redacted_before_truncation(self):               # Codex notify R1 #6
        self.job()
        tok = "eyJhbGciOiJIUzI1NiJ9." + "a" * 300 + ".sig"
        msg = '{"error": true, "message": "' + "x " * 90 + tok + '"}'
        with mock.patch.object(rp.subprocess, "run", return_value=self.res(1, msg)):
            rp.deliver_notices("77-5", self.review)
        n = self.notices()["G1|U1"]
        self.assertEqual((n["state"], n["attempts"]), ("failed", 1))
        self.assertNotIn("eyJhbGci", n["error"])
        self.assertNotIn("aaaa", n["error"])
        self.assertLessEqual(len(n["error"]), 200)

    def test_closing_a_worker_tab_does_not_overwrite_notices(self):            # Codex notify R1 #5
        self.job(worker_terminal="term_x", updated=0)
        snapshot = {**rc.load_job("77-5"), "updated": 0}                      # what close_idle_workers lists (old)
        j = rc.load_job("77-5")
        j["notices"]["G1|U1"]["state"] = "sent"                               # meanwhile: another process delivered
        rc.save_job(j)
        with mock.patch.dict(os.environ, {"REVIEW_CLOSE_WORKER_TERMINAL": "1"}), \
             mock.patch.object(rc, "orca", return_value=mock.Mock()), \
             mock.patch.object(rc, "all_jobs", return_value=[snapshot]):
            out = rp.close_idle_workers()
        self.assertEqual(out[0]["closed_terminal"], "term_x")
        saved = rc.load_job("77-5")
        self.assertEqual(saved["notices"]["G1|U1"]["state"], "sent")
        self.assertTrue(saved["worker_terminal_closed"])

    def test_timeout_is_uncertain_not_retried(self):                          # Codex notify R2 #1
        self.job()
        with mock.patch.object(rp.subprocess, "run", side_effect=rp.subprocess.TimeoutExpired("x", 60)):
            rp.deliver_notices("77-5", self.review)
        self.assertEqual(self.notices()["G1|U1"]["state"], "uncertain")
        with mock.patch.object(rp.subprocess, "run") as run, \
             mock.patch.object(rc, "now", return_value=rc.now() + 100000):
            rp.sweep()
            run.assert_not_called()

    def test_a_requeued_job_is_never_overwritten(self):                       # Codex notify R2 #2
        self.job(people=("U1", "U2"), note_id=7)

        def requeue_while_sending(*a, **kw):                                  # add_job replaces the job
            rc.save_job({"id": "77-5", "project_id": "77", "iid": 5, "status": "queued", "head_sha": "b" * 40,
                          "requests": [{"source": "fchat", "group": "G2", "by_id": "U9"}]})
            return self.res(0, self.OK)
        with mock.patch.object(rp.subprocess, "run", side_effect=requeue_while_sending) as run:
            rp.deliver_notices("77-5", self.review)
        self.assertEqual(run.call_count, 1)                                   # stopped: delivery obsolete
        saved = rc.load_job("77-5")
        self.assertEqual(saved["status"], "queued")
        self.assertEqual(saved["requests"][0]["group"], "G2")
        self.assertNotIn("notices", saved)

    def test_a_crashed_child_is_uncertain_a_structured_error_is_failed(self):  # Codex notify R3 #1
        self.job(people=("U1", "U2"))
        outs = iter([self.res(1, "", "Traceback ...\nConnectionResetError: x"),
                     self.res(1, '{"error": true, "message": "could not resolve groupType"}')])
        with mock.patch.object(rp.subprocess, "run", side_effect=lambda *a, **k: next(outs)):
            rp.deliver_notices("77-5", self.review)
        n = self.notices()
        self.assertEqual((n["G1|U1"]["state"], n["G1|U2"]["state"]), ("uncertain", "failed"))
        self.assertIn("groupType", n["G1|U2"]["error"])

    def test_posting_keeps_a_requester_added_meanwhile(self):                 # Codex notify R3 #2
        self.job(status="running")
        mine = rc.load_job("77-5")
        cur = rc.load_job("77-5")
        cur["requests"].append({"source": "fchat", "group": "G1", "by": "Late", "by_id": "U7"})
        rc.save_job(cur)                                                     # add_job, while GitLab was called
        mine.update(status="posted", note_id=3)
        rp._save_posted(mine, notify=True)
        saved = rc.load_job("77-5")
        self.assertIn("U7", [r.get("by_id") for r in saved["requests"]])
        self.assertEqual(saved["notices"]["G1|U7"]["state"], "pending")

    def test_every_posting_save_keeps_requesters_added_meanwhile(self):       # Codex notify R4 #1
        self.job(status="running")
        mine = rc.load_job("77-5")
        cur = rc.load_job("77-5")
        cur["requests"].append({"source": "fchat", "group": "G1", "by": "Late", "by_id": "U7"})
        rc.save_job(cur)
        mine["inline_done"] = {HEAD: [0]}
        rp._save_merged(mine, sync=False)                                     # an intermediate (inline) save
        self.assertIn("U7", [r.get("by_id") for r in rc.load_job("77-5")["requests"]])
        import inspect
        for fn in (rp._post_locked, rp._inline):
            self.assertNotIn("rc.save_job(", inspect.getsource(fn))         # no raw save left on this path

    def test_dashboard_lists_open_notices(self):
        self.job()
        j = rc.load_job("77-5")
        j["notices"]["G1|U1"].update(state="failed", attempts=2, error="timeout")
        rc.save_job(j)
        sys.path.insert(0, os.path.join(os.path.dirname(rp.__file__), "..", "..", "dev-automation", "tools"))
        import dashboard
        jobs = dashboard.read_reviews()["jobs"]
        self.assertEqual(jobs[0]["notify_open"], [{"state": "failed", "error": "timeout", "attempts": 2}])

    def test_guard_asks_before_notify(self):
        sys.path.insert(0, os.path.join(os.path.dirname(rp.__file__), "..", "..", "..", "hooks"))
        import guard_write
        self.assertEqual(guard_write.decide({"tool_name": "Bash",
                                             "tool_input": {"command": "python review_post.py notify 77-5"}})[0], "ask")


class GuardHookTest(unittest.TestCase):
    HOOK = os.path.join(_ROOT, ".claude", "hooks", "guard_write.py")

    def decide(self, event, **env):
        e = {k: v for k, v in os.environ.items()
             if k not in ("TOOLKIT_HEADLESS", "CLAUDE_TG_BRIDGE", "TOOLKIT_GUARD_ASK")}
        e.update(env)
        p = subprocess.run([sys.executable, self.HOOK], input=json.dumps(event), capture_output=True,
                           text=True, env=e)
        out = p.stdout.strip()
        return json.loads(out)["hookSpecificOutput"]["permissionDecision"] if out else None

    def bash(self, cmd, **env):
        return self.decide({"tool_name": "Bash", "tool_input": {"command": cmd}}, **env)

    def test_decisions(self):
        self.assertEqual(self.bash("python3 gitlab_api.py merge-mr 3"), "ask")
        self.assertEqual(self.bash("python3 gitlab_api.py merge-mr 3", TOOLKIT_HEADLESS="1"), "deny")
        self.assertEqual(self.bash("python3 review_post.py approve 1-2"), "deny")
        self.assertIsNone(self.bash("python3 gitlab_api.py mr-detail 3"))
        self.assertIsNone(self.bash("python3 review_post.py request-approval 1-2"))
        self.assertEqual(self.bash("cat .env"), "ask")
        self.assertIsNone(self.bash("python3 gitlab_api.py merge-mr 3", CLAUDE_TG_BRIDGE="1"))
        self.assertIsNone(self.bash("python3 gitlab_api.py merge-mr 3", TOOLKIT_GUARD_ASK="off"))
        self.assertEqual(self.bash("python3 review_post.py approve 1-2", TOOLKIT_GUARD_ASK="off"), "deny")
        self.assertEqual(self.decide({"tool_name": "Write",
                                      "tool_input": {"file_path": "/r/temp/review_queue/jobs/1-2.json"}}), "deny")

    def ps(self, cmd, **env):
        return self.decide({"tool_name": "PowerShell", "tool_input": {"command": cmd}}, **env)

    def test_bypasses_found_in_review_are_closed(self):
        # via the dispatcher, plain and skill-qualified
        self.assertEqual(self.bash("python agentkit.py gitlab_api create-mr a b"), "ask")
        self.assertEqual(self.bash("python agentkit.py dev-automation:gitlab_api merge-mr 1"), "ask")
        self.assertIsNone(self.bash("python agentkit.py gitlab_api mr-detail 1"))
        # PowerShell tool
        self.assertEqual(self.ps("python gitlab_api.py merge-mr 1"), "ask")
        self.assertEqual(self.ps("Get-Content .env"), "ask")
        self.assertEqual(self.ps("python gitlab_api.py merge-mr 1", TOOLKIT_HEADLESS="1"), "deny")
        # copying / grepping secrets
        self.assertEqual(self.bash("cp .env /tmp/x"), "ask")
        self.assertEqual(self.decide({"tool_name": "Grep", "tool_input": {"pattern": "TOKEN", "path": "D:\\r\\.env"}}), "ask")
        self.assertIsNone(self.bash("cat .env.sample"))
        self.assertEqual(self.decide({"tool_name": "Read",
                                      "tool_input": {"file_path": "C:\\Users\\u\\AppData\\Roaming\\orca\\orca-runtime.json"}}),
                         "ask")
        # forging review / approval state through a shell
        self.assertEqual(self.bash("python -c \"import review_common as rc; j=rc.load_job('1-2'); rc.save_job(j)\""), "deny")
        self.assertEqual(self.bash("python -c \"import approvals; approvals.decide('x', True)\""), "deny")
        self.assertEqual(self.ps("Set-Content temp\\review_queue\\jobs\\1-2.json '{}'"), "deny")
        self.assertEqual(self.decide({"tool_name": "Edit",
                                      "tool_input": {"file_path": "D:\\r\\temp\\tg_approvals\\a.json"}}), "deny")
        # reading help is not resolving a gate
        self.assertIsNone(self.bash("orca orchestration gate-resolve --help"))
        self.assertEqual(self.bash("orca orchestration gate-resolve --id g --resolution ok"), "deny")


class RoleRoutingTest(unittest.TestCase):
    def test_fallback_on_rate_limit_and_avoid(self):
        import agent_runner as ar
        cfg = {"roles": {"review": ["claude:sonnet", "codex"]}, "limits": {}}
        calls = []

        def fake(prompt, **k):
            calls.append(k["backend"])
            if k["backend"] == "claude":
                raise ar.AgentRunError("usage limit reached")
            return "ok"

        with mock.patch.object(ar, "_agents_config", return_value=cfg), \
             mock.patch.object(ar, "run_turn", side_effect=fake):
            self.assertEqual(ar.run_role("p", "review")["used"], "codex")
            self.assertEqual(calls, ["claude", "codex"])
            calls.clear()
            with self.assertRaises(ar.AgentRunError):      # codex avoided, claude rate-limited
                ar.run_role("p", "review", avoid=("codex",))
            self.assertEqual(calls, ["claude"])

    def test_non_retryable_error_raises(self):
        import agent_runner as ar
        cfg = {"roles": {"review": ["claude", "codex"]}}
        with mock.patch.object(ar, "_agents_config", return_value=cfg), \
             mock.patch.object(ar, "run_turn", side_effect=ar.AgentRunError("syntax error in prompt")):
            with self.assertRaises(ar.AgentRunError):
                ar.run_role("p", "review")


if __name__ == "__main__":
    unittest.main()
