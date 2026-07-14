#!/usr/bin/env python3
"""Resolve MY aTask tasks: verify-against-code, then close / hand off (human-gated).

Khác với `atask_watch.py` (chỉ phân loại EXECUTE/ASSIGN), tool này chạy MỘT luồng
xử-lý-task đầy đủ và CHỈ lấy task ở 2 nhóm trạng thái `todo` (Chưa làm) + `processing`
(Đang làm) — bỏ qua mọi task đã duyệt/hoàn thành/đóng.

Vòng đời mỗi task MỚI / ĐỔI TRẠNG THÁI:
  1. Spawn `claude -p` (CHỈ ĐỌC) → đọc nội dung task, map task → project trong
     `work/projects.json`, rồi MỞ code trong `clone_dir` để xác minh task ĐÃ fix hay CHƯA.
     Trả verdict máy-đọc-được: fixed | not_fixed | unclear (+ project, estimate, người gợi ý).
  2. Quyết định (mọi thao tác GHI đều xin DUYỆT qua Telegram trước — nút bấm):
       • FIXED      → hỏi: ✅ Hoàn thành (complete_task) | 🕓 Chờ phê duyệt (update_task status).
                      Tín hiệu đã-validate của task: percent==1.0 hoặc statusType ∈ {approved, completed}.
       • NOT_FIXED  → hỏi: 👤 Tôi tự làm | ➡️ Giao người khác.
                        - Tôi tự làm  → bổ sung comment + chỉnh estimate, để assignee = tôi (theo dõi).
                        - Giao người  → hỏi tiếp: ✅ Giao <tên> | ❌ Bỏ qua → assign_task_users +
                          comment thông tin bổ sung + chỉnh estimate hợp lý.
       • UNCLEAR    → báo Telegram, KHÔNG ghi.

Chống lấy trùng / gán nhiều người 1 task:
  • FLOW LOCK (chung với `auto-dev/tools/task_queue.py`, owner `task_resolver`): luồng này
    xử lý TUẦN TỰ 1 task/lúc — tránh xung đột code khi nhiều task đụng cùng repo; lock bận
    thì task để lại cho vòng poll sau. Luồng khác (người làm tay) không bị chặn.
  • STATE (`temp/atask_resolved.json`) khoá theo (task_id → statusType): đã xử lý ở đúng
    trạng thái đó thì KHÔNG đụng lại; lưu luôn `assigned_to` để vòng sau không gán lại người khác.
  • `_inflight`: task đang chờ duyệt sẽ KHÔNG bị vòng poll kế tiếp bốc lại.
  • Trước khi gán: get_task kiểm tra đã có assignee (khác tôi) chưa → có thì báo & bỏ qua gán.

Lần chạy đầu lập BASELINE (đánh dấu task hiện có, không xử lý cả backlog) — về sau chỉ xử lý
task mới/đổi trạng thái. Dùng --resolve-existing để làm cả backlog (giới hạn --max-per-cycle).

Cấu hình (.env):
  ATASK_RESOLVE_INTERVAL (mặc định 600s) · ATASK_RESOLVE_TIMEOUT (phân tích, mặc định 900s)
  ATASK_RESOLVE_STATUS_TYPES (mặc định "todo,processing")
  ATASK_RESOLVE_STATUS_REVIEW (status code "chờ phê duyệt" — [Unverified], confirm theo workflow của bạn)
  ATASK_RESOLVE_STATUS_INPROGRESS (mặc định IN_PROGRESS)
  ATASK_MY_LOGIN (login của tôi để loại khỏi gợi ý assignee; đặt trong .env)
  (dùng lại) TELEGRAM_ALLOWED_CHATS, TELEGRAM_APPROVAL_TIMEOUT, CLAUDE_ACCOUNTS, TELEGRAM_AGENT_MODEL

Chạy:
  python task_resolver.py                  # poll 10', baseline trước
  python task_resolver.py --interval 300   # poll 5'
  python task_resolver.py --once           # một vòng rồi thoát
  python task_resolver.py --no-act         # chỉ in task lấy được, không phân tích/ghi
  python task_resolver.py --task <id>      # xử lý ĐÚNG 1 task id rồi thoát
"""

import argparse
import datetime
import html
import json
import os
import re
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import client          # noqa: E402  (atask)
import config          # noqa: E402  (atask)
import search          # noqa: E402  (atask)
import tasks as task_api  # noqa: E402  (atask)

# remote-control tools: Telegram + approval IPC + account/settings helpers
_RC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__),
                                       "..", "..", "remote-control", "tools"))
sys.path.insert(0, _RC_DIR)
import approvals             # noqa: E402
import rc_config as rccfg    # noqa: E402
import telegram_bridge as tb  # noqa: E402
import tg_api                # noqa: E402

# auto-dev tools: khoá TUẦN TỰ của luồng resolver, dùng CHUNG với task_queue.py —
# resolver và `task_queue.py next` (owner mặc định 'task_resolver') không bao giờ
# chạy 2 task cùng lúc; luồng khác (người làm tay) không bị chặn.
_AD_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__),
                                       "..", "..", "auto-dev", "tools"))
