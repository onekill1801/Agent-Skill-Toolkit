#!/usr/bin/env python3
"""Báo thay đổi môi trường vào nhóm bảng việc (group-workboard §5.3, Pha 2b).

Mỗi FCHAT_ENV_WATCH_INTERVAL giây (mặc định 600): với mỗi repo trong FCHAT_ENV_REPOS, đọc snapshot từng env
(cùng nguồn với `@bot env`: k8s + GitLab tag + Jenkins). Env đổi phiên bản / commit / digest ⇒
  * env báo ngay (mặc định mọi env trừ FCHAT_ENV_DIGEST_ENVS): 1 thẻ E-<repo>-<env> cho mỗi nhóm FCHAT_BOARD_GROUPS
  * env gom (mặc định `dev` — đổi nhiều lần/ngày): vào bản tổng hợp gửi lúc FCHAT_ENV_DIGEST_AT (08:30)
Thẻ E liệt kê MR thêm / mất (mã R nếu MR có thẻ review) + link so sánh + link build.

An toàn:
  * FCHAT_ENV_WATCH_MODE=shadow (MẶC ĐỊNH): KHÔNG gửi gì — ghi temp/env_watch/shadow.jsonl + in ra; chạy vài ngày,
    đối chiếu tay, rồi mới đặt `send`.
  * Lần đầu thấy một env: chỉ lưu mốc, không báo. Env không đọc được lần này: không coi là thay đổi.
  * Mốc chỉ được cập nhật SAU khi đã báo (gửi lỗi ⇒ lượt sau thử lại).

Chạy:  python env_watch.py [--once] [--interval 600]
       supervise: python supervise.py --name env_watch --heartbeat env_watch --stale-after 1800 -- python env_watch.py
"""

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import group_watch as gw  # noqa: E402  (registry / snapshots / send helpers, Pha 2a)
from agent_skill_toolkit.core import board_store, paths  # noqa: E402
from agent_skill_toolkit.workflows.group_board import envmap, envwatch  # noqa: E402

REQ_MAX_AGE_S = 12 * 3600
REQ_POLL_S = 3          # kiểm yêu cầu "@bot env" mỗi 3 s (chu kỳ đọc môi trường vẫn là FCHAT_ENV_WATCH_INTERVAL)
SLIM = ("env", "version", "commit", "digest", "level", "rs_created_at", "build", "observed_at", "branch")


def _cfg(key, default=""):
    return (gw.rccfg.get(key) or default).strip()


def _csv(key, default=""):
    return [x.strip() for x in _cfg(key, default).split(",") if x.strip()]


def _slim(s: dict) -> dict:
    return {k: s.get(k) for k in SLIM if s.get(k) not in (None, "")}


def _shadow(record: dict) -> None:
    path = paths.state_dir("env_watch", "shadow.jsonl")
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%S"), **record}, ensure_ascii=False) + "\n")
    print(f"[{gw._now()}] [shadow] {record.get('kind')} → {record.get('group')}:\n{record.get('text')}\n")


def deliver(board, gid: str, text: str, card=None, mode: str = "shadow") -> bool:
    """card = (card_id, ref, level) for an E card. Returns True when delivered (or logged in shadow)."""
    if mode != "send":
        _shadow({"kind": "E" if card else "digest", "group": gid, "card": card and card[0], "text": text})
        return True
    if card:
        cid, ref, level = card
        c = board.card_open(gid, "E", ref, title=ref, card_id=cid)
        board.card_update(gid, c["id"], state=level)
    res = gw.send.send_text(gid, text, None, card_id=card[0] if card else "")
    ok = isinstance(res, dict) and not res.get("error")
    if not ok:
        print(f"[{gw._now()}] [WARN] gửi {card and card[0] or 'tổng hợp'} lỗi: {(res or {}).get('message')}",
              file=sys.stderr)
    return ok


def _r_code(board, gid, pid):
    return lambda iid: (board.card_get(gid, kind="R", ref=f"{pid}!{iid}") or {}).get("id")


