#!/usr/bin/env python3
"""Listen-only TChat bridge that distils your action items — NEVER replies.

What it does
------------
- Keeps a realtime WS connection (same transport as listen.py) and buffers every
  READABLE incoming TEXT message (DMs + all groups), skipping your own messages
  and non-TEXT. E2E-encrypted bodies are DECRYPTED via crypto.py khi có private key
  (work/secrets/fchat_private.pem); vắng key thì đếm-và-bỏ như cũ.
- Every --interval minutes, asks `claude -p` to pull the action items addressed to
  YOU out of the batch, then:
    * appends a dated section to  temp/fchat_tasks/digest.md  (a running to-do list)
    * pushes a short summary of the NEW items to Telegram (remote-control bot)
- It does NOT send anything back into any conversation. Pure read + notify.

Note: TChat messages are E2E-encrypted (beatchat RSA-OAEP). Với private key có
mặt, task_digest tự giải mã cả hội thoại secure; không có key thì chỉ phần plaintext
đọc được (tin mã hoá bị đếm-và-bỏ). Xem crypto.py trong SKILL.md.

Usage
-----
  python task_digest.py                      # run bridge, flush every 10 min
  python task_digest.py --interval 5         # flush every 5 minutes
  python task_digest.py --no-telegram        # write digest.md only, no push
  python task_digest.py --atask --atask-list 12345
                                             # per flush, send ONE Telegram approve
                                             # card PER extracted item; each ✅ approve
                                             # creates just that task, ❌/timeout skips
                                             # only that one (list from --atask-list or
                                             # env FCHAT_DIGEST_ATASK_LIST). Wait window
                                             # = FCHAT_DIGEST_APPROVAL_TIMEOUT (def 300s).
                                             # Never auto-creates: no bridge / no
                                             # approval / deny / timeout → skipped.
  python task_digest.py --test "anh review giúp MR 412 trước trưa nay nhé"
                                             # one-shot: run extraction on this text,
                                             # print result, no WS / no side effects
  python task_digest.py --as-name "Chung Tran Van" --test "@Chung Tran Van anh build à"
                                             # simulate the account owner's name so
                                             # @-mentions / direct questions register

Messages from groups named "New Group" are ignored (never buffered).
Stop with Ctrl+C (a final flush runs on exit).
"""

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time

import client
import config
import listen   # reuse _looks_encrypted (E2E detection)
import tokens
import ws_client
try:
    import crypto   # E2E decrypt (cần work/secrets/fchat_private.pem); vắng key → no-op
except Exception:   # noqa: BLE001
    crypto = None

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(TOOLS_DIR, "..", "..", "..", ".."))
TASKS_DIR = os.path.join(REPO_ROOT, "temp", "fchat_tasks")
INBOX_LOG = os.path.join(TASKS_DIR, "inbox.jsonl")   # durable raw log of readable msgs
DIGEST_MD = os.path.join(TASKS_DIR, "digest.md")     # running to-do list (markdown)

# aTask CLI (separate skill; its own config.py/client.py) — we shell out to it
# instead of importing to avoid the config/client module-name clash between skills.
ATASK_TOOLS_DIR = os.path.abspath(
    os.path.join(TOOLS_DIR, "..", "..", "atask-automation", "tools"))
ATASK_TASKS_PY = os.path.join(ATASK_TOOLS_DIR, "tasks.py")

# Groups whose messages are never buffered (case-insensitive, trimmed).
IGNORED_GROUPS = {"new group"}

# digest priority (Vietnamese) → aTask priority enum.
_ATASK_PRIORITY = {"cao": "HIGH", "vừa": "MEDIUM", "thấp": "LOW"}

# Telegram push + Claude account resolution via the remote-control skill
# (best-effort import; both optional).
_RC_DIR = os.path.abspath(os.path.join(TOOLS_DIR, "..", "..", "remote-control", "tools"))
sys.path.insert(0, _RC_DIR)
try:
    import tg_api  # noqa: E402
except Exception:
    tg_api = None
try:
    import approvals  # noqa: E402  (file-backed approve/deny store; needs bridge daemon)
except Exception:
    approvals = None
try:
    import rc_config  # noqa: E402  (approval chat/timeout config from remote-control)
except Exception:
    rc_config = None
try:
    import telegram_bridge as tb  # noqa: E402  (account home + claude-bin helpers)
except Exception:
    tb = None

