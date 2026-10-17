"""Editable toolkit settings — ONE catalog for the dashboard and `settings.py` (checklist F.17).

Only NON-SECRET behaviour switches are editable. Tokens/passwords are never read out or written here:
`secrets_status()` only says which secret keys are set. Two kinds of setting:

  * env    — a key in the toolkit .env (written by core.env.set_values: in place, atomic, verified).
             Running daemons keep the value they loaded at start: each entry names the tabs to restart.
  * store  — a policy row in temp/toolkit.db (capacity, daily agent-run cap): live at once.

Every change is validated against the catalog (type, choices, range), checked against the value the
caller saw (`expected`: someone else changed it -> refused) and appended to temp/settings_audit.jsonl.

    from agent_skill_toolkit.workflows import settings
    settings.snapshot()                                    # [{key, label, help, kind, value, default, ...}]
    settings.apply({"REVIEW_AUTO_POST": "auto"}, by="dashboard", expected={"REVIEW_AUTO_POST": "shadow"})
"""

import json
import os
import re
import time

from agent_skill_toolkit.core import env, locks, paths
from agent_skill_toolkit.core import store as store_mod

COORD = "Coordinator"
GROUP_WATCH = "TChat group_watch"
TG_CODE = "TG bot CODE"
TG_OPS = "TG bot OPS"

_ID = r"[0-9a-fA-F]{24}"
_CSV_ITEM = re.compile(r"[^,]+")


def _s(key, group, label, help, kind, default="", *, choices=(), lo=None, hi=None, restart=(), store=False,
       sensitive=False, item=None):
    return {"key": key, "group": group, "label": label, "help": help, "kind": kind, "default": default,
            "choices": list(choices), "lo": lo, "hi": hi, "restart": list(restart), "store": store,
            "sensitive": sensitive, "item": item}