MAX_DEFER = 3            # GitLab failing to list MRs: postpone the notice this many passes, then send it anyway


def _k(mode: str, *parts) -> str:
    """board_meta key scoped by MODE: shadow and send never share a baseline, a queue or a digest date."""
    return ":".join(("env", mode) + tuple(str(p) for p in parts))


def _digest_due(board, mode: str, now: float) -> bool:
    at = _cfg("FCHAT_ENV_DIGEST_AT", "08:30")
    try:
        hh, mm = (int(x) for x in at.split(":"))
    except ValueError:
        hh, mm = 8, 30
    lt = time.localtime(now)
    today = time.strftime("%Y-%m-%d", lt)
    return (lt.tm_hour, lt.tm_min) >= (hh, mm) and board.meta_get(_k(mode, "digest", "last")) != today


def _protected(proj: dict) -> set:
    return {p.lower() for p in (proj.get("protected_envs") or ("prod", "production"))}


# ---------------------------------------------------------------- bảng môi trường của extension (chỉ gửi thay đổi)
# MỘT nơi gửi duy nhất (tiến trình này): số thứ tự #seq, tin chờ và "dòng đã gửi" nằm trong board_meta, ghi trong
# transaction TRƯỚC khi gửi ⇒ crash giữa chừng chỉ gửi lại đúng tin đó (cùng số) cho nơi còn thiếu. Độc lập với
# FCHAT_ENV_WATCH_MODE (shadow chỉ áp cho thẻ E / tổng hợp). "@bot env" (group_watch) chỉ xếp yêu cầu vào envtable:req.
REQ_KEY = "envtable:req"


def _tk(*parts) -> str:
    return ":".join(("envtable",) + tuple(str(p) for p in parts))


def table_scopes(groups: list, owner_group: str) -> dict:
    """"nhóm" (không bao giờ prod) → mọi nhóm bảng việc · "chủ nhóm" (có prod) → nhóm riêng của chủ nhóm."""
    scopes = {"nhóm": list(groups)} if groups else {}
    if owner_group:
        scopes["chủ nhóm"] = [owner_group]
    return scopes


def collect_rows(key: str, proj: dict, snaps: list, scopes: dict, rows: dict, ok: dict) -> None:
    prot = _protected(proj)
    for s in snaps:
        env = s["env"]
        for sc in scopes:
            if sc == "nhóm" and env.lower() in prot:
                continue                                  # prod không bao giờ vào bảng chung
            k = f"{key}|{env}"
            rows[sc][k] = envmap.snapshot_row(key, s)
            if s.get("version") or s.get("commit"):
                ok[sc].add(k)                             # đọc được lần này (đọc lỗi ⇒ không phải thay đổi)


def read_all_rows(scopes: dict, owner_group: str):
    """Đọc mới mọi repo (bỏ cache) ⇒ (rows, ok, webs) cho các scope."""
    rows, ok, webs = {sc: {} for sc in scopes}, {sc: set() for sc in scopes}, {}
    reg = gw._registry()
    for key in _csv("FCHAT_ENV_REPOS"):
        proj = reg.get(key)
        if not isinstance(proj, dict) or not proj.get("gitlab_project_id"):
            continue
        with gw._env_cache_lock:
            for k in [k for k in gw._env_cache if k[0] == key]:
                gw._env_cache.pop(k, None)
        webs[key] = gw._gitlab_web(proj)
        collect_rows(key, proj, gw._env_snapshots(key, proj, show_prod=bool(owner_group)), scopes, rows, ok)
    return rows, ok, webs


def _send_table(gid: str, text: str) -> bool:
    res = gw.send.send_text(gid, text, None)
    ok = isinstance(res, dict) and not res.get("error")
    if not ok:
        print(f"[{gw._now()}] [WARN] gửi bảng môi trường tới {gid} lỗi: {(res or {}).get('message')}", file=sys.stderr)
    return ok