_DEV_DIR = os.path.abspath(os.path.join(TOOLS_DIR, "..", "..", "dev-automation", "tools"))
sys.path.insert(0, _DEV_DIR)
try:
    import daemon_common  # noqa: E402  (shared health log)
except Exception:
    daemon_common = None


def _hlog(event, detail=""):
    if daemon_common is not None:
        daemon_common.health_log("task_digest", event, detail)


def _now():
    return time.strftime("%H:%M:%S")


def _hm(ts):
    return time.strftime("%H:%M", time.localtime(ts))


# ----------------------------- buffering ------------------------------------

class Buffer:
    """Thread-safe pending-message buffer; the flusher drains it each interval."""

    def __init__(self):
        self._lock = threading.Lock()
        self._items = []
        self.encrypted_skipped = 0

    def add(self, item):
        with self._lock:
            self._items.append(item)

    def bump_encrypted(self):
        with self._lock:
            self.encrypted_skipped += 1

    def drain(self):
        with self._lock:
            items, self._items = self._items, []
            enc, self.encrypted_skipped = self.encrypted_skipped, 0
            return items, enc

    def requeue(self, items):
        """Put a failed batch back at the front so a transient error doesn't lose it."""
        with self._lock:
            self._items = items + self._items


def _persist(item):
    """Append the readable message to a durable JSONL log (survives restarts)."""
    os.makedirs(TASKS_DIR, exist_ok=True)
    with open(INBOX_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(item, ensure_ascii=False) + "\n")


# ----------------------------- extraction -----------------------------------

def _build_prompt(items, owner_name=""):
    """Turn a batch of messages into an extraction prompt for `claude -p`.

    owner_name (chủ tài khoản) is injected so the model can recognise messages
    that @-mention or address the account owner — otherwise a mention like
    "@Chung Tran Van" is just an anonymous name and gets filtered as chit-chat."""
    lines = []
    for it in items:
        where = "DM" if it["is_direct"] else f"Nhóm {it['gname']}"
        lines.append(f"[{_hm(it['ts'])}] ({where}) {it['sender_name']}: {it['content']}")
    transcript = "\n".join(lines)
    who = (f'Chủ tài khoản (NGƯỜI NHẬN) tên là: "{owner_name}". '
           "Mọi tin @nhắc tên này, hỏi trực tiếp, hay nhờ vả người này đều là "
           "tin GỬI TỚI CHỦ TÀI KHOẢN.\n" if owner_name else "")
    return (
        "Bạn là trợ lý lọc CÔNG VIỆC từ tin nhắn chat công ty (TChat).\n"
        + who +
        "Dưới đây là các tin nhắn mới trong DM và nhóm.\n"
        "Hãy rút ra những việc mà chủ tài khoản CẦN LÀM / được giao / được nhờ / "
        "cần phản hồi:\n"
        "- yêu cầu, deadline, nhắc việc;\n"
        "- CÂU HỎI hỏi trực tiếp chủ tài khoản (kể cả ngắn/mơ hồ như \"anh build à\", "
        "\"xong chưa\") → việc là TRẢ LỜI / LÀM RÕ cho người hỏi;\n"
        "- tin @nhắc tên chủ tài khoản kèm yêu cầu/câu hỏi.\n"
        "GỘP các tin liên tiếp của cùng một người thành MỘT ý trước khi xét. "
        "BỎ QUA chuyện phiếm thuần, thông báo không cần hành động, và tin người khác "
        "nói với nhau KHÔNG liên quan tới chủ tài khoản.\n\n"
        "Trả về DUY NHẤT một mảng JSON (không kèm giải thích, không markdown), mỗi phần tử:\n"
        '  {"task": "<việc cần làm, ngắn gọn>", '
        '"source": "<ai nhờ + ở đâu>", '
        '"due": "<deadline nếu có, không thì \\"\\">", '
        '"priority": "<cao|vừa|thấp>"}\n'
        "Nếu KHÔNG có việc nào, trả về [].\n\n"
        "--- TIN NHẮN ---\n"
        f"{transcript}\n"
        "--- HẾT ---\n"
    )


def _parse_tasks(raw):
    """Pull the first JSON array out of claude's output; tolerant of stray text."""
    if not raw:
        return []
    m = re.search(r"\[.*\]", raw, re.DOTALL)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except Exception:
        return []
    out = []
    if isinstance(data, list):
        for d in data:
            if isinstance(d, dict) and d.get("task"):
                out.append({
                    "task": str(d.get("task")).strip(),
                    "source": str(d.get("source", "")).strip(),
                    "due": str(d.get("due", "")).strip(),
                    "priority": str(d.get("priority", "")).strip().lower(),
                })
    return out


