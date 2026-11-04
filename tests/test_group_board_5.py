"""Env table for every repo (31/10): envs without a declared k8s workload are filled from the job's newest
SUCCESSFUL Jenkins build (level "built" 🔨 — never proof of what runs), and a big table travels in parts that the
extension joins back (a crash mid-way resumes at the next part)."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".claude" / "skills" / "_runtime"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from agent_skill_toolkit.workflows.group_board import envmap  # noqa: E402

EXT = Path(os.environ.get("FQR_EXT_DIR") or ROOT.parent / "fpt-chat-quick-reply")
T = {"env": "dev", "target": "", "job": "job/DEV/job/x", "branch": "develop", "tag_prefix": ""}
GOOD = {"number": 77, "timestamp": 1761000000000, "url": "http://j/77/", "commit_sha": "c" * 40,
        "pushes": [{"image": "reg/x", "version": "0.0.69", "digest": "sha256:" + "1" * 64}]}


class FromBuildTest(unittest.TestCase):
    def snap(self, lg, t=T, tag=None):
        return envmap.snapshot(t, {}, tag or (lambda n: {}), None, None, lambda: lg)

    def test_newest_successful_build(self):
        s = self.snap({"good": GOOD, "last": {"number": 79, "result": "FAILURE"}})
        self.assertEqual((s["level"], s["version"], s["commit"], s["build"]["number"]), ("built", "0.0.69", "c" * 40, 77))
        self.assertIn("chưa xác minh pod", s["notes"][0])
        self.assertIn("build mới hơn #79 FAILURE", s["notes"][-1])
        row = envmap.snapshot_row("x", s)
        self.assertTrue(row.startswith("x | dev | 🔨 | 0.0.69 | " + "c" * 40))
        self.assertTrue(row.endswith("| develop | http://j/77/"))

    def test_no_sha_uses_git_tag_and_errors_stay_unknown(self):
        s = self.snap({"good": {**GOOD, "commit_sha": ""}, "last": {"number": 77}}, {**T, "tag_prefix": "v"},
                      lambda n: {"commit_id": "d" * 40} if n == "v0.0.69" else {})
        self.assertEqual((s["commit"], s["tag"]), ("d" * 40, "v0.0.69"))
        self.assertEqual(self.snap({"error": True, "message": "HTTP 403"})["level"], "unknown")
        self.assertEqual(self.snap({"good": {}, "last": {"number": 3, "result": "FAILURE"}})["level"], "unknown")
        self.assertEqual(envmap.snapshot(T, {}, lambda n: {})["level"], "unknown")   # no lastgood reader: as before

    def test_parts_split_by_repo_and_join(self):
        rows = {f"r{i:02d}|dev": f"r{i:02d} | dev | 🔨 | 1 | {'c' * 40} | 01/10 10:00 | develop | http://j/{i}/"
                for i in range(30)}
        parts = envmap.table_parts("SNAPSHOT", rows, {}, "nhóm", 4, now=1761000000.0, limit=1000)
        self.assertGreater(len(parts), 1)
        n = len(parts)
        for i, p in enumerate(parts, 1):
            self.assertLessEqual(len(p), 1000)                                   # header counted
            self.assertIn(f"Bảng môi trường ({i}/{n}) · ", p.splitlines()[0])
            self.assertTrue(p.splitlines()[0].endswith(" · nhóm · #4"))
            self.assertEqual(p.splitlines()[1][:7], "repo | ")                   # a part starts with a whole repo block
        joined = [ln for p in parts for ln in p.splitlines()[1:]]
        self.assertEqual(joined, envmap.table_text("SNAPSHOT", rows, {}, "nhóm", 4, now=1761000000.0).splitlines()[1:])
        self.assertEqual(len(envmap.table_parts("DELTA", dict(list(rows.items())[:2]), {}, "nhóm", 5)), 1)
        big = {f"big|e{i:02d}": f"big | e{i:02d} | 🔨 | 1 | {'c' * 40} | 01/10 10:00 | develop | http://j/{i}/"
               for i in range(30)}                                                # one repo bigger than a message
        parts = envmap.table_parts("SNAPSHOT", big, {}, "chủ nhóm", 7, now=1761000000.0, limit=1000)
        self.assertGreater(len(parts), 1)
        for p in parts:
            self.assertLessEqual(len(p), 1000)
            self.assertTrue(p.splitlines()[1].startswith("repo | big | "))
        self.assertEqual(sum(len(p.splitlines()) - 2 for p in parts), 30)

    @unittest.skipUnless((EXT / "tests" / "parse_cli.js").exists(), "extension missing")
    def test_extension_joins_parts(self):
        rows = {f"r{i:02d}|dev": f"r{i:02d} | dev | 🔨 | 1 | {'c' * 40} | 01/10 10:00 | develop | http://j/{i}/"
                for i in range(30)}
        parts = envmap.table_parts("SNAPSHOT", rows, {}, "nhóm", 4, now=1761000000.0, limit=1000)
        msgs = [{"id": f"m{i}", "messageIdInc": i + 1, "senderId": "BOT", "type": "TEXT", "content": p,
                 "createdAt": f"2026-10-31T10:00:0{i}Z", "user": {"displayName": "Chung"}, "metadata": {}}
                for i, p in enumerate(parts)]
        f = Path(tempfile.mkdtemp(prefix="gb5-")) / "m.json"
        for label, ms, want in (("all", msgs, 30), ("missing", msgs[:-1], None)):
            f.write_text(json.dumps(ms, ensure_ascii=False), encoding="utf-8")
            p = subprocess.run(["node", str(EXT / "tests" / "parse_cli.js"), str(f), "BOT", "--env"],
                               capture_output=True, text=True, encoding="utf-8", timeout=60)
            self.assertEqual(p.returncode, 0, p.stderr)
            env = json.loads(p.stdout).get("env")
            self.assertEqual(env and len(env["rows"]), want, label)              # incomplete table never shown


FLUSH = r'''
import json, sys
sys.argv = ["x"]
import env_watch as ew
from agent_skill_toolkit.core import board_store
b = board_store.Board()
sent, FAIL = [], {"at": None}
def send(gid, text, gt=None, **k):
    if FAIL["at"] == (gid, text.splitlines()[0]):
        return {"error": True}
    sent.append((gid, text.splitlines()[0].split(" (")[1][:3]))
    return {"sent": True}
ew.gw.send.send_text = send
ew.envmap.PART_CHARS = 200
rows = {f"r{i}|dev": f"r{i} | dev | 🔨 | 1 | {'c' * 40} | 01/10 10:00 | develop | http://j/{i}/" for i in range(4)}
ew._queue(b, "nhóm", ["G", "H"], "SNAPSHOT", rows, {}, 1761000000.0)
texts = b.meta_get("envtable:pend:nhóm")["texts"]
FAIL["at"] = ("H", texts[2].splitlines()[0])
r1 = ew._flush_pending(b, "nhóm")
s1 = list(sent); sent.clear()
FAIL["at"] = None
r2 = ew._flush_pending(b, "nhóm")
print(json.dumps({"n": len(texts), "r": [r1, r2], "s1": s1, "s2": sent,
                  "drows": sorted(b.meta_get("envtable:drows:nhóm") or {}), "pend": b.meta_get("envtable:pend:nhóm")}))
'''


ANSWER = r'''
import json, sys
sys.argv = ["x"]
import env_watch as ew
from agent_skill_toolkit.core import board_store
b = board_store.Board()
sent, FAIL = [], {"n": None}
def send(gid, text, gt=None, **k):
    if FAIL["n"] is not None and len(sent) == FAIL["n"]:
        return {"error": True}
    sent.append(text.splitlines()[0])
    return {"sent": True}
ew.gw.send.send_text = send
ew.envmap.PART_CHARS = 250
rows = {"nhóm": {f"r{i}|dev": f"r{i} | dev | 🔨 | 1 | {'c' * 40} | 01/10 10:00 | develop | http://j/{i}/" for i in range(4)}}
r = {"scope": "nhóm", "group": "G", "req": "q1"}
FAIL["n"] = 2
d1 = ew._answer(b, "nhóm", r, rows, {}, 1761000000.0)
s1 = list(sent)
FAIL["n"] = None
rows["nhóm"]["r0|dev"] = rows["nhóm"]["r0|dev"].replace("| 1 |", "| 2 |")   # data changed meanwhile: parts kept
d2 = ew._answer(b, "nhóm", r, rows, {}, 1761000600.0)
left = b.meta_get("envtable:ans:q1:nhóm:G")
# a failed answer, then env_watch down for long: the stale request AND its prepared parts are dropped
FAIL["n"] = len(sent) + 1
ew._answer(b, "nhóm", {"scope": "nhóm", "group": "G", "req": "q2"}, rows, {}, 1761000000.0)
b.meta_set(ew.REQ_KEY, [{"id": "q2", "at": 1761000000.0, "left": [{"scope": "nhóm", "group": "G"}]}])
held = bool(b.meta_get("envtable:ans:q2:nhóm:G"))
asks = ew.serve_requests(b, now=1761000000.0 + ew.REQ_MAX_AGE_S + 1)
print(json.dumps({"d": [d1, d2], "s1": s1, "s2": sent[len(s1):len(s1) + 2], "left": left,
                  "stale": [held, asks, b.meta_get(ew.REQ_KEY), b.meta_get("envtable:ans:q2:nhóm:G")]}))
'''


class AnswerPartsTest(unittest.TestCase):
    def test_answer_resumes_same_parts(self):
        from test_group_board import _run_tchat, _tmpdb
        out = _run_tchat(ANSWER, _tmpdb())
        self.assertEqual(out["d"], [False, True])
        self.assertEqual(len(out["s1"]), 2)
        self.assertEqual(len({h.split(" · ")[1] for h in out["s1"] + out["s2"]}), 1)  # same time stamp in every part
        self.assertEqual([h.split("(")[1][:3] for h in out["s2"]], [f"{i}/4" for i in range(3, 5)])
        self.assertIsNone(out["left"])
        self.assertEqual(out["stale"], [True, [], [], None])


PRODCHAN = r'''
import json, sys
sys.argv = ["x"]
import env_watch as ew
from agent_skill_toolkit.core import board_store
cfg = {"FCHAT_BOARD_GROUPS": "N", "FCHAT_ENV_OWNER_GROUP": "P", "FCHAT_ENV_REPOS": "etask"}
ew.gw.rccfg.get = lambda k, d="": cfg.get(k, d)
ROW = lambda env: {"env": env, "version": "1", "commit": "c" * 40, "level": "built"}
ew.read_all_rows = lambda scopes, og: ({"nhóm": {"etask|dev": "etask | dev | 🔨 | 1"},
                                        "chủ nhóm": {"etask|dev": "etask | dev | 🔨 | 1", "etask|prod": "etask | prod | 🔨 | 1"}},
                                       {"nhóm": set(), "chủ nhóm": set()}, {})
sent = []
ew.gw.send.send_text = lambda gid, text, gt=None, **k: (sent.append((gid, text.splitlines()[0].split(" · ")[2])), {"sent": True})[1]
b = board_store.Board()
b.meta_set("envtable:drows:nhóm", {}); b.meta_set("envtable:drows:chủ nhóm", {})
b.meta_set(ew.REQ_KEY, [{"id": "a", "group": "P", "owner": False, "at": 1761000000.0},
                        {"id": "b", "group": "N", "owner": False, "at": 1761000000.0},
                        {"id": "c", "group": "N", "owner": True, "at": 1761000000.0}])
ew.serve_requests(b, now=1761000001.0)
print(json.dumps({"sent": sent, "left": b.meta_get(ew.REQ_KEY)}))
'''


ENVCMD = r'''
import json, sys
sys.argv = ["x"]
import group_watch as gw
cfg = {"FCHAT_ENV_REPOS": "etask", "FCHAT_ENV_OWNER_GROUP": "P"}
gw.rccfg.get = lambda k, d="": cfg.get(k, d)
gw._registry = lambda: {"etask": {"gitlab_project_id": 1, "repo_path": "idaas/etask",
                                  "environments": {"dev": {"jenkins": {"job": "J"}}}}}
seen = []
gw._env_snapshots = lambda key, proj, show_prod=None: (seen.append(show_prod), [])[1]
gw._post_inline = lambda *a, **k: None
for g in ("N", "P"):
    gw._env_command(g, "GROUP", "Lan", "u-lan", "env", ["etask"])
print(json.dumps(seen))
'''


GROUPPROD = r'''
import json, sys
sys.argv = ["x"]
import env_watch as ew
from agent_skill_toolkit.core import board_store
cfg = {"FCHAT_BOARD_GROUPS": "N", "FCHAT_ENV_OWNER_GROUP": "", "FCHAT_ENV_SHOW_PROD": "0"}
ew.gw.rccfg.get = lambda k, d="": cfg.get(k, d)
ew.gw._registry = lambda: {"etask": {"gitlab_project_id": 1, "repo_path": "idaas/etask"}}
SN = [{"env": "dev", "version": "1", "commit": "c" * 40, "level": "built"},
      {"env": "prod", "version": "9", "commit": "d" * 40, "level": "built"}]
asked = []
ew.gw._env_snapshots = lambda key, proj, show_prod=None: (asked.append(show_prod), SN if show_prod else SN[:1])[1]
cfg["FCHAT_ENV_REPOS"] = "etask"
sent = []
ew.gw.send.send_text = lambda gid, text, gt=None, **k: (sent.append(text), {"sent": True})[1]
b = board_store.Board()
out = []
for flag in ("0", "0", "1", "1", "0"):
    cfg["FCHAT_ENV_SHOW_PROD"] = flag
    sent.clear()
    sc = ew.table_scopes(["N"], "")
    rows, ok, webs = ew.read_all_rows(sc, "")
    r = ew.table_pass(b, sc, rows, ok, webs, 1761000000.0)
    out.append([r.get("nhóm"), any("| prod |" in t for t in sent), sorted(b.meta_get("envtable:drows:nhóm") or {})])
print(json.dumps({"out": out, "asked": asked}))
'''


GROUPPROD_FAIL = r'''
import json, sys
sys.argv = ["x"]
import env_watch as ew
from agent_skill_toolkit.core import board_store
cfg = {"FCHAT_BOARD_GROUPS": "N", "FCHAT_ENV_OWNER_GROUP": "", "FCHAT_ENV_SHOW_PROD": "0", "FCHAT_ENV_REPOS": "etask"}
ew.gw.rccfg.get = lambda k, d="": cfg.get(k, d)
ew.gw._registry = lambda: {"etask": {"gitlab_project_id": 1, "repo_path": "idaas/etask"}}
SN = [{"env": "dev", "version": "1", "commit": "c" * 40, "level": "built"},
      {"env": "prod", "version": "9", "commit": "d" * 40, "level": "built"}]
ew.gw._env_snapshots = lambda key, proj, show_prod=None: SN if show_prod else SN[:1]
sent, DOWN = [], {"v": False}
ew.gw.send.send_text = lambda gid, text, gt=None, **k: ({"error": True} if DOWN["v"] else (sent.append(text), {"sent": True})[1])
b = board_store.Board()
def run():
    sc = ew.table_scopes(["N"], "")
    rows, ok, webs = ew.read_all_rows(sc, "")
    return ew.table_pass(b, sc, rows, ok, webs, 1761000000.0)
run()                                                     # baseline, prod off
cfg["FCHAT_ENV_SHOW_PROD"] = "1"; DOWN["v"] = True
r_on = run()                                              # full WITH prod queued, sending fails
flag_saved = b.meta_get("envtable:prodflag:nhóm")         # stored with the queued table (same transaction)
# a "@bot env" answer built while prod was on, sending fails half way
ans = ew._answer(b, "nhóm", {"scope": "nhóm", "group": "N", "req": "q"}, {"nhóm": {"etask|dev": "etask | dev | 🔨 | 1", "etask|prod": "etask | prod | 🔨 | 9"}}, {}, 1761000000.0)
cfg["FCHAT_ENV_SHOW_PROD"] = "0"; DOWN["v"] = False; sent.clear()
r_off = run()
leak = any("| prod |" in t for t in sent)
sent.clear()
ans2 = ew._answer(b, "nhóm", {"scope": "nhóm", "group": "N", "req": "q"}, {"nhóm": {"etask|dev": "etask | dev | 🔨 | 1"}}, {}, 1761000001.0)
print(json.dumps({"on": r_on, "flag": flag_saved, "ans": ans, "off": r_off, "leak": leak,
                  "ans2": [ans2, any("| prod |" in t for t in sent)], "drows": sorted(b.meta_get("envtable:drows:nhóm"))}))
'''


class GroupProdOffNeverLeaksTest(unittest.TestCase):
    """Codex R1: tin dựng lúc còn bật prod (chờ gửi / trả lời dở) không bao giờ đi ra sau khi tắt."""

    def test_off_drops_pending_and_rebuilds_answers(self):
        from test_group_board import _run_tchat, _tmpdb
        out = _run_tchat(GROUPPROD_FAIL, _tmpdb())
        self.assertEqual((out["on"].get("nhóm"), out["flag"], out["ans"]), ("pending", "1", False))
        self.assertEqual(out["off"].get("nhóm"), "full")                    # stored "1" ≠ "0" ⇒ whole table without prod
        self.assertFalse(out["leak"])
        self.assertEqual(out["ans2"], [True, False])                           # answer rebuilt, no prod
        self.assertEqual(out["drows"], ["etask|dev"])


class GroupProdSwitchTest(unittest.TestCase):
    """FCHAT_ENV_SHOW_PROD=1: prod vào thẳng bảng nhóm chung; bật/tắt ⇒ cả bảng mới (tắt: dòng prod biến mất)."""

    def test_switch(self):
        from test_group_board import _run_tchat, _tmpdb
        out = _run_tchat(GROUPPROD, _tmpdb())
        self.assertEqual(out["out"], [
            ["full", False, ["etask|dev"]],
            [None, False, ["etask|dev"]],                                       # nothing changed
            ["full", True, ["etask|dev", "etask|prod"]],                        # turned on: whole table, with prod
            [None, False, ["etask|dev", "etask|prod"]],
            ["full", False, ["etask|dev"]]])                                    # turned off: prod row gone
        self.assertEqual(out["asked"], [False, False, True, True, False])      # prod read only when needed


STAR = r'''
import json, sys
sys.argv = ["x"]
import group_watch as gw
cfg = {"FCHAT_ENV_REPOS": "*"}
gw.rccfg.get = lambda k, d="": cfg.get(k, d)
gw._registry = lambda: {"a": {"gitlab_project_id": 1, "environments": {"dev": {"jenkins": {"job": "j"}}}},
                        "b": {"gitlab_project_id": 2, "environments": {}},
                        "c": {"environments": {"dev": {"jenkins": {"job": "j"}}}},
                        "d": {"gitlab_project_id": 4, "environments": {"prod": {"jenkins": {"job": "p"}}}}}
out = [gw.env_repos(), gw._env_repo("a", gw._registry()), gw._env_repo("b", gw._registry())]
cfg["FCHAT_ENV_REPOS"] = "b,*"
out.append(gw.env_repos())
cfg["FCHAT_ENV_REPOS"] = "a"
out.append(gw.env_repos())
print(json.dumps(out))
'''


class EnvReposStarTest(unittest.TestCase):
    def test_star_means_every_project_with_envs(self):
        from test_group_board import _run_tchat, _tmpdb
        self.assertEqual(_run_tchat(STAR, _tmpdb()), [["a", "d"], "a", None, ["a", "d", "b"], ["a"]])


class ProdChannelTest(unittest.TestCase):
    """Kênh prod (FCHAT_ENV_OWNER_GROUP) cho mọi thành viên: hỏi TRONG kênh prod ⇒ chỉ bản có prod ở đó."""

    def test_where_answers_go(self):
        from test_group_board import _run_tchat, _tmpdb
        out = _run_tchat(PRODCHAN, _tmpdb())
        self.assertEqual(out["sent"], [["N", "nhóm"], ["N", "nhóm"], ["P", "chủ nhóm"], ["P", "chủ nhóm"]])
        self.assertEqual(out["left"], [])

    def test_env_repo_in_prod_channel_shows_prod(self):
        from test_group_board import _run_tchat, _tmpdb
        self.assertEqual(_run_tchat(ENVCMD, _tmpdb()), [None, True])           # nhóm thường: theo cấu hình; kênh prod: có


class FlushPartsTest(unittest.TestCase):
    def test_resume_at_next_part(self):
        from test_group_board import _run_tchat, _tmpdb
        out = _run_tchat(FLUSH, _tmpdb())
        n = out["n"]
        self.assertEqual(n, 4)
        self.assertEqual(out["r"], [False, True])
        self.assertEqual(out["s1"], [["G", f"{i}/{n}"] for i in range(1, n + 1)] + [["H", "1/4"], ["H", "2/4"]])
        self.assertEqual(out["s2"], [["H", "3/4"], ["H", "4/4"]])                  # parts already sent: not again
        self.assertEqual((out["drows"], out["pend"]), ([f"r{i}|dev" for i in range(4)], None))


TRACK = r"""
import json, sys
sys.argv = ["x"]
import group_watch as gw
cfg = {"FCHAT_ENV_REPOS": "etask"}
gw.rccfg.get = lambda k, d="": cfg.get(k, d)
E = {"environments": {"dev": {"jenkins": {"job": "J"}}}}
gw._registry = lambda: {"etask": {"gitlab_project_id": 1, "repo_path": "idaas/etask", **E},
                        "collabora": {"gitlab_project_id": 2, "repo_path": "x/collabora", **E},
                        "noenv": {"gitlab_project_id": 3, "repo_path": "x/noenv"}}