CATALOG = [
    # ---------------------------------------------------------------- review MR
    _s("REVIEW_AUTO_POST", "Review MR", "Chế độ đăng review",
       "off = luôn hỏi duyệt · shadow = chấm luật nhưng vẫn hỏi · auto = qua đủ luật thì tự đăng lên GitLab",
       "enum", "shadow", choices=("off", "shadow", "auto"), restart=(COORD, GROUP_WATCH), sensitive=True),
    _s("REVIEW_AUTO_POST_PROJECTS", "Review MR", "Project được tự đăng",
       "tên trong work/projects.json, đường dẫn GitLab hoặc id — cách nhau dấu phẩy; trống = không project nào",
       "csv", "", restart=(COORD, GROUP_WATCH), item=r"[\w./-]+", sensitive=True),
    _s("REVIEW_AUTO_POST_SOURCES", "Review MR", "Nguồn yêu cầu được tự đăng",
       "manual (gõ tay) · mr_watch · fchat (group TChat)", "multi", "manual,mr_watch,fchat",
       choices=("manual", "mr_watch", "fchat"), restart=(COORD, GROUP_WATCH)),
    _s("REVIEW_AUTO_POST_DAILY_MAX", "Review MR", "Số lần tự đăng tối đa / ngày",
       "quá số này thì quay về hỏi duyệt", "int", "30", lo=0, hi=500, restart=(COORD, GROUP_WATCH)),
    _s("REVIEW_NOTE_STYLE", "Review MR", "Độ dài comment review",
       "full = mẫu Code Review Summary của team (mặc định) · compact = gọn (tóm tắt + mỗi lỗi 1–2 dòng, không bảng)",
       "enum", "full", choices=("full", "compact"), restart=(COORD, GROUP_WATCH)),
    _s("REVIEW_INLINE_COMMENTS", "Review MR", "Comment thẳng vào dòng code",
       "1 = lỗi có file:dòng được đăng THÊM thành comment trên đúng dòng trong diff (tác giả resolve từng cái) · "
       "0 = tất cả gom trong một comment", "bool", "0", restart=(COORD, GROUP_WATCH)),
    _s("REVIEW_KEEP_WORKTREES", "Review MR", "Giữ worktree sau khi review",
       "0 = xoá ngay khi review đã đăng (Orca không dồn mục) · 1 = giữ tới khi chạy review_ws gc",
       "bool", "0", restart=(COORD, GROUP_WATCH)),
    _s("REVIEW_CODEGRAPH", "Review MR", "Dùng CodeGraph khi chuẩn bị review",
       "1 = có (nơi gọi ngoài diff, test bị ảnh hưởng; chậm hơn ~1,5 phút) · 0 = tắt", "bool", "1",
       restart=(COORD,)),
    # ---------------------------------------------------------------- TChat
    _s("FCHAT_WATCH_GROUP", "TChat", "Group được nghe",
       "id group TChat (24 ký tự hex) mà group_watch theo dõi", "text", "", item=_ID, restart=(GROUP_WATCH,)),
    _s("FCHAT_REVIEW_ALLOWED", "TChat", "Ai được gửi yêu cầu",
       "* = mọi người trong group · danh sách user id cách nhau dấu phẩy · trống = chỉ chủ token",
       "csv", "", item=r"\*|" + _ID, restart=(GROUP_WATCH,), sensitive=True),
    _s("FCHAT_WATCH_KEYWORDS", "TChat", "Từ khoá kích hoạt",
       "tin của người khác phải chứa một trong các cụm này; trống = mặc định (mr, review, build, deploy, lên dev…)",
       "csv", "", item=r"[^,]{1,40}", restart=(GROUP_WATCH,)),
    _s("FCHAT_SELF_PREFIX", "TChat", "Tiền tố lệnh của chính bạn",
       "tin của chủ token chỉ được xử lý khi bắt đầu bằng tiền tố này (vd @bot review MR 412)", "text", "@bot",
       item=r"\S{1,20}", restart=(GROUP_WATCH,)),
    _s("FCHAT_WATCH_FREEFORM", "TChat", "Nhận cả việc khác review/build",
       "1 = tin khớp từ khoá nhưng không phải review/build vẫn giao cho agent", "bool", "0",
       restart=(GROUP_WATCH,), sensitive=True),
    _s("FCHAT_WATCH_AUTO_APPROVE", "TChat", "Lượt group tự duyệt THÊM",
       "CỘNG THÊM vào 'Bot CODE tự duyệt nhóm' (không thay thế): lượt group được tự duyệt = hợp của hai cài đặt. "
       "read = chỉ đọc · file = sửa file · bash = lệnh shell ghi; còn lại hỏi nút Duyệt trên Telegram",
       "multi", "read", choices=("read", "file", "bash"), restart=(GROUP_WATCH,), sensitive=True),
    _s("FCHAT_WATCH_TIMEOUT", "TChat", "Thời gian tối đa một lượt agent (giây)", "", "int", "1800",
       lo=60, hi=7200, restart=(GROUP_WATCH,)),
    # ---------------------------------------------------------------- Telegram
    _s("TELEGRAM_APPROVAL_TIMEOUT", "Telegram", "Chờ bấm Duyệt (giây)",
       "không bấm trong khoảng này thì yêu cầu tự bị từ chối", "int", "300", lo=30, hi=86400,
       restart=(COORD, GROUP_WATCH, TG_CODE)),
    _s("TELEGRAM_AUTO_APPROVE", "Telegram", "Bot CODE tự duyệt nhóm",
       "read · file · bash — nhóm thao tác bot CODE chạy không hỏi ('danger' luôn hỏi); cũng là MỨC SÀN "
       "của lượt group TChat", "multi", "read,file",
       choices=("read", "file", "bash"), restart=(TG_CODE, GROUP_WATCH), sensitive=True),   # group turns inherit it (R4 #1)
    # ---------------------------------------------------------------- coordinator (store, live)
    _s("capacity", "Điều phối", "Số việc chạy song song",
       "giới hạn toàn cục cho worker (tab Coordinator); 0 = không giới hạn (worker chạy 2 luồng); áp dụng ngay",
       "int", str(store_mod.max_running_default()), lo=0, hi=50, store=True),
    _s("daily_agent_runs", "Điều phối", "Trần lượt agent / ngày",
       "0 = không giới hạn; chạm trần thì dừng tới 0 giờ; áp dụng ngay", "int", "0", lo=0, hi=100000, store=True),
]
BY_KEY = {s["key"]: s for s in CATALOG}