def _claude_bin():
    if tb is not None:
        try:
            return tb._claude_bin()
        except Exception:
            pass
    return config.get("CLAUDE_BIN") or "claude"


def _claude_accounts():
    """Ordered [(label, home)] from CLAUDE_ACCOUNTS (primary first, fallbacks
    after). An account is a home dir whose <home>/.claude holds its own creds —
    `claude` is pointed at it via HOME/USERPROFILE. Default 'work' first."""
    if tb is not None:
        try:
            return tb._accounts()
        except Exception:
            pass
    return [("default", None)]


def _account_unavailable(text):
    """True if the error is the ACCOUNT's fault (401/quota/login/5xx) → another
    account is worth trying. Reuses telegram_bridge's classifier when available."""
    if tb is not None:
        try:
            return tb._is_account_unavailable(text)
        except Exception:
            pass
    t = (text or "").lower()
    return any(n in t for n in ("401", "403", "unauthorized", "authentication",
                                "usage limit", "rate limit", "quota", "/login",
                                "overloaded", "500", "502", "503", "529"))


def _run_claude_once(prompt, model, home):
    """One `claude -p` call under a given account home. Returns (tasks, err, unavailable)."""
    argv = [_claude_bin(), "-p", "--model", model]
    env = dict(os.environ)
    if home:  # point claude at this account's creds: <home>/.claude
        env["HOME"] = home          # macOS / Linux
        env["USERPROFILE"] = home   # Windows
    try:
        r = subprocess.run(argv, input=prompt, cwd=REPO_ROOT, env=env,
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=240)
    except FileNotFoundError:
        return None, "không tìm thấy lệnh 'claude' trên PATH (đặt CLAUDE_BIN trong .env)", False
    except subprocess.TimeoutExpired:
        return None, "claude -p quá 240s (timeout) — lô quá lớn hoặc CLI treo", False
    except Exception as e:  # noqa: BLE001
        return None, str(e), False
    out = (r.stdout or "").strip()
    err = (r.stderr or "").strip()
    if r.returncode != 0:
        # Salvage: claude sometimes prints valid JSON then still exits non-zero.
        salvaged = _parse_tasks(out)
        if salvaged:
            return salvaged, None, False
        # claude prints most errors (quota, auth, bad model) to stdout, not stderr.
        detail = (err or out or "không có output").replace("\n", " ").strip()[:500]
        return None, f"claude exit {r.returncode}: {detail}", _account_unavailable(detail)
    return _parse_tasks(out), None, False


def _extract(items, owner_name=""):
    """Run `claude -p` over a batch under the work account; fall back to other
    accounts on auth/quota errors. Returns (tasks, error)."""
    prompt = _build_prompt(items, owner_name)
    model = config.get("FCHAT_DIGEST_MODEL", "sonnet") or "sonnet"
    last_err = None
    for label, home in _claude_accounts():
        tasks, err, unavailable = _run_claude_once(prompt, model, home)
        if err is None:
            return tasks, None
        last_err = f"[{label}] {err}"
        if not unavailable:
            break   # request-level error — switching account won't help
        print(f"[{_now()}] account '{label}' không dùng được → thử account kế tiếp…",
              file=sys.stderr)
    return [], last_err


# ----------------------------- outputs --------------------------------------

_PRIO_ICON = {"cao": "🔴", "vừa": "🟡", "thấp": "🟢"}


def _write_digest(tasks, enc_skipped):
    os.makedirs(TASKS_DIR, exist_ok=True)
    header_new = not os.path.isfile(DIGEST_MD)
    stamp = time.strftime("%Y-%m-%d %H:%M")
    with open(DIGEST_MD, "a", encoding="utf-8") as f:
        if header_new:
            f.write("# TChat — việc cần làm (tự tổng hợp)\n\n"
                    "> Tự sinh bởi task_digest.py. Chỉ gồm tin ĐỌC ĐƯỢC "
                    "(tin mã hoá E2E không tính).\n")
        note = f"  · {enc_skipped} tin mã hoá bỏ qua" if enc_skipped else ""
        f.write(f"\n## {stamp} — {len(tasks)} việc mới{note}\n")
        for t in tasks:
            icon = _PRIO_ICON.get(t["priority"], "•")
            meta = " · ".join(x for x in (t["source"], f"⏰ {t['due']}" if t["due"] else "") if x)
            f.write(f"- [ ] {icon} {t['task']}" + (f"  _({meta})_" if meta else "") + "\n")