posts, applied = [], []
send_ok = [True]
gw._post_inline = lambda gid, gt, text, people: (posts.append(text), send_ok[0])[1]
class S:
    @staticmethod
    def apply(ch, by):
        if ch["FCHAT_ENV_REPOS"] == "etask,collabora":
            raise RuntimeError("đã bị đổi ở nơi khác")
        applied.append([ch, by]); return {"changed": ch}
    @staticmethod
    def validate(k, v):
        return "quá dài" if len(v) > 20 else ""
gw.settings_mod = S
gw._OWNER.update(id="OWN", name="chủ")
out = {}
gw._env_track("G", "GROUP", "Lan", "u-lan", "OWN", "collabora")
out["member"] = [applied[:], posts[-1][:1]]
gw._env_track("G", "GROUP", "Chủ", "OWN", "OWN", "")
out["list"] = [applied[:], posts[-1].splitlines()[:3]]
gw._env_track("G", "GROUP", "Chủ", "OWN", "OWN", "noenv, etask")
out["bad"] = [applied[:], "noenv" in posts[-1]]
gw._env_track("G", "GROUP", "Chủ", "OWN", "OWN", "Collabora etask")
out["set"] = [applied[-1], posts[-1].splitlines()[1]]
gw._env_track("G", "GROUP", "Chủ", "OWN", "OWN", "*")
out["star"] = applied[-1][0]
n = len(applied)
gw._env_track("G", "GROUP", "Chủ", "OWN", "OWN", "etask")
out["same"] = [len(applied) - n, posts[-1].splitlines()[0]]
cfg["FCHAT_ENV_REPOS"] = "etask"
p0 = len(posts)
gw._env_track("G", "GROUP", "Chủ", "OWN", "OWN", "etask,collabora")
out["fail"] = [len(applied) - n, [p.splitlines()[1] for p in posts[p0:]], "KHÔNG lưu được" in posts[-1]]
gw.env_capable = lambda reg: ["etask", "collabora", "x" * 30]
gw._env_track("G", "GROUP", "Chủ", "OWN", "OWN", "x" * 30)
out["long"] = [len(applied) - n, posts[-1][:2]]
send_ok[0] = False
gw._env_track("G", "GROUP", "Chủ", "OWN", "OWN", "collabora")
out["send_fail"] = len(applied) - n
# @bot env <repo> chưa theo dõi vẫn trả lời; repo không có môi trường thì không
seen = []
gw._env_snapshots = lambda key, proj, show_prod=None: (seen.append(key), [])[1]
gw._env_command("G", "GROUP", "Lan", "u-lan", "env", ["collabora"])
gw._env_command("G", "GROUP", "Lan", "u-lan", "env", ["noenv"])
out["ondemand"] = seen
print(json.dumps(out, ensure_ascii=False))
"""


class EnvTrackTest(unittest.TestCase):
    """@bot env theo dõi: chỉ chủ nhóm; danh sách cho extension; ghi FCHAT_ENV_REPOS qua settings.apply."""

    def test_track_command(self):
        from test_group_board import _run_tchat, _tmpdb
        out = _run_tchat(TRACK, _tmpdb())
        self.assertEqual(out["member"][0], [], "thành viên không đổi được")
        self.assertEqual(out["member"][1], "⛔")
        self.assertEqual(out["list"][0], [])
        self.assertTrue(out["list"][1][0].startswith("🧭 [ENV-REPOS] theo dõi 1/2 repo"))
        self.assertEqual(out["list"][1][1:], ["theo dõi: etask", "khác: collabora"])
        self.assertEqual(out["bad"], [[], True], "repo không có môi trường ⇒ từ chối, không ghi gì")
        self.assertEqual(out["set"][0][0], {"FCHAT_ENV_REPOS": "collabora,etask"})
        self.assertEqual(out["set"][1], "theo dõi: etask, collabora")
        self.assertEqual(out["star"], {"FCHAT_ENV_REPOS": "*"})
        self.assertEqual(out["same"][0], 0, "không đổi ⇒ không ghi .env (không làm bot chạy lại)")
        self.assertIn("không đổi", out["same"][1])
        self.assertEqual(out["fail"][0], 0)
        self.assertEqual(out["fail"][1], ["theo dõi: etask, collabora", "theo dõi: etask"],
                         "báo trước khi ghi; ghi lỗi ⇒ tin 🧭 mới với tập cũ")
        self.assertTrue(out["fail"][2])
        self.assertEqual(out["long"], [0, "⚠️"], "quá giới hạn ⇒ báo, không ghi")
        self.assertEqual(out["send_fail"], 0, "gửi trả lời lỗi ⇒ không ghi .env")
        self.assertEqual(out["ondemand"], ["collabora"], "repo chưa theo dõi vẫn hỏi được")

    def test_parse(self):
        from agent_skill_toolkit.workflows.group_board import commands as c
        self.assertEqual(c.parse("@bot env theo dõi etask,collabora"), ("env_track", ("etask,collabora",)))
        self.assertEqual(c.parse("@bot env track"), ("env_track", ("",)))
        self.assertEqual(c.parse("@bot env etask"), ("env", ("etask",)))


REPOSIG = r"""
import json, sys
sys.argv = ["x"]
import env_watch as ew
from agent_skill_toolkit.core import board_store
cfg = {"FCHAT_ENV_SHOW_PROD": "0"}
ew.gw.rccfg.get = lambda k, d="": cfg.get(k, d)
sent = []
ew._send_table = lambda g, t: (sent.append(t.splitlines()[0][:30]), True)[1]
b = board_store.Board()
sc = {"nhóm": ["N"]}
row = lambda r: f"{r} | dev | 🔨 | 1 | {'c' * 8} | — | — | —"
rows = {"nhóm": {"etask|dev": row("etask"), "collabora|dev": row("collabora")}}
ok = {"nhóm": set(rows["nhóm"])}
out = {}
out["first"] = ew.table_pass(b, sc, rows, ok, {}, 1761000000.0, repos=["etask", "collabora"])
out["same"] = ew.table_pass(b, sc, rows, ok, {}, 1761000060.0, repos=["collabora", "etask"])
rows2 = {"nhóm": {"etask|dev": row("etask")}}
out["dropped"] = ew.table_pass(b, sc, rows2, {"nhóm": set(rows2["nhóm"])}, {}, 1761000120.0, repos=["etask"])
out["drows"] = sorted(b.meta_get("envtable:drows:nhóm"))
b.meta_set("envtable:repos:nhóm", None)
out["legacy"] = ew.table_pass(b, sc, rows2, {"nhóm": set(rows2["nhóm"])}, {}, 1761000180.0, repos=["etask"])
out["legacy_sig"] = b.meta_get("envtable:repos:nhóm")
sent.clear()
out["empty"] = ew.table_pass(b, sc, {"nhóm": {}}, {"nhóm": set()}, {}, 1761000240.0, repos=["prodonly"])
out["empty_sent"] = [len(sent), b.meta_get("envtable:drows:nhóm")]
print(json.dumps(out, ensure_ascii=False))
"""


class EnvRepoSetChangeTest(unittest.TestCase):
    """Đổi tập repo theo dõi ⇒ cả bảng mới (repo bỏ biến mất); cùng tập (khác thứ tự) ⇒ không gửi; bản cũ chưa lưu
    tập repo ⇒ lấy mốc, không gửi lại."""

    def test_repo_set_change_resends_full(self):
        from test_group_board import _run_tchat, _tmpdb
        out = _run_tchat(REPOSIG, _tmpdb())
        self.assertEqual(out["first"], {"nhóm": "full"})
        self.assertEqual(out["same"], {})
        self.assertEqual(out["dropped"], {"nhóm": "full"})
        self.assertEqual(out["drows"], ["etask|dev"])
        self.assertEqual(out["legacy"], {})
        self.assertEqual(out["legacy_sig"], "etask")
        self.assertEqual(out["empty"], {"nhóm": "full"}, "tập mới không có dòng nào ⇒ vẫn gửi bảng rỗng xoá bảng cũ")
        self.assertEqual(out["empty_sent"], [1, {}])


if __name__ == "__main__":
    unittest.main()
