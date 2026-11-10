#!/usr/bin/env python3
"""Watch MY aTask tasks, analyze each, and propose assign-vs-execute (human-gated).

Poll (mặc định mỗi 10') danh sách task đang giao cho tôi (search_my_assigned_tasks).
Với mỗi task MỚI hoặc ĐỔI TRẠNG THÁI mà chưa hoàn thành:
  1. Spawn `claude -p` (read-only) đọc + phân tích task, phân loại độ khó (triage)
     rồi ĐỀ XUẤT một trong hai: EXECUTE (chạy auto-dev) hoặc ASSIGN (gợi ý người làm).
  2. Gửi đề xuất về Telegram kèm nút DUYỆT/TỪ CHỐI (qua `approvals` — bridge daemon
     giao nút bấm về; tool này KHÔNG tự poll Telegram → không tranh getUpdates).
  3. Khi DUYỆT:
       - EXECUTE → mở auto-dev trong MỘT terminal (`/auto-dev <id>`), để các
         checkpoint của pipeline vẫn do người giám sát.
       - ASSIGN  → báo người-đề-xuất về Telegram (API AI của aTask hiện KHÔNG có
         tool gán assignee-người → assign tay trong UI; chỉ `assign_task_to_sprint`).

Lần chạy đầu lập BASELINE: đánh dấu task hiện có là đã thấy, KHÔNG triage cả backlog
(tránh bắn hàng loạt) — về sau chỉ xử lý task mới/đổi trạng thái. Dùng
--triage-existing để xử lý cả backlog (giới hạn --max-per-cycle).

Cấu hình (.env): ATASK_WATCH_INTERVAL, ATASK_WATCH_DONE_TYPES, ATASK_WATCH_TIMEOUT.
(dùng lại) TELEGRAM_ALLOWED_CHATS, TELEGRAM_APPROVAL_TIMEOUT, CLAUDE_ACCOUNTS.

Chạy:
  python atask_watch.py                 # poll 10', baseline trước
  python atask_watch.py --interval 300  # poll 5'
  python atask_watch.py --once          # một vòng rồi thoát
  python atask_watch.py --no-act        # chỉ in task mới, không phân tích/duyệt
  python atask_watch.py --triage <id>   # phân tích + đề xuất + duyệt cho ĐÚNG 1 task
"""

import argparse
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

# remote-control tools: Telegram + approval IPC + account/settings helpers
_RC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__),
                                       "..", "..", "remote-control", "tools"))
sys.path.insert(0, _RC_DIR)
import approvals             # noqa: E402
import rc_config as rccfg    # noqa: E402
import telegram_bridge as tb  # noqa: E402
import tg_api                # noqa: E402

# fork-terminal (mở terminal auto-dev trên macOS/Linux)
_FT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__),
                                       "..", "..", "fork-terminal", "tools"))
sys.path.insert(0, _FT_DIR)
try:
    import fork_terminal
except Exception:
    fork_terminal = None

# dev-automation tools: shared daemon supervisor (backoff + fatal-stop + health log)
_DEV_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__),
                                        "..", "..", "dev-automation", "tools"))
sys.path.insert(0, _DEV_DIR)
import daemon_common  # noqa: E402

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = rccfg.repo_root()
STATE = os.path.join(REPO_ROOT, "temp", "atask_triaged.json")
INBOX = os.path.join(REPO_ROOT, "temp", "atask_incoming")

# statusType coi là ĐÃ XONG (không xử lý). Đè bằng ATASK_WATCH_DONE_TYPES.
DEFAULT_DONE_TYPES = ["completed", "done", "closed", "cancelled", "rejected"]

_busy = threading.Semaphore(2)   # tối đa 2 task đang phân tích/chờ duyệt cùng lúc


def _now():
    return time.strftime("%H:%M:%S")


def _load_state() -> dict:
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_state(d: dict):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2)


def _done_types() -> set:
    raw = rccfg.get_list("ATASK_WATCH_DONE_TYPES")
    return {t.lower() for t in (raw or DEFAULT_DONE_TYPES)}