# keys whose VALUE is a credential: only "set / not set" ever leaves the process
_SECRET_NAME = re.compile(r"TOKEN|PASSWORD|SECRET|(?:^|_)PAT$|_PAT_|API_KEY|PRIVATE", re.I)


def env_path() -> str:
    """The .env every tool reads: $TOOLKIT_ENV_FILE, else <toolkit root>/.env."""
    explicit = os.environ.get("TOOLKIT_ENV_FILE", "").strip()
    return explicit or os.path.join(paths.toolkit_root(), ".env")


def _read_env(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return env.parse(f.read())
    except FileNotFoundError:
        return {}


def validate(key: str, value) -> str:
    """'' if `value` is acceptable for catalog entry `key`, else the reason (Vietnamese, for the UI)."""
    s = BY_KEY.get(key)
    if not s:
        return "không phải cài đặt chỉnh được"
    why = env.check_value(value)
    if why:
        return why
    k = s["kind"]
    if k == "enum":
        return "" if value in s["choices"] else f"chọn một trong: {', '.join(s['choices'])}"
    if k == "bool":
        return "" if value in ("0", "1") else "chỉ 0 hoặc 1"
    if k == "int":
        if not re.fullmatch(r"\d{1,9}", value):
            return "phải là số nguyên không âm"
        n = int(value)
        return "" if s["lo"] <= n <= s["hi"] else f"trong khoảng {s['lo']}–{s['hi']}"
    if k == "multi":
        items = value.split(",") if value else []
        if not items:
            return "chọn ít nhất một mục"
        bad = [i for i in items if i not in s["choices"]]
        return f"không hợp lệ: {', '.join(bad)}" if bad else ("" if len(set(items)) == len(items) else "trùng mục")
    if k in ("csv", "text") and value:
        items = [i.strip() for i in value.split(",")] if k == "csv" else [value]
        if any(not i for i in items):
            return "có mục rỗng giữa hai dấu phẩy"
        if s["item"] and any(not re.fullmatch(s["item"], i) for i in items):
            return "có mục sai định dạng"
    return ""


def _store_value(st, key: str) -> str:
    if key == "capacity":
        return str(st.capacity())
    return str(int(st.get_setting(key) or 0))


def snapshot(store=None) -> dict:
    """Catalog + current values (non-secret only) + which secret keys are set + config warnings."""
    path = env_path()
    vals = _read_env(path)
    items = []
    for s in CATALOG:
        it = {k: v for k, v in s.items() if k != "item"}
        if s["store"]:
            # no store yet = the value a new store starts with (capacity = max_running_default(), caps 0),
            # exactly what apply() reads back once it creates the store — a first edit is not refused as
            # stale (Codex settings R1 #3)
            it["value"] = _store_value(store, s["key"]) if store is not None else s["default"]
            it["set"] = True
        else:
            it["set"] = s["key"] in vals
            it["value"] = vals.get(s["key"], "")
            it["env_override"] = s["key"] in os.environ and os.environ[s["key"]] != it["value"]
        items.append(it)
    return {"env_file": path, "env_exists": os.path.isfile(path), "items": items,
            "secrets": secrets_status(vals), "warnings": warnings(vals)}


def _sample_keys() -> list:
    sample = os.path.join(paths.toolkit_root(), ".env.sample")
    try:
        with open(sample, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return []
    keys = re.findall(r"(?m)^\s*#?\s*([A-Z][A-Z0-9_]*)=", text)
    return list(dict.fromkeys(keys))


def secrets_status(vals: dict) -> list:
    """[{key, set}] for every credential-like key of .env.sample / .env — NEVER a value."""
    keys = [k for k in dict.fromkeys(_sample_keys() + list(vals))
            if _SECRET_NAME.search(k) and not k.endswith("_HEADER")]
    return [{"key": k, "set": bool(vals.get(k))} for k in sorted(keys)]


def warnings(vals: dict) -> list:
    out = []
    etask = sorted(k for k in vals if k.startswith("ETASK_"))
    if etask and not any(k.startswith("ATASK_") for k in vals):
        out.append(f"Có {len(etask)} khoá ETASK_* nhưng code chỉ đọc ATASK_* — aTask chưa chạy được: đổi tiền tố ETASK_ → ATASK_.")
    if vals.get("REVIEW_AUTO_POST") == "auto" and not vals.get("REVIEW_AUTO_POST_PROJECTS"):
        out.append("REVIEW_AUTO_POST=auto nhưng chưa có project nào được tự đăng (REVIEW_AUTO_POST_PROJECTS trống).")
    # what a TChat group turn may really do without asking: the two settings are UNIONED by
    # telegram_approve (the group one only widens) — Codex settings R3 #1
    def cats(key, default):
        raw = vals.get(key)
        raw = default if not raw else raw
        return {c.strip().lower() for c in raw.split(",") if c.strip()}
    group = cats("FCHAT_WATCH_AUTO_APPROVE", "read") | cats("TELEGRAM_AUTO_APPROVE", "read,file")
    if group - {"read"}:
        out.append("Lượt agent từ group TChat (nhiều người gõ) đang được tự duyệt: "
                   + ", ".join(sorted(group)) + " — hợp của 2 cài đặt tự duyệt; muốn chỉ đọc thì để cả hai là read.")
    for k in ("SSL_VERIFY", "FCHAT_VERIFY_SSL", "ATASK_VERIFY_SSL", "TELEGRAM_VERIFY_SSL"):
        if (vals.get(k) or "").lower() == "false":
            out.append(f"{k}=false: kết nối không kiểm chứng chỉ TLS.")
    return out


def apply(changes: dict, by: str, expected: dict = None, store_factory=None) -> dict:
    """Validate ALL changes, refuse if any current value differs from `expected` (stale page), then write
    env keys in one atomic edit and store keys through the store. Returns {changed, restart, audit}."""
    if not isinstance(changes, dict) or not changes:
        raise ValueError("không có thay đổi nào")
    errs = {k: validate(k, v) for k, v in changes.items()}
    errs = {k: e for k, e in errs.items() if e}
    if errs:
        raise ValueError("; ".join(f"{k}: {e}" for k, e in errs.items()))
    env_changes = {k: v for k, v in changes.items() if not BY_KEY[k]["store"]}
    store_changes = {k: v for k, v in changes.items() if BY_KEY[k]["store"]}
    path = env_path()
    if env_changes and not os.path.isfile(path):
        raise FileNotFoundError(f"không có file .env ở {path}")
    expected = expected or {}
    stale_msg = "đã bị đổi ở nơi khác kể từ lúc bạn mở trang: {} — tải lại rồi sửa"
    old, st = {}, None
    # check + write under ONE lock: two concurrent saves based on the same page can not both pass
    # (Codex settings R1 #1). The .env part is checked inside set_values, under the file's own lock.
    with locks.file_lock(paths.state_path("locks", "settings-apply.lock"), timeout=10):
        try:
            if store_changes:
                st = (store_factory or store_mod.Store)()
                stale = [k for k in store_changes if k in expected and _store_value(st, k) != expected[k]]
                if stale:
                    raise RuntimeError(stale_msg.format(", ".join(stale)))
            if env_changes:
                try:
                    old = env.set_values(path, env_changes,
                                         expected={k: expected[k] for k in env_changes if k in expected})
                except env.StaleValue as e:
                    raise RuntimeError(stale_msg.format(", ".join(e.keys))) from None
            for k, v in store_changes.items():
                old[k] = _store_value(st, k)
                if k == "capacity":
                    st.set_capacity(int(v), by=by)
                else:
                    st.set_setting(k, str(int(v)), by=by)
        finally:
            if st is not None:
                st.close()
    changed = {k: {"old": old.get(k), "new": v} for k, v in changes.items() if (old.get(k) or "") != v}
    restart = sorted({r for k in changed for r in BY_KEY[k]["restart"]})
    rec = {"at": time.time(), "by": by, "changed": changed, "restart": restart}
    try:
        with open(os.path.join(paths.state_dir(), "settings_audit.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass                    # the change is done; a missing audit line is not worth failing it
    return {"changed": changed, "restart": restart}
