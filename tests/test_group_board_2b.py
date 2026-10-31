"""Group workboard phase 2b (docs/design/group-workboard.md §5.3): env change notices — what counts as a change,
MRs added / lost, the E card (golden-parsed by the extension), the daily digest, and env_watch.tick (shadow by
default, baseline first, the baseline moves only after delivery)."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".claude" / "skills" / "_runtime"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from agent_skill_toolkit.workflows.group_board import envwatch  # noqa: E402

EXT = Path(os.environ.get("FQR_EXT_DIR") or ROOT.parent / "fpt-chat-quick-reply")
PREV = {"env": "sandbox", "version": "0.197.45", "commit": "a" * 40, "digest": "aaa", "level": "verified"}
CUR = {"env": "sandbox", "version": "0.198.45", "commit": "b" * 40, "digest": "bbb", "level": "verified",
       "rs_created_at": "2026-10-29T18:16:42Z", "build": {"url": "http://j/282/"}, "notes": []}


class ChangedTest(unittest.TestCase):
    def test_changed(self):
        self.assertTrue(envwatch.changed(PREV, CUR))
        self.assertFalse(envwatch.changed(PREV, {**PREV, "level": "inferred"}))          # level alone: no news
        self.assertFalse(envwatch.changed(PREV, {"env": "sandbox", "level": "unknown", "version": "", "commit": ""}))
        self.assertFalse(envwatch.changed(PREV, {**PREV, "version": "0.198.45", "level": "unknown", "commit": ""}))
        self.assertTrue(envwatch.changed(PREV, {**PREV, "digest": "ccc"}))              # same tag re-pushed
        self.assertFalse(envwatch.changed(None, CUR))


class MrsBetweenTest(unittest.TestCase):
    def test_mrs(self):
        commits = [{"id": f"c{i}"} for i in range(3)]
        by = {"c0": [{"iid": 5, "state": "merged", "target_branch": "rfc", "title": "A"}],
              "c1": [{"iid": 5, "state": "merged", "target_branch": "rfc"}, {"iid": 6, "state": "opened"}],
              "c2": [{"iid": 7, "state": "merged", "target_branch": "develop"}]}
        out = envwatch.mrs_between("a", "b", lambda f, t: commits, lambda s: by[s], "rfc")
        self.assertEqual([m["iid"] for m in out["mrs"]], [5])                           # merged, right branch, once
        self.assertFalse(out["truncated"])
        many = [{"id": f"c{i}"} for i in range(envwatch.MAX_COMMITS + 5)]
        self.assertTrue(envwatch.mrs_between("a", "b", lambda f, t: many, lambda s: [], "")["truncated"])
        self.assertTrue(envwatch.mrs_between("a", "b", lambda f, t: None, lambda s: [], "")["error"])
        self.assertTrue(envwatch.mrs_between("a", "b", lambda f, t: [{"id": "c"}], lambda s: None, "")["error"])
        self.assertTrue(envwatch.mrs_between("", "b", lambda f, t: [], lambda s: [], "")["unknown"])


class TextTest(unittest.TestCase):
    ADDED = {"mrs": [{"iid": 1388}, {"iid": 1401}], "truncated": False, "unknown": False}
    LOST = {"mrs": [{"iid": 1300}], "truncated": False, "unknown": False}

    def text(self):
        return envwatch.e_card_text("etask", "https://g/idaas/etask", PREV, CUR, self.ADDED, self.LOST,
                                    {1388: "R-7", 1300: "R-2"}.get)

    def test_e_card(self):
        t = self.text().split("\n")
        self.assertEqual(t[0], "🤖 [E-etask-sandbox] etask @ sandbox · 0.198.45")
        self.assertEqual(t[1], "Môi trường: sandbox")
        self.assertEqual(t[2], "Phiên bản: 0.198.45 · Commit: bbbbbbbb · Xác minh: ✅ đã xác minh")
        self.assertTrue(t[3].startswith("Rollout: ") and t[3].endswith("Trước đó: 0.197.45"))
        self.assertEqual(t[4], "MR thêm: R-7 · MR mất: R-2")
        self.assertEqual(t[5], "Ghi chú: MR chưa có thẻ thêm !1401")
        self.assertIn(f"🔗 https://g/idaas/etask/-/compare/{'a' * 40}...{'b' * 40}", t)
        self.assertIn("🔗 http://j/282/", t)
        self.assertEqual(sum(1 for ln in t if ln.startswith("🤖 [")), 1)

    def test_digest(self):
        added = {"mrs": [{"iid": i} for i in range(12)], "truncated": True}
        t = envwatch.digest_text({"etask": [("dev", {"version": "1"}, {"version": "2", "level": "inferred"}, added, {})]},
                                 since=0, now=100)
        self.assertIn("etask @ dev: 1 → 2 (🟡 suy ra) · MR thêm: !0 !1", t)
        self.assertIn("+2", t)
        self.assertIn(f"chỉ tính {envwatch.MAX_COMMITS} commit mới nhất", t)
        self.assertIn("Không có thay đổi.", envwatch.digest_text({}, 0, 100))

    @unittest.skipUnless((EXT / "tests" / "parse_cli.js").exists() and shutil.which("node"), "extension / node missing")
    def test_golden_extension_reads_e_card(self):
        r = "🤖 [R-7] idaas/etask!1388 · Fix\nTrạng thái: sẵn sàng merge"
        msgs = [{"id": "m1", "messageIdInc": 1, "senderId": "BOT", "type": "TEXT", "content": r,
                 "createdAt": "2026-10-30T01:00:00Z", "user": {"displayName": "Chung"}, "metadata": {}},
                {"id": "m2", "messageIdInc": 2, "senderId": "BOT", "type": "TEXT", "content": self.text(),
                 "createdAt": "2026-10-30T02:00:00Z", "user": {"displayName": "Chung"}, "metadata": {}}]
        f = Path(tempfile.mkdtemp(prefix="golden2b-")) / "m.json"
        f.write_text(json.dumps(msgs, ensure_ascii=False), encoding="utf-8")
        p = subprocess.run(["node", str(EXT / "tests" / "parse_cli.js"), str(f), "BOT"], capture_output=True,
                           text=True, encoding="utf-8", timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        cards = {c["cardId"]: c for c in json.loads(p.stdout)["cards"]}
        e = cards["E-etask-sandbox"]
        self.assertEqual((e["type"], e["state"]), ("env", "env_ok"))
        self.assertIn("0.198.45", e["summary"])
        self.assertTrue(cards["R-7"]["envs"]["sandbox"].startswith("✅"))               # chip from "MR thêm: R-7"


TICK = r'''
import json, os, sys
sys.argv = ["x"]
import env_watch as ew
from agent_skill_toolkit.core import board_store
cfg = {"FCHAT_ENV_REPOS": "etask", "FCHAT_BOARD_GROUPS": "G,H", "FCHAT_ENV_DIGEST_AT": "00:00", "FCHAT_ENV_OWNER_GROUP": "O",
       "FCHAT_ENV_SHOW_PROD": "0"}                     # prod off for the group: only the owner's copy carries it
ew.gw.rccfg.get = lambda k, d="": cfg.get(k, d)
ew.gw._registry = lambda: {"etask": {"gitlab_project_id": 5401, "repo_path": "idaas/etask"}}
SB = {"env": "sandbox", "version": "0.197.45", "commit": "a" * 40, "digest": "x", "level": "verified"}
DV = {"env": "dev", "version": "1", "commit": "d" * 40, "digest": "y", "level": "inferred"}
SNAPS = {"v": [SB, DV]}
PROD = {"env": "prod", "version": "0.197.45", "commit": "", "level": "unknown"}
ew.gw._env_snapshots = lambda key, proj, show_prod=None: SNAPS["v"] + ([PROD] if show_prod else [])
GLERR = {"v": False}
class GL:
    def __init__(self, pid): pass
    def compare_commits(self, a, b): return [{"id": "c1"}]
    def commit_mrs(self, s): return None if GLERR["v"] else [{"iid": 1388, "state": "merged", "target_branch": None}]
ew.gw.gitlab_api.Client = GL
sent, FAIL = [], set()
ew.gw.send.send_text = lambda gid, text, gt=None, card_id="", **k: (sent.append((gid, card_id or ("table" if text.startswith("🌐 [ENV-") else "digest"), text)),
                                                                     {"error": True} if gid in FAIL else {"sent": True})[1]
b = board_store.Board()
K = ew._k
out = {}
D = 86400 * 3  # far from any digest time edge in tests that do not want it
out["t1"] = ew.tick(b, now=1000.0, mode="send")                                 # baseline only
# no baseline yet: the FULL table once (groups + the owner's copy with prod), numbered #1 per scope
out["snap1"] = [(g, len(t.splitlines()) - 1, "| prod |" in t, t.splitlines()[0][-2:]) for g, c, t in sent if c == "table"]
out["snap_again"] = [ew.tick(b, now=1000.5, mode="send")["table"], sum(1 for x in sent if x[1] == "table")]   # nothing changed
sent.clear()
SNAPS["v"] = [{**SB, "version": "0.198.45", "commit": "b" * 40, "digest": "z"}, {**DV, "version": "2", "commit": "e" * 40}]
# shadow has its own state: its first pass is a baseline, it sends nothing and touches no send state
out["shadow1"] = ew.tick(b, now=1001.0, mode="shadow")
out["shadow_sent"] = len([x for x in sent if x[1] != "table"])                   # E cards / digest: none
# the env table is not part of the shadow trial: the change already travels as ONE delta (2 rows) per scope, #2
out["delta_first"] = [(g, t.splitlines()[0].split(" · ")[-1], len(t.splitlines())) for g, c, t in sent if c == "table"]
# GitLab fails to list MRs: postponed (twice), then announced anyway
GLERR["v"] = True
out["defer"] = [ew.tick(b, now=1002.0 + i, mode="send")["deferred"] for i in range(2)]
out["sent_while_deferred"] = len([x for x in sent if x[1] != "table"])
out["table_while_deferred"] = len([x for x in sent if x[1] == "table"]) - len(out["delta_first"])   # nothing new
GLERR["v"] = False
# group H fails: G told once, H retried alone next pass
FAIL.add("H")
t = ew.tick(b, now=1010.0, mode="send")
out["partial"] = t["delivered"]

out["baseline_kept"] = b.meta_get(K("send", "snap", "etask", "sandbox"))["version"]
FAIL.clear()
t = ew.tick(b, now=1011.0, mode="send")
out["retry"] = t["delivered"]
out["group_snaps_prod"] = any("| prod |" in x for g, c, x in sent if c == "table" and g != "O")
# only the table changes (level): ONE delta per scope; H fails ⇒ kept with the same #seq, resent to H only
saved = list(sent)
sent.clear(); FAIL.add("H")
SNAPS["v"] = [{**SNAPS["v"][0], "level": "inferred"}, SNAPS["v"][1]]
t = ew.tick(b, now=1012.0, mode="send")
d1 = [(g, x.splitlines()[0].split(" · ")[-1], len(x.splitlines())) for g, c, x in sent if c == "table"]
pend = b.meta_get("envtable:pend:nhóm")
FAIL.clear(); sent.clear()
t2 = ew.tick(b, now=1013.0, mode="send")
d2 = [(g, x.splitlines()[0].split(" · ")[-1]) for g, c, x in sent if c == "table"]
t3 = ew.tick(b, now=1014.0, mode="send")
out["delta"] = [d1, pend and pend["left"], t["table"], d2, t2["table"], t3["table"], len(sent)]
sent[:] = saved
out["baseline_moved"] = b.meta_get(K("send", "snap", "etask", "sandbox"))["version"]
out["e_sends"] = [(g, c) for g, c, _ in sent if c not in ("digest", "table")]
out["queue_G"] = [it["id"] for it in b.meta_get(K("send", "queue", "G"), [])]
# digest: fails for H -> date not marked, G's queue cleared; next pass H only
b.meta_set(K("send", "digest", "last"), "2000-01-01")
FAIL.add("H")
sent.clear()
t = ew.tick(b, now=1020.0, mode="send")
out["digest1"] = [t["digest"], [g for g, c, _ in sent if c == "digest"], len(b.meta_get(K("send", "queue", "H"), []))]
FAIL.clear(); sent.clear()
t = ew.tick(b, now=1021.0, mode="send")
out["digest2"] = [t["digest"], [g for g, c, _ in sent if c == "digest"]]
SNAPS["v"] = [{**SNAPS["v"][0]}, {"env": "dev", "level": "unknown", "version": "", "commit": ""}]
out["unreadable"] = ew.tick(b, now=1030.0, mode="send")["changes"]
# GitLab keeps failing: postponed MAX_DEFER-1 passes, then sent WITH the warning; a new target restarts the count
GLERR["v"] = True
sent.clear()
SNAPS["v"] = [{**SB, "version": "0.199.45", "commit": "c" * 40, "digest": "q"}]
d1 = ew.tick(b, now=1040.0, mode="send")["deferred"]
SNAPS["v"] = [{**SB, "version": "0.200.45", "commit": "f" * 40, "digest": "r"}]          # new target: count resets
d2 = [ew.tick(b, now=1041.0 + i, mode="send")["deferred"] for i in range(ew.MAX_DEFER - 1)]
t = ew.tick(b, now=1050.0, mode="send")
out["long_error"] = [d1, d2, t["delivered"], [x for _, c, x in sent if c != "table"]]
GLERR["v"] = False
print(json.dumps(out, ensure_ascii=False))
'''


class TickTest(unittest.TestCase):
    def test_tick(self):
        from test_group_board import _run_tchat, _tmpdb
        out = _run_tchat(TICK, _tmpdb())
        self.assertEqual(sorted(out["t1"]["baseline"]), ["etask/dev", "etask/sandbox"])
        self.assertEqual(sorted(out["shadow1"]["baseline"]), ["etask/dev", "etask/sandbox"])   # separate state
        self.assertEqual(out["snap1"], [["G", 3, False, "#1"], ["H", 3, False, "#1"], ["O", 4, True, "#1"]])  # prod: O only
        self.assertEqual(out["snap_again"], [{}, 3])                                        # unchanged: nothing sent
        # the change itself: only the 2 changed rows (header + repo line + 2), #2, even while E cards wait for GitLab
        self.assertEqual(out["delta_first"], [["G", "#2", 4], ["H", "#2", 4], ["O", "#2", 4]])
        self.assertEqual((out["shadow_sent"], out["table_while_deferred"]), (0, 0))
        self.assertEqual(out["defer"], [["etask/sandbox", "etask/dev"], ["etask/sandbox", "etask/dev"]])
        self.assertEqual(out["sent_while_deferred"], 0)
        self.assertEqual(out["partial"], ["G:E-etask-sandbox"])
        self.assertEqual(out["baseline_kept"], "0.197.45")
        self.assertEqual(out["retry"], ["H:E-etask-sandbox"])                            # H only, G not twice
        self.assertFalse(out["group_snaps_prod"])                                        # prod only to the owner group
        d1, left, tb1, d2, tb2, tb3, after = out["delta"]
        self.assertEqual(d1, [["G", "#3", 3], ["H", "#3", 3], ["O", "#3", 3]])            # 1 row changed
        self.assertEqual((left, tb1), (["H"], {"nhóm": "pending", "chủ nhóm": "delta"}))
        self.assertEqual((d2, tb2), ([["H", "#3"]], {}))                                      # same #3, H only
        self.assertEqual((tb3, after), ({}, 1))                                               # nothing more
        self.assertEqual(out["baseline_moved"], "0.198.45")
        self.assertEqual([tuple(x) for x in out["e_sends"]],
                         [("G", "E-etask-sandbox"), ("H", "E-etask-sandbox"), ("H", "E-etask-sandbox")])
        self.assertEqual(len(out["queue_G"]), 1)                                         # dev enqueued once
        self.assertEqual(out["digest1"], [False, ["G", "H"], 1])                         # H kept for retry
        self.assertEqual(out["digest2"], [True, ["H"]])
        self.assertEqual(out["unreadable"], [])
        d1, d2, delivered, texts = out["long_error"]
        self.assertEqual(d1, ["etask/sandbox"])
        self.assertEqual(d2, [["etask/sandbox"]] * 2)                                    # restarted on the new target
        self.assertEqual(delivered, ["G:E-etask-sandbox", "H:E-etask-sandbox"])
        self.assertTrue(all("không đọc được danh sách MR thêm/mất" in x for x in texts))


TABLE = r"""
import json, sys, time
sys.argv = ["x"]
import env_watch as ew
from agent_skill_toolkit.core import board_store
cfg = {"FCHAT_ENV_REPOS": "etask", "FCHAT_BOARD_GROUPS": "G,H", "FCHAT_ENV_OWNER_GROUP": "O"}
ew.gw.rccfg.get = lambda k, d="": cfg.get(k, d)
ew.gw._registry = lambda: {"etask": {"gitlab_project_id": 5401, "repo_path": "idaas/etask"}}
SB = {"env": "sandbox", "version": "0.198.45", "commit": "a" * 40, "level": "verified"}
PR = {"env": "prod", "version": "0.197.45", "commit": "", "level": "unknown"}
SNAPS = {"v": [SB]}
ew.gw._env_snapshots = lambda key, proj, show_prod=None: SNAPS["v"] + ([PR] if show_prod else [])
sent, CRASH = [], set()
def send_text(gid, text, gt=None, card_id="", **k):
    if gid in CRASH:
        raise KeyboardInterrupt("process killed while sending")
    sent.append((gid, text.splitlines()[0].split(" · ")[-1], text.splitlines()[0][:16]))
    return {"sent": True}