def _my_tasks() -> list:
    """Danh sách task đang giao cho tôi (chuẩn hoá field cần dùng)."""
    r = search.search_my_assigned_tasks()
    if not isinstance(r, dict) or r.get("error"):
        return [{"error": True, "message": (r or {}).get("message", "lỗi my-tasks")}]
    data = ((r.get("content") or {}).get("data")) or []
    out = []
    for t in data:
        out.append({
            "id": t.get("id"),
            "name": t.get("name") or "(không tên)",
            "statusType": (t.get("statusType") or "").lower(),
            "project": t.get("projectName") or "",
            "due": t.get("dueDate") or "",
            "priority": t.get("priority"),
        })
    return out


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
    "Bạn là trợ lý kỹ thuật phân tích một task aTask để đề xuất hướng xử lý. "
    "CHỈ ĐỌC, KHÔNG ghi/đổi gì. Đọc task bằng tool aTask, đánh giá độ rõ ràng, "
    "phạm vi, độ khó. Kết thúc câu trả lời bằng ĐÚNG một khối máy-đọc-được:\n"
    "[[PROPOSAL]]\n"
    "action: execute | assign\n"
    "assignee: <tên/role gợi ý nếu assign, '-' nếu execute>\n"
    "tier: trivial | standard | complex\n"
    "reason: <một dòng vì sao>\n"
    "[[/PROPOSAL]]\n"
    "Chọn EXECUTE nếu task đủ rõ và là việc code có thể tự động qua pipeline "
    "(Plan→Implement→Test→MR). Chọn ASSIGN nếu cần con người (mơ hồ, cần quyết định "
    "nghiệp vụ, ngoài phạm vi tự động, hoặc nên giao người khác).\n"
    "KHI ASSIGN: tra kho đội nhóm để gợi ý ĐÚNG người — chạy "
    "`python .claude/skills/team-registry/tools/team.py match --task \"<tên task>\" "
    "--exclude <my_login>`, rồi `team.py get <key>` đọc hồ sơ ứng viên đầu, chọn người "
    "hợp nhất (skill khớp + tải + tính cách) và đặt TÊN họ vào trường assignee."
)

_PROPOSAL_RE = re.compile(r"\[\[PROPOSAL\]\](.*?)\[\[/PROPOSAL\]\]", re.S | re.I)


def _parse_proposal(text: str) -> dict:
    m = _PROPOSAL_RE.search(text or "")
    body = m.group(1) if m else ""
    out = {"action": "assign", "assignee": "-", "tier": "?", "reason": ""}
    for line in body.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            k = k.strip().lower()
            if k in out:
                out[k] = v.strip()
    out["action"] = "execute" if out["action"].lower().startswith("exec") else "assign"
    return out


def _analyze(task, chat) -> tuple:
    """Spawn claude -p (read-only) phân tích task. Trả (full_text, proposal_dict)."""
    settings = tb._write_bridge_settings()
    env, label, _ = _claude_env(chat)
    prompt = (
        f"Phân tích task aTask sau rồi đề xuất hướng xử lý.\n"
        f"- ID: {task['id']}\n- Tên: {task['name']}\n"
        f"- Project: {task['project']} · trạng thái: {task['statusType']} · due: {task['due']}\n\n"
        f"Đọc chi tiết: `python .claude/skills/atask-automation/tools/tasks.py get {task['id']}`. "
        f"Có thể dùng thêm subtasks/search nếu cần. Sau khi phân tích, in tóm tắt NGẮN "
        f"(việc cần làm, độ khó, rủi ro) rồi kết thúc bằng khối [[PROPOSAL]] theo quy định."
    )
    argv = [tb._claude_bin(), "-p", prompt, "--output-format", "json",
            "--append-system-prompt",
            _ANALYSIS_SYS.replace("<my_login>", rccfg.get("ATASK_MY_LOGIN", "")),
            "--settings", settings]
    if rccfg.get("TELEGRAM_AGENT_MODEL"):
        argv += ["--model", rccfg.get("TELEGRAM_AGENT_MODEL")]
    timeout = int(rccfg.get("ATASK_WATCH_TIMEOUT", "900") or "900")
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
    return result, _parse_proposal(result)


# ── actions on approval ─────────────────────────────────────────────
def _launch_autodev(task, chat):
    """Mở auto-dev cho task trong MỘT terminal (interactive, để giám sát checkpoint)."""
    os.makedirs(INBOX, exist_ok=True)
    _, _, home = _claude_env(chat)
    skip = "--dangerously-skip-permissions"
    cmd = f'claude {skip} "/auto-dev {task["id"]}"'
    try:
        if os.name == "nt":
            launcher = os.path.normpath(os.path.join(INBOX, f"autodev_{task['id']}.cmd"))
            with open(launcher, "w", encoding="utf-8") as f:
                f.write("@echo off\r\n")
                if home:
                    f.write(f'set "USERPROFILE={home}"\r\n')
                f.write(f'cd /d "{os.path.normpath(REPO_ROOT)}"\r\n')
                f.write(f"echo Auto-dev task {task['id']} ...\r\n")
                f.write(cmd + "\r\n")
            subprocess.Popen(f'start "Auto-dev {task["id"]}" cmd /k "{launcher}"', shell=True)
        elif fork_terminal is not None:
            pre = f'USERPROFILE="{home}" HOME="{home}" ' if home else ""
            fork_terminal.fork_terminal(f'cd "{REPO_ROOT}" && {pre}{cmd}')
        else:
            print(f"[{_now()}] [WARN] không có terminal spawner; chạy tay: {cmd}", file=sys.stderr)
            return False
        print(f"[{_now()}]        -> đã mở auto-dev cho task {task['id']}")
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[{_now()}] [WARN] không mở được auto-dev: {e}", file=sys.stderr)
        return False