def _flush_pending(board, sc: str) -> bool:
    """Gửi tin chờ của scope cho nơi còn thiếu (tiến độ ghi sau MỖI phần, MỖI nơi); đủ ⇒ dòng đã gửi + xoá tin chờ,
    một transaction. Tin nhiều phần: nơi nhận tiếp tục từ phần chưa gửi. Bảo đảm ÍT NHẤT MỘT LẦN: crash ngay sau khi
    gửi mà chưa ghi tiến độ ⇒ đúng phần đó gửi lại (cùng nội dung; extension ghép trùng phần vô hại)."""
    pend = board.meta_get(_tk("pend", sc))
    if not pend:
        return True
    texts = pend.get("texts") or [pend["text"]]
    done_parts = pend.setdefault("parts", {})
    for g in list(pend["left"]):
        while done_parts.get(g, 0) < len(texts):
            if not _send_table(g, texts[done_parts.get(g, 0)]):
                break
            done_parts[g] = done_parts.get(g, 0) + 1
            board.meta_set(_tk("pend", sc), pend)
        if done_parts.get(g, 0) >= len(texts):
            pend["left"].remove(g)
            board.meta_set(_tk("pend", sc), pend)
    if pend["left"]:
        return False

    def done(get, put):
        d = get(_tk("drows", sc)) or {}
        d.update(pend["rows"])
        put(_tk("drows", sc), d)
        put(_tk("pend", sc), None)
    board.meta_txn(done)
    return True


def _queue(board, sc: str, targets: list, kind: str, rows: dict, webs: dict, now: float) -> int:
    """Cấp số + dựng tin + ghi tin chờ TRONG MỘT transaction, rồi mới gửi (_flush_pending)."""
    def q(get, put):
        seq = int(get(_tk("seq", sc), 0) or 0) + 1
        texts = [gw.board_render.defuse_body(t) for t in envmap.table_parts(kind, rows, webs, sc, seq, now)]
        put(_tk("seq", sc), seq)
        put(_tk("pend", sc), {"seq": seq, "texts": texts, "left": list(targets), "rows": rows})
        return seq
    return board.meta_txn(q)


def table_pass(board, scopes: dict, rows: dict, ok: dict, webs: dict, now: float, requests=()) -> dict:
    """Một lượt: tin chờ trước; chưa có mốc ⇒ cả bảng (đánh số); có dòng đổi ⇒ tin thay đổi (đánh số) cho MỌI nơi nhận;
    rồi trả lời yêu cầu "@bot env": cả bảng hiện tại CHỈ cho nơi hỏi, mang số hiện tại (không cấp số mới ⇒ nơi khác
    không bị báo lỡ tin). Dòng đọc lỗi trong cả bảng: dùng dòng tốt gần nhất đã gửi."""
    out = {}
    for sc, targets in scopes.items():
        if not _flush_pending(board, sc):
            out[sc] = "pending"
            continue
        drows = board.meta_get(_tk("drows", sc))
        if drows is None:
            if rows[sc]:
                _queue(board, sc, targets, "SNAPSHOT", dict(rows[sc]), webs, now)
                out[sc] = "full"
        else:
            diff = {k: r for k, r in rows[sc].items() if k in ok[sc] and drows.get(k) != r}
            if diff:
                _queue(board, sc, targets, "DELTA", diff, webs, now)
                out[sc] = "delta"
        if out.get(sc) and not _flush_pending(board, sc):
            out[sc] = "pending"
            continue
        for r in [r for r in requests if r.get("scope") == sc]:
            r["done"] = _answer(board, sc, r, rows, webs, now)
    return out


