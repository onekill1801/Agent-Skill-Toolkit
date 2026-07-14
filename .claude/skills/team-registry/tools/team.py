#!/usr/bin/env python3
"""Team registry — vai trò, kỹ năng, tính cách của từng người để giao việc & tương tác.

Kho hồ sơ đội nhóm dùng chung cho cả repo: nơi atask_watch (đề xuất ASSIGN đúng
người) và các watcher TChat (điều chỉnh tông giao tiếp) cùng tra cứu. Dữ liệu
ở `work/team.json` — **gitignored** (vai trò/skill/tính cách là thông tin nhạy cảm,
không commit). Thuần stdlib, chạy mọi OS.

Mỗi người là một bản ghi keyed theo `key` (mặc định = username), gồm trường có
CẤU TRÚC để match + ghi chú TỰ DO cho tính cách/cách tương tác:

    {
      "name", "role", "seniority",        # senior|mid|junior|lead
      "skills": [...],                     # ["java","spring","kafka","react"]
      "personality": "...",                # mô tả tính cách (tự do)
      "interaction": "...",                # cách giao tiếp/giao việc hiệu quả (tự do)
      "load": "low|normal|high",           # tải hiện tại (cập nhật tay)
      "handles": {"email","atask_user_id","gitlab_username","fchat_id","fchat_username"},
      "projects": [...], "notes": "..."
    }

Lệnh:
  python team.py list [--format summary|json]
  python team.py get <key>
  python team.py set <key> [--name N] [--role R] [--seniority S] [--skills a,b]
        [--personality T] [--interaction T] [--load low|normal|high]
        [--email E] [--atask-id X] [--gitlab U] [--fchat-id X] [--fchat-username U]
        [--projects a,b] [--notes T]            # upsert: chỉ đổi field được truyền
  python team.py remove <key>
  python team.py match --task "mô tả task" [--skills a,b] [--top N] [--exclude k1,k2]
  python team.py bootstrap --stdin            # nạp skeleton từ JSON [{id,username,displayName,department}]
"""

import argparse
import json
import os
import re
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass


def _repo_root() -> str:
    search = os.path.dirname(os.path.abspath(__file__))
    for _ in range(8):
        if os.path.isfile(os.path.join(search, ".env")) or os.path.isdir(os.path.join(search, ".git")):
            return search
        parent = os.path.dirname(search)
        if parent == search:
            break
        search = parent
    return os.getcwd()


def _store_path() -> str:
    work = os.environ.get("WORK_DIR") or os.path.join(_repo_root(), "work")
    os.makedirs(work, exist_ok=True)
    return os.path.join(work, "team.json")


