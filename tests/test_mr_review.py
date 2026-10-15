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
        self.assertIn("**1. [`a.py`:3] t**", note)
        self.assertIn("No critical issues found.", note)

    def test_render_note_uses_code_review_summary_layout(self):
        review = self.review(verdict="REQUEST_CHANGES", date="2026-10-15", positives=["gọn"],
                             compliance={"Correctness": {"status": "fail", "note": "a|b"}},
                             findings=[{"severity": "nit", "title": "n"},
                                       {"severity": "blocker", "title": "b", "body": "why", "suggestion": "fix",
                                        "file": "x.java", "line": 9},
                                       {"severity": "major", "title": "m"}])
        ws = {"files": [{"path": "x.java", "added": 3, "removed": 1}, {"path": "img.png", "added": None}]}
        job = self.job(title="T", source_branch="f", target_branch="dev")
        with mock.patch.object(rp, "_ws", return_value=ws):
            note = rp.render_note(job, review, set())
        self.assertTrue(note.startswith(rr.marker(job)))
        order = [note.index(s) for s in ("## Code Review Summary", "**Overall Verdict:** 🔴 REQUEST_CHANGES",
                                         "### Overview", "### 🔴 Critical", "**1. [`x.java`:9] b**",
                                         "- **Why:** why", "- **Suggestion:** fix", "### 🟡 Major", "**2. m**",
                                         "### 🟢 Minor / Nitpicks", "**3. n**", "### Standards Compliance",
                                         "### Positive Observations", "### Files Reviewed")]
        self.assertEqual(order, sorted(order))
        self.assertIn("**Date:** 2026-10-15", note)
        self.assertIn("| Correctness | ❌ | a\\|b |", note)
        self.assertIn("| Security | — |  |", note)
        self.assertIn("| `x.java` | +3 / -1 | 1 |", note)
        self.assertIn("| `img.png` | binary | 0 |", note)
        self.assertIn("(`f` -> `dev`)", note)

    def test_inline_finding_is_listed_once_in_note(self):
        review = self.review(findings=[{"severity": "major", "title": "t", "body": "secret why", "file": "a.py",
                                        "line": 3}])
        with mock.patch.object(rp, "_ws", return_value={}):
            note = rp.render_note(self.job(), review, {0})
        self.assertIn("Chi tiết ở comment inline", note)
        self.assertNotIn("secret why", note)

    def test_defang_neutralises_mentions_except_fences_and_author(self):
        wj = rp.WORD_JOINER
        self.assertEqual(rp.defang("thiếu dòng trước @Test và @dev", "dev"), f"thiếu dòng trước @{wj}Test và @dev")
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
                  "_@all_", "__@all__", "*@all*", "x_@all", "~~@all~~", "\\@all", "@al\\_l"]  # R5 #1
        for t in tricks:
            out = rp.defang(t)
            self.assertFalse([r for r in rr.content_problems({"author": "dev"}, [out]) if "mention" in r], (t, out))
            # crude model of GitLab's DOM text: entities decoded once, markdown punctuation stripped
            dom = re.sub(r"[*_~`\\]", "", html.unescape(rr.outside_fences(out)))
            for m in re.finditer(r"@(?=[A-Za-z0-9_])", dom):
                self.fail(f"live mention left in {out!r}: …{dom[m.start():m.start() + 8]}")
            self.assertNotIn("HTML character reference in the text",
                             rr.content_problems({"author": "dev"}, [out]), (t, out))
        self.assertEqual(rp.defang("a &amp; b, x &lt; y"), "a &amp; b, x &amp;lt; y")
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
           "GITLAB_URL": "https://gitlab.example.vn", "REVIEW_AUTO_POST_DAILY_MAX": "30"}

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
        self.assertTrue(self.ok(self.job(), note="cc @dev — https://gitlab.example.vn/grp/proj/-/merge_requests/5")
                        ["auto_post"])

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
        self.assertEqual(rr.fence_spans("<!-- ai-review:1:ab -->\n```\n@x\n```"), [(24, 34)])   # closed marker ok
        for unsafe in ("<!-- ai-review\n```\n@x\n```", "a\r\n```\n@x\n```", "[r]: /x\n```\n@x\n```"):
            self.assertEqual(rr.fence_spans(unsafe), [], unsafe)

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
        self.assertIn("AI review", note)

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


class GuardHookTest(unittest.TestCase):
    HOOK = os.path.join(_ROOT, ".claude", "hooks", "guard_write.py")

    def decide(self, event, **env):
        e = {k: v for k, v in os.environ.items() if k not in ("TOOLKIT_HEADLESS", "CLAUDE_TG_BRIDGE")}
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