def _answer(board, sc: str, r: dict, rows: dict, webs: dict, now: float) -> bool:
    """Cả bảng cho một nơi hỏi. Các phần dựng MỘT lần và lưu bền (envtable:ans:<req>:<scope>:<group>) cùng tiến độ
    sau MỖI phần ⇒ lỗi / crash gửi tiếp đúng các phần cũ (cùng giờ + số: extension ghép được), không dựng lại.
    Crash ngay sau khi gửi mà chưa kịp ghi tiến độ ⇒ phần đó gửi lại (ít nhất một lần; extension ghép trùng vô hại)."""
    key = _tk("ans", r.get("req") or "-", sc, r["group"])
    a = board.meta_get(key)
    if not a:
        d = board.meta_get(_tk("drows", sc)) or {}
        cur = {k: d.get(k) or rows[sc][k] for k in rows[sc]}
        a = {"texts": [gw.board_render.defuse_body(t) for t in envmap.table_parts(
            "SNAPSHOT", cur, webs, sc, int(board.meta_get(_tk("seq", sc), 0) or 0), now)], "sent": 0}
        board.meta_set(key, a)
    while a["sent"] < len(a["texts"]):
        if not _send_table(r["group"], a["texts"][a["sent"]]):
            return False
        a["sent"] += 1
        board.meta_set(key, a)
    board.meta_set(key, None)
    return True


def _drop_stale(reqs: list, put, now: float) -> list:
    """Yêu cầu quá cũ (env_watch dừng rất lâu, người hỏi đã được báo): bỏ, cùng các phần trả lời đã dựng sẵn của nó."""
    keep = []
    for q in reqs:
        if now - (q.get("at") or now) <= REQ_MAX_AGE_S:
            keep.append(q)
            continue
        for a in q.get("left") or []:
            put(_tk("ans", q.get("id") or "-", a["scope"], a["group"]), None)
    return keep


def serve_requests(board, now: float = None) -> list:
    """"@bot env" đã xếp hàng (group_watch) ⇒ đọc mới, gửi thay đổi nếu có, rồi cả bảng cho từng nơi hỏi.
    Hàng yêu cầu BỀN: không lấy ra trước; mỗi yêu cầu {id, left:[nơi nhận]} chỉ bớt nơi đã nhận, xoá khi đủ
    (crash / lỗi giữa chừng ⇒ yêu cầu còn nguyên phần chưa gửi; gửi lại cả bảng thì vô hại — cùng số hiện tại)."""
    now = now or time.time()
    reqs = [r for r in (board.meta_get(REQ_KEY, []) or []) if now - (r.get("at") or now) <= REQ_MAX_AGE_S]
    if not reqs:                                      # hàng chỉ còn yêu cầu quá cũ: dọn, không gửi gì (Codex resume R1)
        board.meta_txn(lambda get, put: put(REQ_KEY, _drop_stale(get(REQ_KEY, []) or [], put, now)))
        return []
    owner_group = _cfg("FCHAT_ENV_OWNER_GROUP", "").strip()
    scopes = table_scopes(_csv("FCHAT_BOARD_GROUPS"), owner_group)
    asks = []
    for r in reqs:
        left = r.get("left")
        if left is None:                              # yêu cầu mới: nơi nhận = nhóm hỏi (+ nhóm riêng nếu chủ nhóm hỏi)
            left = [{"scope": "nhóm", "group": r.get("group")}]
            if r.get("owner") and owner_group:
                left.append({"scope": "chủ nhóm", "group": owner_group})
        asks += [{**a, "req": r.get("id")} for a in left]
    rows, ok, webs = read_all_rows(scopes, owner_group)
    table_pass(board, scopes, rows, ok, webs, now, asks)

    def settle(get, put):                             # theo id: yêu cầu mới xếp thêm trong lúc gửi vẫn giữ nguyên
        keep = []
        for q in _drop_stale(get(REQ_KEY, []) or [], put, now):
            mine = [a for a in asks if a["req"] == q.get("id")]
            if not mine:
                keep.append(q)
                continue
            left = [{"scope": a["scope"], "group": a["group"]} for a in mine if not a.get("done")]
            if left:
                keep.append({**q, "left": left})
        put(REQ_KEY, keep)
    board.meta_txn(settle)
    return asks