def _notify(chat, text):
    try:
        tg_api.send_message(chat, text, bot=tg_api.approval_bot())
    except Exception as e:  # noqa: BLE001
        print(f"[{_now()}] [WARN] gửi Telegram lỗi: {e}", file=sys.stderr)


def _handle_task(task, chat, state):
    """Phân tích → đề xuất → xin duyệt → hành động. Chạy trong thread riêng."""
    import html
    with _busy:
        try:
            print(f"[{_now()}] [TRIAGE] {task['id']} {task['name']!r} → phân tích…")
            full, prop = _analyze(task, chat)
            if prop is None:
                _notify(chat, f"⚠️ Phân tích task <code>{html.escape(task['id'])}</code> lỗi:\n{html.escape(full[:500])}")
                return
            action = prop["action"]
            head = "▶️ Thực hiện (auto-dev)" if action == "execute" else f"👤 Giao người: {prop['assignee']}"
            summary = full.split("[[PROPOSAL]]")[0].strip()
            req_id = approvals.create("atask-triage",
                                      f"{action} · {task['name'][:60]}", task["id"], "write")
            text = (f"🗂️ <b>Đề xuất xử lý task</b>\n"
                    f"<b>{html.escape(task['name'])}</b>\n"
                    f"<i>{html.escape(task['project'])} · {task['statusType']} · due {task['due']}</i>\n\n"
                    f"<b>Đề xuất:</b> {html.escape(head)} (tier {html.escape(prop['tier'])})\n"
                    f"<b>Lý do:</b> {html.escape(prop['reason'])}\n\n"
                    f"<pre>{html.escape(summary[:1500])}</pre>")
            resp = tg_api.send_message(chat, text, reply_markup=tg_api.approve_keyboard(req_id),
                                       bot=tg_api.approval_bot())
            if not resp.get("ok"):
                print(f"[{_now()}] [WARN] không gửi được yêu cầu duyệt: {resp.get('description')}")
                return
            timeout = int(rccfg.get("TELEGRAM_APPROVAL_TIMEOUT", "300") or "300")
            verdict = approvals.wait(req_id, timeout=max(timeout, 600))
            if verdict != "approved":
                print(f"[{_now()}]        -> {verdict} (không hành động).")
                _notify(chat, f"⏭️ Bỏ qua task <b>{html.escape(task['name'][:60])}</b> ({verdict}).")
                return
            if action == "execute":
                ok = _launch_autodev(task, chat)
                _notify(chat, ("✅ Đã khởi chạy auto-dev cho <b>"
                               f"{html.escape(task['name'][:60])}</b> (terminal giám sát)."
                               if ok else "⚠️ Không mở được auto-dev — xem log máy chủ."))
            else:
                _notify(chat, (f"👤 <b>Đề xuất giao việc</b>\nTask: <b>{html.escape(task['name'])}</b>\n"
                               f"Người đề xuất: <b>{html.escape(prop['assignee'])}</b>\n"
                               f"Lý do: {html.escape(prop['reason'])}\n\n"
                               f"<i>(API aTask chưa hỗ trợ gán assignee tự động — assign tay trong UI.)</i>"))
        finally:
            # dù kết quả nào cũng đánh dấu đã xử lý ở trạng thái này (tránh lặp)
            state[task["id"]] = task["statusType"]
            _save_state(state)