def _telegram_summary(tasks):
    if not tg_api:
        print(f"[{_now()}] [telegram] module remote-control/tg_api không nạp được — bỏ push.",
              file=sys.stderr)
        return
    nbot = tg_api.notify_bot()
    chats = tg_api.allowed_chats(nbot)
    if not chats:
        print(f"[{_now()}] [telegram] TELEGRAM_ALLOWED_CHATS trống — bỏ push.", file=sys.stderr)
        return
    lines = [f"📋 <b>{len(tasks)} việc mới từ TChat</b>"]
    for t in tasks:
        icon = _PRIO_ICON.get(t["priority"], "•")
        extra = []
        if t["source"]:
            extra.append(t["source"])
        if t["due"]:
            extra.append(f"⏰ {t['due']}")
        suffix = f"  <i>({' · '.join(extra)})</i>" if extra else ""
        lines.append(f"{icon} {t['task']}{suffix}")
    text = "\n".join(lines)
    for c in chats:
        res = tg_api.send_message(c, text, bot=nbot)
        if not res.get("ok"):
            print(f"[{_now()}] [telegram] gửi {c} lỗi: {res.get('description')}", file=sys.stderr)


def _approval_chat(bot):
    """Resolve which Telegram chat to send the approval card to (mirror the
    telegram_approve hook): explicit approval chat → approval bot's allowlist →
    default allowlist. Returns "" if none configured."""
    if rc_config is not None:
        chat = rc_config.get("TELEGRAM_APPROVAL_CHAT")
        if chat:
            return chat
    chat = (tg_api.allowed_chats(bot)[:1] or [""])[0]
    return chat


def _approval_timeout() -> int:
    """Digest có timeout RIÊNG (FCHAT_DIGEST_APPROVAL_TIMEOUT) để chờ lâu (vd 24h =
    86400) mà KHÔNG ảnh hưởng hook duyệt tương tác (dùng TELEGRAM_APPROVAL_TIMEOUT,
    mặc định 300s). Thứ tự: digest-riêng → chung → 300."""
    if rc_config is None:
        return 300
    raw = (rc_config.get("FCHAT_DIGEST_APPROVAL_TIMEOUT")
           or rc_config.get("TELEGRAM_APPROVAL_TIMEOUT", "300") or "300")
    try:
        return int(raw)
    except ValueError:
        return 300


def _send_task_card(t, list_id, bot, chat):
    """Gửi MỘT thẻ duyệt cho MỘT việc. Trả req_id nếu gửi được, None nếu lỗi."""
    line = (f"{_PRIO_ICON.get(t['priority'], '•')} {t['task']}"
            + (f"  ⏰ {t['due']}" if t["due"] else ""))
    detail = line + (f"\nNguồn: {t['source']}" if t["source"] else "")
    req_id = approvals.create("atask_create", f"Tạo 1 task trên aTask (list {list_id})",
                              detail, risk="write")
    text = (
        f"📥 <b>TChat digest xin duyệt tạo 1 task trên aTask</b>\n"
        f"<b>List đích:</b> <code>{list_id}</code>\n\n"
        f"• {line}" + (f"\n<i>{t['source']}</i>" if t["source"] else "")
    )
    resp = tg_api.send_message(chat, text,
                               reply_markup=tg_api.approve_keyboard(req_id), bot=bot)
    if not resp.get("ok"):
        print(f"[{_now()}] [atask] gửi thẻ duyệt lỗi: {resp.get('description')} — "
              f"bỏ việc '{t['task'][:40]}…'.", file=sys.stderr)
        return None
    return req_id


# ── Approval worker (MỘT thread nền duy nhất cho MỌI thẻ chờ duyệt) ──────────
# flush() chỉ gửi thẻ + đăng ký vào _PENDING (nhanh, không block). Một worker duy
# nhất poll tất cả thẻ đang chờ: approved → tạo task ngay; denied → bỏ; quá deadline
# → auto-huỷ. Số thread cố định = 1 bất kể tải (thay vì mỗi lô một thread).
_PENDING = {}                     # req_id -> {"task", "list_id", "deadline"}
_PENDING_LOCK = threading.Lock()
_WORKER_STARTED = False
_WORKER_LOCK = threading.Lock()