def load() -> dict:
    try:
        with open(_store_path(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save(data: dict):
    with open(_store_path(), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _blank() -> dict:
    return {"name": "", "role": "", "seniority": "", "skills": [], "personality": "",
            "interaction": "", "load": "normal", "handles": {}, "projects": [], "notes": ""}


def _csv(v):
    return [x.strip() for x in v.split(",") if x.strip()] if v else None


def upsert(key, fields: dict) -> dict:
    data = load()
    rec = data.get(key) or _blank()
    handles = rec.get("handles") or {}
    for k, v in fields.items():
        if v is None:
            continue
        if k in ("email", "atask_user_id", "gitlab_username", "fchat_id", "fchat_username"):
            handles[k] = v
        elif k in ("skills", "projects"):
            rec[k] = v
        else:
            rec[k] = v
    rec["handles"] = handles
    data[key] = rec
    save(data)
    return rec


# ── matching: heuristic shortlist (agent chọn cuối từ shortlist này) ──
_LOAD_SCORE = {"low": 1.0, "normal": 0.0, "high": -1.5, "": 0.0}


def match(task_text: str, skills=None, top=5, exclude=None) -> list:
    """Chấm điểm từng người cho task. Trả shortlist [{key,score,matched,...}] giảm dần.

    Heuristic: mỗi skill của người xuất hiện trong mô tả task (hoặc trong --skills)
    +2; role nhắc trong task +1; tải thấp +1 / cao -1.5; lead/senior nhỉnh nhẹ khi
    hoà điểm. Đây là BƯỚC LỌC — agent đọc shortlist + hồ sơ đầy đủ để chọn cuối."""
    data = load()
    exclude = set(exclude or [])
    text = (task_text or "").lower()
    want = {s.lower() for s in (skills or [])}
    out = []
    for key, rec in data.items():
        if key in exclude:
            continue
        matched = []
        score = 0.0
        for sk in rec.get("skills", []):
            skl = sk.lower()
            if skl and (skl in text or skl in want):
                score += 2.0
                matched.append(sk)
        role = (rec.get("role") or "").lower()
        if role and any(tok and tok in text for tok in re.split(r"[\s/,]+", role)):
            score += 1.0
        score += _LOAD_SCORE.get(rec.get("load", "normal"), 0.0)
        sen = (rec.get("seniority") or "").lower()
        if sen in ("lead", "senior"):
            score += 0.3
        out.append({"key": key, "name": rec.get("name") or key, "role": rec.get("role"),
                    "seniority": rec.get("seniority"), "load": rec.get("load"),
                    "score": round(score, 2), "matched_skills": matched,
                    "all_skills": rec.get("skills", [])})
    out.sort(key=lambda x: x["score"], reverse=True)
    return out[:top]


def bootstrap_from(participants: list) -> dict:
    """Tạo skeleton từ [{id,username,displayName,department}] — KHÔNG đè người đã có."""
    data = load()
    added = []
    for p in participants:
        uname = p.get("username") or p.get("id")
        if not uname or uname in data:
            continue
        rec = _blank()
        rec["name"] = p.get("displayName") or uname
        rec["notes"] = f"dept: {p.get('department','')}".strip()
        rec["handles"] = {"fchat_id": p.get("id"), "fchat_username": p.get("username")}
        data[uname] = rec
        added.append(uname)
    save(data)
    return {"added": added, "total": len(data)}


def _summary(data: dict) -> str:
    if not data:
        return "(trống — chưa có hồ sơ. Dùng `team.py bootstrap` hoặc `team.py set`.)"
    lines = []
    for key, r in data.items():
        sk = ", ".join(r.get("skills", [])) or "-"
        lines.append(f"• {key} — {r.get('name','')} | {r.get('role') or '?'}"
                     f"/{r.get('seniority') or '?'} | tải={r.get('load','?')} | skills: {sk}")
    return "\n".join(lines)


if __name__ == "__main__":
    p = argparse.ArgumentParser(prog="team.py")
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("list").add_argument("--format", default="summary", choices=["summary", "json"])
    g = sub.add_parser("get"); g.add_argument("key")
    r = sub.add_parser("remove"); r.add_argument("key")

    s = sub.add_parser("set")
    s.add_argument("key")
    for opt in ("name", "role", "seniority", "personality", "interaction", "load",
                "email", "atask-id", "gitlab", "fchat-id", "fchat-username", "notes"):
        s.add_argument(f"--{opt}")
    s.add_argument("--skills"); s.add_argument("--projects")

    m = sub.add_parser("match")
    m.add_argument("--task", required=True); m.add_argument("--skills")
    m.add_argument("--top", type=int, default=5); m.add_argument("--exclude")

    b = sub.add_parser("bootstrap"); b.add_argument("--stdin", action="store_true")

    a = p.parse_args()
    if a.cmd == "list":
        d = load()
        print(json.dumps(d, ensure_ascii=False, indent=2) if a.format == "json" else _summary(d))
    elif a.cmd == "get":
        print(json.dumps(load().get(a.key) or {"error": f"no such key: {a.key}"},
                         ensure_ascii=False, indent=2))
    elif a.cmd == "remove":
        d = load(); d.pop(a.key, None); save(d); print(json.dumps({"removed": a.key}))
    elif a.cmd == "set":
        fields = {
            "name": a.name, "role": a.role, "seniority": a.seniority,
            "personality": a.personality, "interaction": a.interaction, "load": a.load,
            "email": a.email, "atask_user_id": getattr(a, "atask_id"),
            "gitlab_username": a.gitlab, "fchat_id": getattr(a, "fchat_id"),
            "fchat_username": getattr(a, "fchat_username"), "notes": a.notes,
            "skills": _csv(a.skills), "projects": _csv(a.projects),
        }
        print(json.dumps(upsert(a.key, fields), ensure_ascii=False, indent=2))
    elif a.cmd == "match":
        print(json.dumps(match(a.task, _csv(a.skills), a.top, _csv(a.exclude)),
                         ensure_ascii=False, indent=2))
    elif a.cmd == "bootstrap":
        parts = json.load(sys.stdin) if a.stdin else []
        print(json.dumps(bootstrap_from(parts), ensure_ascii=False, indent=2))
    else:
        print(__doc__)