sys.path.insert(0, _AD_DIR)
import task_queue            # noqa: E402

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = rccfg.repo_root()
STATE = os.path.join(REPO_ROOT, "temp", "atask_resolved.json")

# statusType coi là ĐÃ hoàn tất → KHÔNG xử lý. Lưu ý: 'approved' (Đã duyệt) KHÔNG nằm đây —
# theo yêu cầu, task chưa 'completed' (gồm todo/processing/approved) đều phải xử lý.
TERMINAL_TYPES = {"completed", "closed", "cancelled", "rejected", "done"}
# Mặc định: lấy MỌI task chưa hoàn tất (None → không lọc server-side, loại TERMINAL ở client).
# Đè bằng ATASK_RESOLVE_STATUS_TYPES (vd "todo,processing" nếu muốn hẹp lại).
DEFAULT_STATUS_TYPES = None

_busy = threading.Semaphore(2)     # trần thread; TUẦN TỰ thật sự do flow lock của task_queue
_inflight = set()                  # task đang xử lý (chống vòng poll bốc lại)
_inflight_lock = threading.Lock()


def _now():
    return time.strftime("%H:%M:%S")


def _log(msg):
    """Ghi ra stdout kèm giờ + flush ngay (để log nền/pipe thấy real-time)."""
    print(f"[{_now()}] {msg}", flush=True)


def _iso(d: datetime.date) -> str:
    # aTask server dùng Instant.parse → BẮT BUỘC có 'Z' (UTC), thiếu sẽ lỗi parse.
    return d.strftime("%Y-%m-%dT00:00:00Z")


def _load_state() -> dict:
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_state(d: dict):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2, ensure_ascii=False)


def _status_types():
    """Nhóm statusType lọc server-side, hoặc None = lấy tất cả (loại TERMINAL ở client)."""
    raw = rccfg.get_list("ATASK_RESOLVE_STATUS_TYPES")
    return raw or DEFAULT_STATUS_TYPES


def _my_login() -> str:
    return rccfg.get("ATASK_MY_LOGIN") or ""


def _my_tasks() -> list:
    """Task đang giao cho tôi, MỌI trạng thái CHƯA hoàn tất (loại TERMINAL_TYPES).
    Mặc định lấy tất cả rồi lọc client-side; đè phạm vi bằng ATASK_RESOLVE_STATUS_TYPES."""
    st = _status_types()
    kwargs = {"size": 100}
    if st:
        kwargs["status_type"] = st
    try:
        r = search.search_my_assigned_tasks(**kwargs)
    except SystemExit:      # check_error có thể sys.exit khi API lỗi (405/500 lúc backend restart)
        return [{"error": True, "message": "aTask API lỗi (search) — backend có thể đang restart"}]
    if not isinstance(r, dict) or r.get("error"):
        return [{"error": True, "message": (r or {}).get("message", "lỗi my-tasks")}]
    data = ((r.get("content") or {}).get("data")) or []
    out = []
    for t in data:
        if (t.get("statusType") or "").lower() in TERMINAL_TYPES:
            continue
        out.append({
            "id": t.get("id"),
            "name": t.get("name") or "(không tên)",
            "statusType": (t.get("statusType") or "").lower(),
            "status": t.get("status"),
            "listTaskId": t.get("listTaskId"),
            "project": t.get("projectName") or "",
            "due": t.get("dueDate") or "",
            "percent": t.get("percent"),
            "priority": t.get("priority"),
            "description": t.get("description") or "",   # từ ES record → để NỐI note, không ghi đè
            # assignee/reviewer chỉ có trong record SEARCH (get_task KHÔNG trả) → mang theo để dedup
            "assignTaskList": t.get("assignTaskList") or [],
            "assignReviewList": t.get("assignReviewList") or [],
        })
    return out


def _user_ids(assign_list) -> list:
    """Trích userId (int) từ một assignTaskList/assignReviewList (list dict {userId,...})."""
    ids = []
    for it in assign_list or []:
        if isinstance(it, dict) and it.get("userId") is not None:
            try:
                ids.append(int(it["userId"]))
            except (TypeError, ValueError):
                pass
    return sorted(set(ids))


def _status_id_for(list_task_id, status_type):
    """Tra status-ID (mờ, theo từng list) cho một nhóm statusType. None nếu list KHÔNG có
    cột thuộc nhóm đó. Cần vì update_task(status=...) nhận status-ID theo-list, không nhận keyword.

    ⚠️ KHÔNG lọc bằng status_type ở server (ES trả TASK CHÉO LIST → mượn nhầm status-ID của list
    khác, set vào task sẽ KHÔNG đổi được). Thay vào đó: lấy task TRONG list rồi chỉ nhận status-ID
    của row THỰC SỰ thuộc list này + đúng statusType."""
    if not list_task_id:
        return None
    try:
        r = search.search_tasks(list_task_id=list_task_id, size=100)
    except SystemExit:
        return None
    if not isinstance(r, dict) or r.get("error"):
        return None
    rows = ((r.get("content") or {}).get("data")) or []
    st = status_type.lower()
    for x in rows:
        if (x.get("listTaskId") == list_task_id
                and (x.get("statusType") or "").lower() == st
                and x.get("status")):
            return x.get("status")
    return None