ew.gw.send.send_text = send_text
b = board_store.Board()
out = {}
scopes = ew.table_scopes(["G", "H"], "O")
rows, ok, webs = ew.read_all_rows(scopes, "O")
out["first"] = [ew.table_pass(b, scopes, rows, ok, webs, 1000.0), list(sent)]
# a change; the process dies after G got it, while sending to H
sent.clear(); CRASH.add("H")
SNAPS["v"] = [{**SB, "version": "0.199.45"}]
rows, ok, webs = ew.read_all_rows(scopes, "O")
try:
    ew.table_pass(b, scopes, rows, ok, webs, 1010.0)
except KeyboardInterrupt:
    pass
out["crash"] = [list(sent), b.meta_get("envtable:pend:nhóm")["left"], b.meta_get("envtable:seq:nhóm")]
# restart: the SAME message (#2) to H only; the change is not re-numbered nor re-sent to G
CRASH.clear(); sent.clear()
rows, ok, webs = ew.read_all_rows(scopes, "O")
out["resume"] = [ew.table_pass(b, scopes, rows, ok, webs, 1020.0), list(sent), b.meta_get("envtable:seq:nhóm")]
# "@bot env" from G by the owner: env changed meanwhile ⇒ delta (#3) to ALL first, then the full table to G only,
# carrying the CURRENT number (#3, no new number ⇒ H sees no gap) + the owner's copy with prod to O
sent.clear()
SNAPS["v"] = [{**SB, "version": "0.200.45"}]
b.meta_set(ew.REQ_KEY, [{"id": "r1", "group": "G", "owner": True, "at": 1}])
asks = ew.serve_requests(b, now=1030.0)
out["request"] = [list(sent), [a.get("done") for a in asks], b.meta_get(ew.REQ_KEY), b.meta_get("envtable:seq:nhóm")]
sent.clear()
out["nothing"] = ew.serve_requests(b, now=1040.0)
# owner copy to O fails ⇒ the request stays with O only; a member request (H) is served; then O succeeds
FAIL = {"O"}
real = ew.gw.send.send_text
ew.gw.send.send_text = lambda gid, text, gt=None, card_id="", **k: {"error": True} if gid in FAIL else real(gid, text, gt, card_id)
b.meta_set(ew.REQ_KEY, [{"id": "r2", "group": "G", "owner": True, "at": 2}, {"id": "r3", "group": "H", "owner": False, "at": 3}])
sent.clear()
ew.serve_requests(b, now=1050.0)
out["o_fail"] = [b.meta_get(ew.REQ_KEY), [x[0] for x in sent]]
FAIL.clear(); sent.clear()
ew.serve_requests(b, now=1060.0)
out["o_retry"] = [b.meta_get(ew.REQ_KEY), [x[0] for x in sent]]
# crash after reading the queue (k8s read raises): the queue is untouched
b.meta_set(ew.REQ_KEY, [{"id": "r4", "group": "G", "owner": False, "at": 4}])
ew.gw._env_snapshots = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("k8s down"))
try:
    ew.serve_requests(b, now=1070.0)