def _ensure_approval_worker():
    global _WORKER_STARTED
    with _WORKER_LOCK:
        if _WORKER_STARTED:
            return
        _WORKER_STARTED = True
    threading.Thread(target=_approval_worker, daemon=True,
                     name="atask-approval-worker").start()


def _approval_worker():
    """Poll mọi thẻ đang chờ mỗi 2s. Chạy suốt đời tiến trình (daemon)."""
    while True:
        time.sleep(2)
        with _PENDING_LOCK:
            snapshot = list(_PENDING.items())
        now = time.time()
        for req_id, rec in snapshot:
            status = (approvals.get(req_id) or {}).get("status")
            done = None
            if status == "approved":
                _create_atask_tasks([rec["task"]], rec["list_id"])
                done = "approved"
            elif status == "denied":
                print(f"[{_now()}] [atask] bị từ chối → bỏ '{rec['task']['task'][:40]}…'.")
                done = "denied"
            elif now >= rec["deadline"]:
                approvals.decide(req_id, False, by="timeout")
                print(f"[{_now()}] [atask] thẻ hết giờ → tự huỷ '{rec['task']['task'][:40]}…'.")
                done = "timeout"
            if done:
                with _PENDING_LOCK:
                    _PENDING.pop(req_id, None)


def _approve_and_create_each(tasks, list_id):
    """MỖI việc = MỘT thẻ duyệt riêng (nút ✅/❌). Chỉ GỬI thẻ + ĐĂNG KÝ vào _PENDING
    rồi trả về ngay (không block); worker nền lo poll + tạo task khi được Duyệt.
    Fail-safe: thiếu tg_api/approvals/chat → không tạo gì."""
    if tg_api is None or approvals is None:
        print(f"[{_now()}] [atask] thiếu module duyệt (tg_api/approvals) — "
              f"BỎ tạo task (không tự tạo khi chưa duyệt).", file=sys.stderr)
        return
    bot = tg_api.approval_bot()
    chat = _approval_chat(bot)
    if not chat:
        print(f"[{_now()}] [atask] chưa cấu hình chat duyệt "
              f"(TELEGRAM_APPROVAL_CHAT / TELEGRAM_ALLOWED_CHATS) — BỎ tạo task.",
              file=sys.stderr)
        return

    timeout = _approval_timeout()
    deadline = time.time() + timeout
    n = 0
    for t in tasks:
        req_id = _send_task_card(t, list_id, bot, chat)
        if req_id:
            with _PENDING_LOCK:
                _PENDING[req_id] = {"task": t, "list_id": list_id, "deadline": deadline}
            n += 1
    if not n:
        return
    _ensure_approval_worker()
    with _PENDING_LOCK:
        total_pending = len(_PENDING)
    print(f"[{_now()}] [atask] đã gửi {n} thẻ (mỗi việc 1 thẻ), chờ duyệt tối đa "
          f"{timeout}s ≈ {timeout // 3600}h{(timeout % 3600) // 60:02d}m — "
          f"worker nền đang theo dõi {total_pending} thẻ.")


def _create_atask_tasks(tasks, list_id):
    """Create one aTask task per extracted item by shelling out to the
    atask-automation CLI (its own config/creds). Returns (created, failed)."""
    if not os.path.isfile(ATASK_TASKS_PY):
        print(f"[{_now()}] [atask] không thấy {ATASK_TASKS_PY} — bỏ tạo task.",
              file=sys.stderr)
        return 0, len(tasks)
    created = failed = 0
    for t in tasks:
        # Fold source + deadline into the description (aTask due_date wants ISO;
        # the extracted "due" is free text, so keep it human-readable in the body).
        desc_parts = []
        if t["source"]:
            desc_parts.append(f"Nguồn: {t['source']}")
        if t["due"]:
            desc_parts.append(f"Deadline (tự chat): {t['due']}")
        desc_parts.append("— tự tạo từ TChat digest (task_digest.py)")
        argv = [sys.executable, ATASK_TASKS_PY, "create",
                "--name", t["task"], "--list", str(list_id),
                "--desc", "\n".join(desc_parts)]
        prio = _ATASK_PRIORITY.get(t["priority"])
        if prio:
            argv += ["--priority", prio]
        try:
            r = subprocess.run(argv, cwd=ATASK_TOOLS_DIR, capture_output=True,
                               text=True, encoding="utf-8", errors="replace",
                               timeout=60)
        except Exception as e:  # noqa: BLE001
            print(f"[{_now()}] [atask] '{t['task'][:40]}…' lỗi: {e}", file=sys.stderr)
            failed += 1
            continue
        out = (r.stdout or "").strip()
        # tasks.py prints JSON; an error surfaces as {"error": true, ...}.
        if r.returncode != 0 or '"error": true' in out or '"error":true' in out:
            detail = (r.stderr or out or "no output").replace("\n", " ")[:300]
            print(f"[{_now()}] [atask] '{t['task'][:40]}…' lỗi: {detail}",
                  file=sys.stderr)
            failed += 1
        else:
            created += 1
    if created or failed:
        print(f"[{_now()}] [atask] tạo {created} task"
              + (f", {failed} lỗi" if failed else "") + f" → list {list_id}")
    return created, failed