def _resolve_status(task, status_type, env_key):
    """status-ID cho nhóm: ưu tiên tra động trong list; fallback .env (env_key = status-ID)."""
    return _status_id_for(task.get("listTaskId"), status_type) or (rccfg.get(env_key) or None)


# ── account / agent ────────────────────────────────────────────────
def _primary_account():
    accts = tb._accounts()
    return accts[0] if accts else ("default", None)


def _claude_env(chat):
    label, home = _primary_account()
    env = dict(os.environ)
    env["CLAUDE_TG_BRIDGE"] = "1"
    env["CLAUDE_TG_CHAT_ID"] = str(chat)
    if home:
        env["HOME"] = home
        env["USERPROFILE"] = home
    return env, label, home


_ANALYSIS_SYS = (
    "Bạn là kỹ sư phân tích MỘT task aTask để quyết định nó ĐÃ được fix trong code hay CHƯA. "
    "TUYỆT ĐỐI CHỈ ĐỌC — không ghi/đổi gì (không tạo branch/MR/comment, không đổi task). "
    "Quy trình bắt buộc:\n"
    "1) Đọc chi tiết task: `python .claude/skills/atask-automation/tools/tasks.py get <id>` "
    "(xem mô tả, checklist, comment để hiểu YÊU CẦU/bug cần fix).\n"
    "2) Map task → project: đọc `work/projects.json`, đối chiếu projectName của task với "
    "key/gitlab_path → lấy `clone_dir`. KHÔNG map được rõ ràng → status=unclear.\n"
    "3) MỞ code trong clone_dir đó (grep/đọc file) để kết luận task đã fix CHƯA: tìm hàm/đoạn "
    "code liên quan, kiểm tra logic mô tả trong task đã hiện diện đúng chưa. Nêu BẰNG CHỨNG "
    "(file:dòng) ngắn gọn.\n"
    "4) Nếu CHƯA fix và nên giao người khác: chạy "
    "`python .claude/skills/team-registry/tools/team.py match --task \"<tên task>\" --exclude <my_login>` "
    "rồi `team.py get <key>` đọc hồ sơ ứng viên đầu (skill khớp + tải) và LẤY userId aTask của họ "
    "ở `handles.atask_user_id` (đặt vào assignee_atask_id; thiếu thì để '-').\n"
    "Kết thúc câu trả lời bằng ĐÚNG một khối máy-đọc-được:\n"
    "[[VERDICT]]\n"
    "status: fixed | not_fixed | unclear\n"
    "project: <key trong projects.json hoặc '-'>\n"
    "needs_approval: yes | no   # task có cần bước review/duyệt trước khi đóng không\n"
    "estimate_days: <số ngày hợp lý để hoàn tất nếu CHƯA fix, hoặc '-'>\n"
    "assignee_name: <tên người gợi ý nếu nên giao, '-' nếu không>\n"
    "assignee_atask_id: <userId aTask của người đó, '-' nếu không có>\n"
    "reason: <một dòng vì sao>\n"
    "[[/VERDICT]]\n"
)

_VERDICT_RE = re.compile(r"\[\[VERDICT\]\](.*?)\[\[/VERDICT\]\]", re.S | re.I)


def _parse_verdict(text: str) -> dict:
    m = _VERDICT_RE.search(text or "")
    body = m.group(1) if m else ""
    out = {"status": "unclear", "project": "-", "needs_approval": "no",
           "estimate_days": "-", "assignee_name": "-", "assignee_atask_id": "-", "reason": ""}
    for line in body.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            k = k.strip().lower()
            if k in out:
                out[k] = v.strip().split("#")[0].strip()
    s = out["status"].lower()
    out["status"] = "fixed" if s.startswith("fix") else ("not_fixed" if "not" in s or "chưa" in s else "unclear")
    return out


