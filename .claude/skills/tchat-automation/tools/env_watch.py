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
from agent_skill_toolkit.workflows.group_board import envwatch  # noqa: E402

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
           "digest": False}
    reg = gw._registry()
    for key in repos:
        proj = reg.get(key)
        if not isinstance(proj, dict) or not proj.get("gitlab_project_id"):
            continue
        with gw._env_cache_lock:                      # fresh reads every pass
            for k in [k for k in gw._env_cache if k[0] == key]:
                gw._env_cache.pop(k, None)
        snaps = gw._env_snapshots(key, proj)
        gl = gw.gitlab_api.Client(proj["gitlab_project_id"])
        web, pid = gw._gitlab_web(proj), proj["gitlab_project_id"]
        for s in snaps:
            env = s["env"]
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
    while True:
        if gw.daemon_common is not None:
            gw.daemon_common.heartbeat("env_watch")
        try:
            res = tick(board)
            if res["changes"] or res["baseline"] or res["digest"]:
                print(f"[{gw._now()}] {json.dumps(res, ensure_ascii=False)}")
        except Exception as e:  # noqa: BLE001 — one bad pass must not stop the watcher
            print(f"[{gw._now()}] [WARN] env_watch: {type(e).__name__}: {e}", file=sys.stderr)
        if a.once:
            return 0
        time.sleep(max(60, a.interval))


if __name__ == "__main__":
    sys.exit(main())