def flush(buf, push_telegram, atask_list=None, owner_name=""):
    items, enc = buf.drain()
    if not items and not enc:
        return
    print(f"[{_now()}] flush: {len(items)} tin đọc được"
          + (f", {enc} tin mã hoá bỏ qua" if enc else "") + " → trích việc…")
    if not items:
        return
    tasks, err = _extract(items, owner_name)
    if err:
        buf.requeue(items)   # keep the batch for next cycle (transient quota/auth errors)
        print(f"[{_now()}] [extract] lỗi: {err}", file=sys.stderr)
        print(f"[{_now()}] (giữ lại {len(items)} tin cho lần flush sau)", file=sys.stderr)
        return
    if not tasks:
        print(f"[{_now()}] không có việc cần làm trong lô này.")
        return
    _write_digest(tasks, enc)
    print(f"[{_now()}] +{len(tasks)} việc → {DIGEST_MD}")
    if push_telegram:
        _telegram_summary(tasks)
    if atask_list:
        # Chỉ gửi thẻ + đăng ký vào _PENDING rồi trả về ngay (không block flusher).
        # MỘT worker nền duy nhất (_approval_worker) poll mọi thẻ + tạo task khi duyệt.
        _approve_and_create_each(tasks, atask_list)


# ----------------------------- WS loop --------------------------------------

def _handle(obj, me, buf, seen):
    data = obj.get("data") or {}
    if data.get("type") != "TEXT":
        return
    sender = data.get("senderId")
    if not sender or sender == me:
        return  # ignore own messages
    gid = data.get("groupId")
    inc = data.get("messageIdInc")
    if (gid, inc) in seen:
        return
    seen.add((gid, inc))

    group = data.get("group") or {}
    gname = group.get("name") or ""
    if gname.strip().lower() in IGNORED_GROUPS:
        return  # skip noise groups (e.g. unnamed "New Group")

    content = data.get("content")
    if crypto and listen._looks_encrypted(content):
        content = crypto.decrypt_if_needed(content)   # E2E → plaintext nếu có key
    if not content or listen._looks_encrypted(content):
        buf.bump_encrypted()                          # rỗng / vẫn ciphertext (không key) → bỏ
        return
    item = {
        "ts": int(time.time()),
        "gid": gid,
        "gname": group.get("name") or gid,
        "is_direct": bool(group.get("isDirectChat")),
        "inc": inc,
        "sender": sender,
        "sender_name": (data.get("user") or {}).get("displayName") or sender,
        "content": content,
    }
    _persist(item)
    buf.add(item)
    where = "DM" if item["is_direct"] else f"Nhóm {item['gname']}"
    print(f"[{_now()}] [{where}] {item['sender_name']}: {content[:80]}")