def _analyze(task, chat) -> tuple:
    """Spawn claude -p (read-only) → (full_text, verdict_dict|None).

    Agent phân tích chạy TỰ TRỊ (`--dangerously-skip-permissions`): đọc code/chạy tool KHÔNG
    hỏi người dùng. An toàn vì system prompt ép CHỈ ĐỌC + mọi WRITE nghiệp vụ do Python làm SAU
    khi bạn duyệt qua thẻ Telegram. Chỉ 'chốt nghiệp vụ' (Hoàn thành/Chờ duyệt · Tự làm/Giao người)
    mới hỏi bạn."""
    env, _, _ = _claude_env(chat)
    sys_prompt = _ANALYSIS_SYS.replace("<my_login>", _my_login())
    prompt = (
        f"Phân tích task aTask sau và xác minh trong CODE đã fix chưa.\n"
        f"- ID: {task['id']}\n- Tên: {task['name']}\n"
        f"- Project: {task['project']} · trạng thái: {task['statusType']} · "
        f"percent: {task.get('percent')} · due: {task['due']}\n\n"
        f"Theo đúng quy trình 4 bước rồi in tóm tắt NGẮN (yêu cầu task, đã fix chưa, bằng chứng "
        f"file:dòng, rủi ro) và kết thúc bằng khối [[VERDICT]] theo quy định."
    )
    argv = [tb._claude_bin(), "-p", prompt, "--output-format", "json",
            "--append-system-prompt", sys_prompt, "--dangerously-skip-permissions"]
    if rccfg.get("TELEGRAM_AGENT_MODEL"):
        argv += ["--model", rccfg.get("TELEGRAM_AGENT_MODEL")]
    timeout = int(rccfg.get("ATASK_RESOLVE_TIMEOUT", "900") or "900")
    try:
        proc = subprocess.run(argv, cwd=REPO_ROOT, env=env, capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=timeout)
    except FileNotFoundError:
        return "❌ Không tìm thấy 'claude' trên PATH.", None
    except subprocess.TimeoutExpired:
        return "⏱️ Phân tích quá lâu, đã hủy.", None
    out = (proc.stdout or "").strip()
    try:
        result = json.loads(out).get("result") or out
    except ValueError:
        result = out
    return result, _parse_verdict(result)


# ── Telegram helpers ────────────────────────────────────────────────
def _binary_kb(req_id: str, yes_label: str, no_label: str) -> dict:
    """Inline keyboard nhị phân, callback theo chuẩn `appr:` mà bridge đã route ở
    CẢ hai mode (full + approvals-only). Nhãn nút tuỳ biến, ngữ nghĩa yes/no do caller định."""
    return {"inline_keyboard": [[
        {"text": yes_label, "callback_data": f"appr:{req_id}:yes"},
        {"text": no_label, "callback_data": f"appr:{req_id}:no"},
    ]]}


def _pick(chat, body: str, options: list, task):
    """Bộ chọn N-lựa-chọn qua Telegram, tái dùng cơ chế `appr:` — MỖI nút là MỘT approval riêng.
    options = [(label, value), ...]. Người dùng bấm nút nào → approval đó 'approved' → trả value đó.
    Trả None nếu hết giờ. Không phải sửa bridge (appr: đã được route ở cả 2 mode)."""
    reqs = []  # (req_id, value)
    buttons = []
    for label, value in options:
        rid = approvals.create("atask-resolve-pick", f"{label[:40]}", task["id"], "write")
        reqs.append((rid, value))
        buttons.append([{"text": label, "callback_data": f"appr:{rid}:yes"}])
    resp = tg_api.send_message(chat, body, reply_markup={"inline_keyboard": buttons},
                               bot=tg_api.approval_bot())
    if not resp.get("ok"):
        print(f"[{_now()}] [WARN] không gửi được menu chọn: {resp.get('description')}", file=sys.stderr)
        return None
    to = int(rccfg.get("TELEGRAM_APPROVAL_TIMEOUT", "300") or "300")
    deadline = time.time() + max(to, 600)
    chosen = None
    while time.time() < deadline:
        for rid, value in reqs:
            if approvals.get(rid).get("status") == "approved":
                chosen = value
                break
        if chosen is not None:
            break
        time.sleep(1.0)
    for rid, _ in reqs:                       # đóng các option còn treo (tránh tap lạc về sau)
        if approvals.get(rid).get("status") == "pending":
            approvals.decide(rid, False, by="auto-close")
    return chosen