def poll_once(chat, state, act, max_per_cycle, baseline=False):
    tasks = _my_tasks()
    if tasks and tasks[0].get("error"):
        # Let the supervisor classify: 401/403 -> stop+notify; network/5xx -> backoff.
        daemon_common.guard(tasks[0], "aTask")
    done = _done_types()
    launched = 0
    for t in tasks:
        if not t.get("id"):
            continue
        if t["statusType"] in done:
            continue                       # đã xong → bỏ qua
        if state.get(t["id"]) == t["statusType"]:
            continue                       # đã xử lý ở đúng trạng thái này
        if baseline:
            state[t["id"]] = t["statusType"]
            continue
        tag = "đổi trạng thái" if t["id"] in state else "mới"
        print(f"[{_now()}] [task {tag}] {t['id']} {t['name']!r} ({t['statusType']})")
        if not act:
            continue
        if launched >= max_per_cycle:
            print(f"[{_now()}]        (đạt {max_per_cycle} task/vòng — phần còn lại để vòng sau)")
            break
        threading.Thread(target=_handle_task, args=(t, chat, state), daemon=True).start()
        launched += 1
    if baseline:
        _save_state(state)
        print(f"[{_now()}] baseline: đánh dấu {len(state)} task hiện có (không triage backlog). "
              f"Từ giờ chỉ xử lý task mới/đổi trạng thái. (--triage-existing để làm cả backlog)")


def run(interval, act, max_per_cycle, triage_existing):
    chat = (tg_api.allowed_chats(tg_api.approval_bot()) or [""])[0]
    if not chat:
        print("[ERROR] thiếu TELEGRAM_ALLOWED_CHATS (cần để gửi đề xuất + nhận duyệt).", file=sys.stderr)
        sys.exit(1)
    label, _ = _primary_account()
    print(f"[{_now()}] watching MY aTask tasks | account={label} | mỗi {interval}s | "
          f"tối đa {max_per_cycle} task/vòng | Ctrl+C để dừng")
    state = _load_state()
    pending_baseline = not state and not triage_existing

    def _poll():
        nonlocal pending_baseline
        if pending_baseline:
            poll_once(chat, state, act, max_per_cycle, baseline=True)
            pending_baseline = False
        else:
            poll_once(chat, state, act, max_per_cycle)

    # Supervised: backoff on network/5xx, stop + Telegram alert on 401/403 (PAT expired).
    daemon_common.supervise(_poll, interval=interval, label="atask_watch",
                            notify=lambda msg: _notify(chat, msg))


if __name__ == "__main__":
    missing = config.validate() if hasattr(config, "validate") else []
    if missing:
        print(f"[ERROR] thiếu config aTask: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)
    p = argparse.ArgumentParser(prog="atask_watch.py")
    p.add_argument("--interval", type=int,
                   default=int(rccfg.get("ATASK_WATCH_INTERVAL", "600") or "600"),
                   help="giây giữa các vòng poll (mặc định 600)")
    p.add_argument("--once", action="store_true", help="một vòng rồi thoát")
    p.add_argument("--no-act", dest="act", action="store_false",
                   help="chỉ in task mới, không phân tích/duyệt/hành động")
    p.add_argument("--max-per-cycle", type=int, default=2, help="số task xử lý mỗi vòng (mặc định 2)")
    p.add_argument("--triage-existing", action="store_true", help="xử lý cả backlog (bỏ baseline)")
    p.add_argument("--triage", dest="triage_id", default=None,
                   help="phân tích + đề xuất + duyệt cho ĐÚNG một task id rồi thoát")
    a = p.parse_args()

    if a.triage_id:
        chat = (tg_api.allowed_chats(tg_api.approval_bot()) or [""])[0]
        if not chat:
            print("[ERROR] thiếu TELEGRAM_ALLOWED_CHATS.", file=sys.stderr); sys.exit(1)
        match = [t for t in _my_tasks() if t.get("id") == a.triage_id]
        task = match[0] if match else {"id": a.triage_id, "name": a.triage_id,
                                       "statusType": "?", "project": "", "due": "", "priority": None}
        _handle_task(task, chat, _load_state())
        sys.exit(0)
    if a.once:
        chat = (tg_api.allowed_chats(tg_api.approval_bot()) or [""])[0]
        try:
            poll_once(chat, _load_state(), a.act, a.max_per_cycle)
        except daemon_common.DaemonError as e:
            print(f"[ERROR] {e}", file=sys.stderr); sys.exit(1)
        # chờ các thread xử lý xong (duyệt) trước khi thoát
        for th in [t for t in threading.enumerate() if t is not threading.current_thread() and t.daemon]:
            th.join(timeout=int(rccfg.get("TELEGRAM_APPROVAL_TIMEOUT", "300") or "300") + 120)
        sys.exit(0)
    try:
        run(a.interval, a.act, a.max_per_cycle, a.triage_existing)
    except KeyboardInterrupt:
        print("\nstopped.")