def run(interval_min, push_telegram, atask_list=None):
    meobj = client.api_get("/user/me") or {}
    me = meobj.get("id")
    if not me:
        _hlog("fatal", "cannot resolve current user (token bad?)")
        print("[ERROR] không lấy được user hiện tại (token sai?)", file=sys.stderr)
        sys.exit(1)
    _hlog("started", f"interval={interval_min}m")
    owner_name = (meobj.get("displayName") or meobj.get("fullName")
                  or meobj.get("name") or "")
    buf = Buffer()
    seen = set()
    stop = threading.Event()

    def _flusher():
        while not stop.wait(interval_min * 60):
            try:
                flush(buf, push_telegram, atask_list, owner_name)
            except Exception as e:  # noqa: BLE001 - keep the bridge alive
                print(f"[{_now()}] [flusher] lỗi: {e}", file=sys.stderr)

    threading.Thread(target=_flusher, daemon=True).start()
    tg = "+telegram" if push_telegram else "markdown-only"
    et = f" | aTask list {atask_list}" if atask_list else ""
    print(f"[{_now()}] nghe như {me} | flush mỗi {interval_min}' | {tg}{et} | Ctrl+C để dừng")
    print(f"[{_now()}] digest: {DIGEST_MD}")
    backoff = 2
    try:
        while True:
            try:
                ws = ws_client.WebSocket(config.ws_url(), subprotocols=[tokens.ensure_fresh()],
                                         origin=config.web_origin(), timeout=20,
                                         verify=config.verify_ssl())
                print(f"[{_now()}] connected.")
                _hlog("recovered" if backoff > 2 else "connected")
                backoff = 2
                last_ping = time.time()
                while True:
                    try:
                        msg = ws.recv()
                    except (socket.timeout, TimeoutError):
                        ws.send_text("ping")
                        last_ping = time.time()
                        continue
                    if msg is None:
                        print(f"[{_now()}] socket closed; reconnecting…")
                        break
                    if msg in ("pong", "ping"):
                        continue
                    try:
                        o = json.loads(msg) if isinstance(msg, str) else None
                    except Exception:
                        o = None
                    if isinstance(o, dict) and o.get("type") == "message":
                        _handle(o, me, buf, seen)
                    if time.time() - last_ping > 20:
                        ws.send_text("ping")
                        last_ping = time.time()
                ws.close()
            except KeyboardInterrupt:
                raise
            except Exception as e:  # noqa: BLE001
                _hlog("transient", f"reconnect in {backoff}s — {e}")
                print(f"[{_now()}] connection error: {e}; retry in {backoff}s", file=sys.stderr)
                tokens.refresh(verbose=False)
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
    except KeyboardInterrupt:
        print(f"\n[{_now()}] dừng — flush lần cuối…")
        stop.set()
        flush(buf, push_telegram, atask_list, owner_name)


# ----------------------------- entry ----------------------------------------

if __name__ == "__main__":
    p = argparse.ArgumentParser(prog="task_digest.py")
    p.add_argument("--interval", type=int, default=10, help="phút giữa các lần trích (mặc định 10)")
    p.add_argument("--no-telegram", action="store_true", help="chỉ ghi digest.md, không push Telegram")
    p.add_argument("--atask", action="store_true",
                   help="ngoài digest.md, HỎI DUYỆT qua Telegram rồi mới tạo task trên "
                        "aTask cho mỗi việc (cần --atask-list hoặc FCHAT_DIGEST_ATASK_LIST; "
                        "cần bridge Telegram đang chạy để bấm nút duyệt)")
    p.add_argument("--atask-list", metavar="LIST_ID", default=None,
                   help="list_task_id đích trên aTask (mặc định: env FCHAT_DIGEST_ATASK_LIST)")
    p.add_argument("--test", metavar="TEXT", help="chạy thử trích việc trên 1 câu, in ra, không WS/không side-effect")
    p.add_argument("--as-name", metavar="NAME", default="",
                   help="(chỉ cho --test) giả lập tên chủ tài khoản để thử nhận diện @nhắc/câu hỏi")
    a = p.parse_args()

    missing = config.validate()
    if missing:
        print(f"[ERROR] thiếu config: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)

    if a.test is not None:
        sample = [{"ts": int(time.time()), "gid": "test", "gname": "Test",
                   "is_direct": True, "inc": 0, "sender": "x",
                   "sender_name": "Người test", "content": a.test}]
        tasks, err = _extract(sample, a.as_name)
        if err:
            print(f"[extract] lỗi: {err}", file=sys.stderr)
            sys.exit(1)
        client.print_json(tasks)
        sys.exit(0)

    atask_list = None
    if a.atask:
        atask_list = a.atask_list or config.get("FCHAT_DIGEST_ATASK_LIST")
        if not atask_list:
            print("[ERROR] --atask cần list đích: truyền --atask-list <ID> "
                  "hoặc đặt FCHAT_DIGEST_ATASK_LIST trong .env", file=sys.stderr)
            sys.exit(1)

    try:
        run(a.interval, push_telegram=not a.no_telegram, atask_list=atask_list)
    except KeyboardInterrupt:
        print("\nstopped.")