def _team_candidates() -> list:
    """[(name, atask_id)] các thành viên team có atask_id (ngoài tôi), để chọn giao việc."""
    path = os.path.join(REPO_ROOT, "work", "team.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    me = _my_login()
    out = []
    for key, rec in (data.items() if isinstance(data, dict) else []):
        if key == me:
            continue
        uid = (rec.get("handles") or {}).get("atask_user_id")
        if uid:
            out.append((rec.get("name") or key, uid))
    return out


def _ask(chat, body: str, yes_label: str, no_label: str, task) -> str:
    """Gửi 1 thẻ nhị phân, chờ duyệt. Trả 'yes' | 'no' | 'timeout'."""
    req_id = approvals.create("atask-resolve", f"{task['name'][:60]}", task["id"], "write")
    resp = tg_api.send_message(chat, body, reply_markup=_binary_kb(req_id, yes_label, no_label),
                               bot=tg_api.approval_bot())
    if not resp.get("ok"):
        print(f"[{_now()}] [WARN] không gửi được thẻ duyệt: {resp.get('description')}", file=sys.stderr)
        return "timeout"
    timeout = int(rccfg.get("TELEGRAM_APPROVAL_TIMEOUT", "300") or "300")
    verdict = approvals.wait(req_id, timeout=max(timeout, 600))
    return "yes" if verdict == "approved" else ("no" if verdict == "denied" else "timeout")


def _notify(chat, text):
    try:
        tg_api.send_message(chat, text, bot=tg_api.approval_bot())
    except Exception as e:  # noqa: BLE001
        print(f"[{_now()}] [WARN] gửi Telegram lỗi: {e}", file=sys.stderr)


def _add_note(task: dict, note: str) -> bool:
    """Bổ sung thông tin cho task. Kênh theo ATASK_RESOLVE_NOTE_CHANNEL:
      - 'description' (mặc định): NỐI vào description hiện có (thuần skill, UI hiện ngay,
        không ghi đè mô tả cũ — lấy desc từ ES record trong `task`).
      - 'comment': tạo comment (chỉ HIỆN trên UI nếu backend đã vá create_comment bỏ commentIn
        + deploy; nếu chưa deploy comment sẽ bị ẩn)."""
    channel = (rccfg.get("ATASK_RESOLVE_NOTE_CHANNEL") or "description").strip().lower()
    if channel == "comment":
        cl = os.path.join(TOOLS_DIR, "checklists.py")
        try:
            proc = subprocess.run([sys.executable, cl, "add-comment", task["id"], note],
                                  cwd=TOOLS_DIR, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=60)
            return proc.returncode == 0
        except Exception as e:  # noqa: BLE001
            print(f"[{_now()}] [WARN] add-comment lỗi: {e}", file=sys.stderr)
            return False
    # description (mặc định)
    cur = (task.get("description") or "").rstrip()
    stamp = time.strftime("%Y-%m-%d %H:%M")
    new = (f"{cur}\n\n" if cur else "") + f"[auto-resolver {stamp}] {note}"
    try:
        task_api.update_task(task["id"], description=new)
        return True
    except SystemExit:
        print(f"[{_now()}] [WARN] add-note (description) lỗi cho {task['id']}", file=sys.stderr)
        return False


def _set_estimate(task_id: str, days):
    """Chỉnh estimate hợp lý: start = hôm nay, due = hôm nay + days."""
    try:
        n = int(float(days))
    except (TypeError, ValueError):
        return
    if n <= 0:
        return
    today = datetime.date.today()
    try:
        task_api.update_task(task_id, start_date=_iso(today),
                             due_date=_iso(today + datetime.timedelta(days=n)))
    except SystemExit:
        pass


def _db_status(task_id):
    """Đọc status THẬT từ DB (get_task, không qua ES) để verify sau khi ghi."""
    try:
        r = task_api.get_task(task_id)
    except SystemExit:
        return None
    d = r.get("content") if isinstance(r, dict) and isinstance(r.get("content"), dict) else r
    return d.get("status") if isinstance(d, dict) else None


# ── per-task resolution ─────────────────────────────────────────────
_ENQUEUE = False   # --enqueue: NOT_FIXED -> xếp vào queue cho worker, không hỏi từng task


def _enqueue_for_autodev(task, chat, v):
    """Chế độ batch: task chưa fix -> intake vào queue (ưu tiên theo priority aTask)
    để `queue_worker.py` tự chạy tuần tự. Trả 'queued' | 'retry'."""
    pri = task_queue.atask_priority_to_queue(task.get("priority"))
    res = task_queue.cmd_intake(_ns_q(
        source="atask", task=task["id"], project=(v.get("project") or "") or None,
        type=None, priority=pri, backend=None, model=None, post_questions=False))
    if res.get("error"):
        if "already queued" in str(res.get("message", "")):
            _log(f"[QUEUE] {task['id']} đã trong queue — bỏ qua")
            return "queued"
        _notify(chat, f"⚠️ Không xếp hàng được task <code>{html.escape(task['id'])}</code>: "
                      f"{html.escape(str(res.get('message'))[:200])}")
        return "retry"
    st = (res.get("item") or {}).get("state")
    _log(f"[QUEUE] {task['id']} -> queue (priority={pri}, state={st})")
    _notify(chat, f"📥 Đã xếp hàng <b>{html.escape(task['name'][:60])}</b> "
                  f"(ưu tiên {pri}, {st}). Chạy: <code>python queue_worker.py run</code>")
    return "queued"


def _ns_q(**kw):
    import argparse as _ap
    return _ap.Namespace(**kw)


def _handle_fixed(task, chat, v):
    summary = (f"🟢 <b>Task có vẻ ĐÃ FIX trong code</b>\n<b>{html.escape(task['name'])}</b>\n"
               f"<i>{html.escape(task['project'])} · {task['statusType']}</i>\n"
               f"Lý do: {html.escape(v['reason'][:300])}\n\nChốt trạng thái nào?")
    pick = _ask(chat, summary, "✅ Hoàn thành", "🕓 Chờ phê duyệt", task)
    _log(f"[FIXED] {task['id']} → duyệt: {'Hoàn thành' if pick=='yes' else ('Chờ phê duyệt' if pick=='no' else 'timeout')}")
    if pick == "timeout":
        _notify(chat, f"⏭️ Hết hạn duyệt — chưa đổi trạng thái task <b>{html.escape(task['name'][:60])}</b>.")
        return
    before = _db_status(task["id"])
    if pick == "yes":
        try:
            task_api.complete_task(task["id"])
        except SystemExit:
            _notify(chat, "⚠️ complete_task lỗi (backend?) — sẽ thử lại vòng sau."); return "retry"
        after = _db_status(task["id"])
        ok = after and after != before
        _log(f"[ACTION] {task['id']} complete_task → status={after} ({'OK' if ok else 'CHƯA đổi'})")
        _notify(chat, (f"✅ Đã đánh dấu HOÀN THÀNH: <b>{html.escape(task['name'][:60])}</b> "
                       f"(status=<code>{html.escape(str(after))}</code>)." if ok else
                       f"⚠️ Gọi hoàn thành nhưng DB chưa đổi (status vẫn <code>{html.escape(str(after))}</code>). "
                       f"Kiểm tra trong UI."))
    else:
        review = _resolve_status(task, "approved", "ATASK_RESOLVE_STATUS_REVIEW")
        if not review:
            _notify(chat, "⚠️ List này KHÔNG có cột 'chờ phê duyệt' (approved). Không đổi được — "
                          "chọn Hoàn thành, hoặc set <code>ATASK_RESOLVE_STATUS_REVIEW=&lt;statusId&gt;</code> nếu có.")
            return
        try:
            task_api.update_task(task["id"], status=review)
        except SystemExit:
            _notify(chat, f"⚠️ update_task(status={html.escape(str(review))}) lỗi (backend?) — thử lại vòng sau."); return "retry"
        after = _db_status(task["id"])
        ok = after == review
        _log(f"[ACTION] {task['id']} set approved={review} → DB status={after} ({'OK' if ok else 'CHƯA đổi'})")
        _notify(chat, (f"🕓 Đã chuyển <b>{html.escape(task['name'][:60])}</b> sang chờ phê duyệt "
                       f"(status=<code>{html.escape(str(review))}</code>)." if ok else
                       f"⚠️ Gọi chuyển 'chờ phê duyệt' nhưng DB chưa nhận (status vẫn "
                       f"<code>{html.escape(str(after))}</code>). Có thể do workflow aTask — đổi trong UI."))


def _handle_not_fixed(task, chat, v):
    if _ENQUEUE:   # chế độ batch: xếp hàng cho worker thay vì hỏi từng task
        return _enqueue_for_autodev(task, chat, v)
    """Menu 1 bước: Tôi tự làm / <mỗi thành viên team> / Bỏ qua. Bạn CHỌN ĐÚNG người."""
    cands = _team_candidates()            # [(name, uid)]
    sugg = (v.get("assignee_name") or "").strip().lower()
    options = [("👤 Tôi tự làm / theo dõi", "self")]
    for name, uid in cands:
        star = "⭐ " if sugg and sugg not in ("", "-") and (sugg in name.lower() or name.lower() in sugg) else ""
        options.append((f"{star}➡️ {name}", ("assign", name, uid)))
    options.append(("❌ Bỏ qua", "skip"))

    body = (f"🟠 <b>Task CHƯA fix</b>\n<b>{html.escape(task['name'])}</b>\n"
            f"<i>{html.escape(task['project'])} · {task['statusType']}</i>\n"
            f"Lý do: {html.escape(v['reason'][:250])}\n"
            + (f"AI gợi ý: <b>{html.escape(v.get('assignee_name'))}</b>\n"
               if sugg and sugg != "-" else "")
            + "\n<b>Giao cho ai?</b> (⭐ = AI gợi ý)")
    if not cands:
        body += "\n<i>(team.json chưa có ai có atask_id — chỉ chọn Tự làm/Bỏ qua)</i>"

    pick = _pick(chat, body, options, task)
    lbl = pick if isinstance(pick, str) else (pick[1] if pick else "timeout")
    _log(f"[NOT_FIXED] {task['id']} → chọn: {lbl}")

    if pick is None:
        _notify(chat, f"⏭️ Hết hạn chọn — chưa xử lý <b>{html.escape(task['name'][:60])}</b>.")
        return "skip"
    if pick == "skip":
        _notify(chat, f"⏭️ Bỏ qua <b>{html.escape(task['name'][:60])}</b>.")
        return "skip"

    if pick == "self":                    # tôi tự làm / theo dõi
        if task["statusType"] == "todo":
            inprog = _resolve_status(task, "processing", "ATASK_RESOLVE_STATUS_INPROGRESS")
            if inprog:
                try:
                    task_api.update_task(task["id"], status=inprog)
                except SystemExit:
                    pass
        _set_estimate(task["id"], v.get("estimate_days"))
        _add_note(task, f"Tôi ({_my_login()}) nhận xử lý task này. Phân tích: {v['reason']}")
        _notify(chat, f"👤 Bạn giữ <b>{html.escape(task['name'][:60])}</b> để tự làm/theo dõi "
                      f"(chỉnh estimate + ghi chú).")
        return "self"

    # ("assign", name, uid) — giao đúng người bạn chọn
    _, name, uid = pick
    others = [i for i in _user_ids(task.get("assignTaskList")) if str(i) != str(uid)]
    try:
        task_api.assign_task_users(task["id"], [int(uid)], mode="add")
        _log(f"[ACTION] {task['id']} assign_task_users {name}({uid}) OK")
    except SystemExit:
        _notify(chat, f"⚠️ Giao cho {html.escape(name)} lỗi (backend?) — sẽ thử lại vòng sau.")
        return "retry"
    except ValueError:
        _notify(chat, f"⚠️ userId của {html.escape(name)} không hợp lệ — bỏ qua.")
        return "skip"
    _set_estimate(task["id"], v.get("estimate_days"))
    _add_note(task, f"Giao cho {name}. Thông tin bổ sung từ phân tích: {v['reason']}")
    _notify(chat, f"✅ Đã giao <b>{html.escape(task['name'][:60])}</b> cho <b>{html.escape(name)}</b>"
                  + (f" ⚠️ (đã có assignee khác: {others})" if others else "")
                  + " — chỉnh estimate + ghi chú.")
    return name


def _handle_task(task, chat, state):
    # Khoá luồng resolver (chung file lock với task_queue): flow này xử lý 1 task/lúc.
    # NGOẠI LỆ --enqueue: review là READ-ONLY + enqueue chỉ ghi file queue — không đụng
    # repo code nên KHÔNG cần khoá thực thi; giữ khoá ở đây làm `--once --enqueue`
    # chỉ xử lý được đúng 1 task/lần chạy (các thread sau fail claim và "để vòng sau"
    # mà --once không có vòng sau).
    if not _ENQUEUE and not task_queue.try_claim(task_queue.DEFAULT_OWNER, task["id"]):
        _log(f"[QUEUE] flow '{task_queue.DEFAULT_OWNER}' đang bận → {task['id']} để vòng sau")
        with _inflight_lock:
            _inflight.discard(task["id"])
        return
    with _busy:
        assigned_to = _my_login()
        outcome = "?"
        mark = True   # đánh dấu đã-xử-lý (dedup theo id). Lỗi phân tích → KHÔNG đánh dấu (cho retry).
        try:
            _log(f"[RESOLVE] {task['id']} {task['name']!r} → xác minh code…")
            full, v = _analyze(task, chat)
            if v is None:
                _log(f"[VERDICT] {task['id']} → LỖI phân tích: {full[:200]}")
                _notify(chat, f"⚠️ Phân tích task <code>{html.escape(task['id'])}</code> lỗi:\n"
                              f"{html.escape(full[:500])}")
                mark = False   # lỗi tạm thời → cho poll sau thử lại
                return
            _log(f"[VERDICT] {task['id']} → {v['status']} | project={v.get('project')} "
                 f"| assignee={v.get('assignee_name')}({v.get('assignee_atask_id')}) "
                 f"| est={v.get('estimate_days')} | {v.get('reason','')[:120]}")
            summary = full.split("[[VERDICT]]")[0].strip()
            _notify(chat, f"🗂️ <b>{html.escape(task['name'])}</b>\n<pre>{html.escape(summary[:1200])}</pre>")
            outcome = v["status"]
            if v["status"] == "fixed":
                if _handle_fixed(task, chat, v) == "retry":
                    mark = False          # ghi thất bại do backend → cho poll sau thử lại
            elif v["status"] == "not_fixed":
                res = _handle_not_fixed(task, chat, v)
                if res == "retry":
                    mark = False
                elif isinstance(res, str) and res not in ("skip", "self", "queued"):
                    assigned_to = res
            else:
                _notify(chat, f"❓ Chưa rõ task <b>{html.escape(task['name'][:60])}</b> đã fix chưa "
                              f"(map project/đánh giá không chắc). Lý do: {html.escape(v['reason'][:200])}")
        finally:
            # DEDUP theo task_id: đã xử lý là bỏ qua ở mọi vòng sau (kể cả khi ta đổi statusType) →
            # chống re-read vô hạn. Muốn xử lý lại 1 task → xoá entry của nó trong state, hoặc chạy --task.
            if mark:
                state[task["id"]] = {"status": task["statusType"], "outcome": outcome,
                                     "assigned_to": assigned_to}
                _save_state(state)
            with _inflight_lock:
                _inflight.discard(task["id"])
            if not _ENQUEUE:
                task_queue.release_claim(task_queue.DEFAULT_OWNER, task["id"])


def poll_once(chat, state, act, max_per_cycle, baseline=False):
    tasks = _my_tasks()
    if tasks and tasks[0].get("error"):
        print(f"[{_now()}] [ERROR] aTask: {tasks[0].get('message')}", file=sys.stderr)
        return
    launched = 0
    for t in tasks:
        if not t.get("id"):
            continue
        if t["statusType"] in TERMINAL_TYPES:
            continue
        if t["id"] in state:            # DEDUP theo id: đã xử lý → bỏ qua (chống re-read)
            continue
        with _inflight_lock:
            if t["id"] in _inflight:
                continue
        if baseline:
            state[t["id"]] = {"status": t["statusType"], "outcome": "baseline",
                              "assigned_to": _my_login()}
            continue
        print(f"[{_now()}] [task mới] {t['id']} {t['name']!r} ({t['statusType']})")
        if not act:
            continue
        if launched >= max_per_cycle:
            print(f"[{_now()}]        (đạt {max_per_cycle} task/vòng — phần còn lại để vòng sau)")
            break
        with _inflight_lock:
            _inflight.add(t["id"])
        threading.Thread(target=_handle_task, args=(t, chat, state), daemon=True).start()
        launched += 1
    if baseline:
        _save_state(state)
        print(f"[{_now()}] baseline: đánh dấu {len(state)} task hiện có (không xử lý backlog). "
              f"Từ giờ chỉ xử lý task mới/đổi trạng thái. (--resolve-existing để làm cả backlog)")


def run(interval, act, max_per_cycle, resolve_existing):
    chat = (tg_api.allowed_chats(tg_api.approval_bot()) or [""])[0]
    if not chat:
        print("[ERROR] thiếu TELEGRAM_ALLOWED_CHATS (cần để gửi thẻ duyệt + nhận nút bấm).", file=sys.stderr)
        sys.exit(1)
    label, _ = _primary_account()
    print(f"[{_now()}] resolving MY aTask tasks (todo/processing) | account={label} | "
          f"mỗi {interval}s | tối đa {max_per_cycle} task/vòng | Ctrl+C để dừng")
    state = _load_state()
    if not state and not resolve_existing:
        poll_once(chat, state, act, max_per_cycle, baseline=True)
    while True:
        try:
            poll_once(chat, state, act, max_per_cycle)
        except KeyboardInterrupt:
            print(f"\n[{_now()}] stopped."); return
        except SystemExit as e:   # check_error sys.exit khi backend lỗi → KHÔNG được chết, chờ vòng sau
            print(f"[{_now()}] poll bị API cắt (backend?): {e} — thử lại sau {interval}s", file=sys.stderr)
        except Exception as e:  # noqa: BLE001
            print(f"[{_now()}] poll error: {e}", file=sys.stderr)
        time.sleep(interval)


if __name__ == "__main__":
    missing = config.validate() if hasattr(config, "validate") else []
    if missing:
        print(f"[ERROR] thiếu config aTask: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)
    p = argparse.ArgumentParser(prog="task_resolver.py")
    p.add_argument("--interval", type=int,
                   default=int(rccfg.get("ATASK_RESOLVE_INTERVAL", "600") or "600"),
                   help="giây giữa các vòng poll (mặc định 600)")
    p.add_argument("--once", action="store_true", help="một vòng rồi thoát")
    p.add_argument("--no-act", dest="act", action="store_false",
                   help="chỉ in task lấy được, không phân tích/ghi")
    p.add_argument("--max-per-cycle", type=int, default=2, help="số task xử lý mỗi vòng (mặc định 2)")
    p.add_argument("--resolve-existing", action="store_true", help="xử lý cả backlog (bỏ baseline)")
    p.add_argument("--task", dest="task_id", default=None,
                   help="xử lý ĐÚNG một task id rồi thoát")
    p.add_argument("--enqueue", action="store_true",
                   help="batch: NOT_FIXED -> xếp vào queue (ưu tiên theo priority aTask) "
                        "cho queue_worker chạy tuần tự, không hỏi từng task")
    a = p.parse_args()
    _ENQUEUE = a.enqueue

    if a.task_id:
        chat = (tg_api.allowed_chats(tg_api.approval_bot()) or [""])[0]
        if not chat:
            print("[ERROR] thiếu TELEGRAM_ALLOWED_CHATS.", file=sys.stderr); sys.exit(1)
        match = [t for t in _my_tasks() if t.get("id") == a.task_id]
        task = match[0] if match else {"id": a.task_id, "name": a.task_id,
                                       "statusType": "?", "status": None, "project": "",
                                       "due": "", "percent": None, "priority": None}
        _handle_task(task, chat, _load_state())
        sys.exit(0)
    if a.once:
        chat = (tg_api.allowed_chats(tg_api.approval_bot()) or [""])[0]
        poll_once(chat, _load_state(), a.act, a.max_per_cycle)
        for th in [t for t in threading.enumerate()
                   if t is not threading.current_thread() and t.daemon]:
            th.join(timeout=int(rccfg.get("TELEGRAM_APPROVAL_TIMEOUT", "300") or "300") + 120)
        sys.exit(0)
    try:
        run(a.interval, a.act, a.max_per_cycle, a.resolve_existing)
    except KeyboardInterrupt:
        print("\nstopped.")