except RuntimeError:
    pass
out["crash_queue"] = [r["id"] for r in b.meta_get(ew.REQ_KEY)]
# env_watch was down for > 12 h: old requests are dropped BEFORE anything is sent
ew.gw._env_snapshots = lambda key, proj, show_prod=None: SNAPS["v"] + ([PR] if show_prod else [])
b.meta_set(ew.REQ_KEY, [{"id": "old", "group": "G", "owner": False, "at": 1}])
sent.clear()
out["stale"] = [ew.serve_requests(b, now=1 + ew.REQ_MAX_AGE_S + 60), len(sent), b.meta_get(ew.REQ_KEY)]
print(json.dumps(out, ensure_ascii=False))
"""


class TablePipelineTest(unittest.TestCase):
    """Only changes travel, numbered by ONE writer; durable before sending (Codex envdelta R1)."""

    def test_pipeline(self):
        from test_group_board import _run_tchat, _tmpdb
        out = _run_tchat(TABLE, _tmpdb())
        first, sent = out["first"]
        self.assertEqual(first, {"nhóm": "full", "chủ nhóm": "full"})
        self.assertEqual([x[:2] for x in sent], [["G", "#1"], ["H", "#1"], ["O", "#1"]])
        crash_sent, left, seq = out["crash"]
        self.assertEqual(([x[:2] for x in crash_sent], left, seq), ([["G", "#2"]], ["H"], 2))
        res, sent2, seq2 = out["resume"]
        self.assertEqual([x[:2] for x in sent2], [["H", "#2"], ["O", "#2"]])               # same #2, G not again
        self.assertEqual(seq2, 2)
        req_sent, done, queue_left, seq3 = out["request"]
        # per scope: the delta to every recipient, then the full table to the asker
        self.assertEqual([x[:2] for x in req_sent],
                         [["G", "#3"], ["H", "#3"], ["G", "#3"], ["O", "#3"], ["O", "#3"]])
        kinds = ["delta" if x[2].startswith("🌐 [ENV-DELTA]") else "full" for x in req_sent]
        self.assertEqual(kinds, ["delta", "delta", "full", "delta", "full"])
        self.assertEqual((done, queue_left, seq3), ([True, True], [], 3))                  # the full took no number
        self.assertEqual(out["nothing"], [])
        queue, got = out["o_fail"]
        self.assertEqual(queue, [{"id": "r2", "group": "G", "owner": True, "at": 2,
                                  "left": [{"scope": "chủ nhóm", "group": "O"}]}])           # G done, O kept
        self.assertEqual(got, ["G", "H"])
        self.assertEqual(out["o_retry"], [[], ["O"]])                                    # only O, then gone
        self.assertEqual(out["crash_queue"], ["r4"])                                     # nothing lost
        self.assertEqual(out["stale"], [[], 0, []])                                        # > 12 h: dropped, not sent


class OneSidedErrorTest(unittest.TestCase):
    def test_one_side_error_is_flagged(self):                                           # Codex 2b R2 #1
        t = envwatch.e_card_text("etask", "", {"version": "1", "commit": "a" * 40}, {**CUR},
                                 {"mrs": [{"iid": 5}], "error": False}, {"mrs": [], "error": True, "unknown": True},
                                 lambda i: None)
        self.assertIn("không đọc được danh sách MR mất", t)


class CardIdTest(unittest.TestCase):
    def test_collision(self):
        self.assertEqual(envwatch.card_id("etask", "dev"), "E-etask-dev")
        owners = {"E-a-b-c": "a-b:c"}
        cid = envwatch.card_id("a", "b-c", owners.get)
        self.assertTrue(cid.startswith("E-a-b-c-") and len(cid) == len("E-a-b-c-") + 4)
        self.assertEqual(envwatch.card_id("a", "b-c", owners.get), cid)                  # stable
        self.assertEqual(envwatch.card_id("a-b", "c", owners.get), "E-a-b-c")            # the owner keeps it

    def test_e_card_has_env_field_and_digest_lists_lost(self):
        t = envwatch.e_card_text("etask-be", "", {"version": "1"}, {"env": "demo-fci", "version": "2"},
                                 {"mrs": []}, {"mrs": []}, lambda i: None)
        self.assertIn("Môi trường: demo-fci", t.split("\n"))
        d = envwatch.digest_text({"etask": [("dev", {"version": "2"}, {"version": "1"}, {"mrs": []},
                                             {"mrs": [{"iid": 9}]})]}, 0, 100)
        self.assertIn("MR mất: !9", d)
        d = envwatch.digest_text({"etask": [("dev", {}, {"version": "1"}, {"mrs": [], "error": True}, {})]}, 0, 100)
        self.assertIn("MR thêm: không đọc được danh sách MR", d)


if __name__ == "__main__":
    unittest.main()