def _target(s: dict) -> str:
    """What a notice is about: the env now running this commit + digest (idempotency key)."""
    return f"{s.get('version')}|{s.get('commit')}|{s.get('digest')}"


def _card_for(board, gid, key, env):
    ref = f"{key}:{env}"
    cid = envwatch.card_id(key, env, lambda c: (board.card_get(gid, c) or {}).get("ref"))
    return cid, ref


def tick(board, now: float = None, mode: str = None) -> dict:
    """One pass. Returns what happened (for logs / tests).
    State (board_meta, per MODE): snap:<repo>:<env> = baseline · sent:<repo>:<env> = groups already told about the
    pending target · defer:<repo>:<env> = passes postponed for a GitLab error · queue:<group> = digest items ·
    digest:last / digest:since."""
    now = now or time.time()
    mode = mode or _cfg("FCHAT_ENV_WATCH_MODE", "shadow")
    groups, repos = _csv("FCHAT_BOARD_GROUPS"), _csv("FCHAT_ENV_REPOS")
    batch_envs = {e.lower() for e in _csv("FCHAT_ENV_DIGEST_ENVS", "dev")}
    out = {"mode": mode, "baseline": [], "changes": [], "delivered": [], "batched": [], "deferred": [],
           "digest": False, "table": {}}
    owner_group = _cfg("FCHAT_ENV_OWNER_GROUP", "").strip()
    scopes = table_scopes(groups, owner_group)
    t_rows, t_ok, webs = {sc: {} for sc in scopes}, {sc: set() for sc in scopes}, {}
    reg = gw._registry()
    for key in repos:
        proj = reg.get(key)
        if not isinstance(proj, dict) or not proj.get("gitlab_project_id"):
            continue
        with gw._env_cache_lock:                      # fresh reads every pass
            for k in [k for k in gw._env_cache if k[0] == key]:
                gw._env_cache.pop(k, None)
        snaps = gw._env_snapshots(key, proj, show_prod=bool(owner_group))   # prod chỉ đọc khi có nhóm riêng
        gl = gw.gitlab_api.Client(proj["gitlab_project_id"])
        web, pid = gw._gitlab_web(proj), proj["gitlab_project_id"]
        webs[key] = web
        prot = _protected(proj)
        collect_rows(key, proj, snaps, scopes, t_rows, t_ok)
        for s in snaps:
            env = s["env"]
            if env.lower() in prot:
                continue                              # prod: không thẻ E / tổng hợp cho nhóm chung
            if s.get("level") == "built":
                continue                              # theo Jenkins, không phải pod đang chạy: chỉ vào bảng, không thẻ E
            prev = board.meta_get(_k(mode, "snap", key, env))
            if prev is None:
                if s.get("version"):
                    board.meta_set(_k(mode, "snap", key, env), _slim(s))
                    out["baseline"].append(f"{key}/{env}")
                continue
            if not envwatch.changed(prev, s):
                continue
            target = _target(s)
            added = envwatch.mrs_between(prev.get("commit"), s.get("commit"), gl.compare_commits, gl.commit_mrs,
                                         s.get("branch") or "")
            lost = envwatch.mrs_between(s.get("commit"), prev.get("commit"), gl.compare_commits, gl.commit_mrs,
                                        s.get("branch") or "")
            if added.get("error") or lost.get("error"):
                d = board.meta_get(_k(mode, "defer", key, env), {})
                n = (d.get("n", 0) + 1) if d.get("target") == target else 1
                board.meta_set(_k(mode, "defer", key, env), {"target": target, "n": n})
                if n < MAX_DEFER:                     # GitLab hiccup: do not announce a wrong / empty MR list
                    out["deferred"].append(f"{key}/{env}")
                    continue
            out["changes"].append(f"{key}/{env}: {prev.get('version')} → {s.get('version')}")
            sent = board.meta_get(_k(mode, "sent", key, env), {})
            done = set(sent.get("groups") or []) if sent.get("target") == target else set()
            for gid in groups:
                if gid in done:
                    continue
                if env.lower() in batch_envs:
                    q = board.meta_get(_k(mode, "queue", gid), [])
                    if not any(it.get("id") == f"{key}|{env}|{target}" for it in q):   # idempotent enqueue
                        q.append({"id": f"{key}|{env}|{target}", "repo": key, "env": env, "prev": prev,
                                  "cur": _slim(s), "added": added, "lost": lost})
                        board.meta_set(_k(mode, "queue", gid), q)
                    out["batched"].append(f"{gid}:{key}/{env}")
                    done.add(gid)
                else:
                    cid, ref = _card_for(board, gid, key, env)
                    text = envwatch.e_card_text(key, web, prev, s, added, lost, _r_code(board, gid, pid), cid)
                    if deliver(board, gid, text, (cid, ref, s.get("level") or "unknown"), mode):
                        out["delivered"].append(f"{gid}:{cid}")
                        done.add(gid)
                board.meta_set(_k(mode, "sent", key, env), {"target": target, "groups": sorted(done)})
            if set(groups) <= done:                   # everyone told: the baseline moves, bookkeeping cleared
                board.meta_set(_k(mode, "snap", key, env), _slim(s))
                board.meta_set(_k(mode, "sent", key, env), {})
                board.meta_set(_k(mode, "defer", key, env), {})
    # bảng môi trường: chỉ gửi THAY ĐỔI (bot trên máy chạy quyết định lúc nào); chưa có mốc ⇒ cả bảng một lần
    out["table"] = table_pass(board, scopes, t_rows, t_ok, webs, now)
    if groups and _digest_due(board, mode, now):
        since = board.meta_get(_k(mode, "digest", "since"), now - 86400)
        all_ok = True
        for gid in groups:
            items = board.meta_get(_k(mode, "queue", gid), [])
            if not items:
                continue
            by_repo = {}
            for it in items:
                by_repo.setdefault(it["repo"], []).append((it["env"], it["prev"], it["cur"], it["added"], it["lost"]))
            if deliver(board, gid, envwatch.digest_text(by_repo, since, now), None, mode):
                board.meta_set(_k(mode, "queue", gid), [])
            else:
                all_ok = False                        # retried next pass, the date is not marked
        if all_ok:
            board.meta_set(_k(mode, "digest", "last"), time.strftime("%Y-%m-%d", time.localtime(now)))
            board.meta_set(_k(mode, "digest", "since"), now)
            out["digest"] = True
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="env_watch.py")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--interval", type=int, default=int(_cfg("FCHAT_ENV_WATCH_INTERVAL", "600") or 600))
    a = ap.parse_args(argv)
    board = board_store.Board()
    print(f"[{gw._now()}] env_watch: repos={_csv('FCHAT_ENV_REPOS')} groups={_csv('FCHAT_BOARD_GROUPS')} "
          f"mode={_cfg('FCHAT_ENV_WATCH_MODE', 'shadow')} interval={a.interval}s")
    last = 0.0
    while True:
        if gw.daemon_common is not None:
            gw.daemon_common.heartbeat("env_watch")
        try:
            if time.time() - last >= max(60, a.interval):
                last = time.time()
                res = tick(board)
                if res["changes"] or res["baseline"] or res["digest"] or any(res["table"].values()):
                    print(f"[{gw._now()}] {json.dumps(res, ensure_ascii=False)}")
            served = serve_requests(board)          # "@bot env": trả lời trong vài giây, không chờ chu kỳ
            if served:
                print(f"[{gw._now()}] bảng môi trường theo yêu cầu: {served}")
        except Exception as e:  # noqa: BLE001 — one bad pass must not stop the watcher
            print(f"[{gw._now()}] [WARN] env_watch: {type(e).__name__}: {e}", file=sys.stderr)
        if a.once:
            return 0
        time.sleep(REQ_POLL_S)


if __name__ == "__main__":
    sys.exit(main())
