#!/usr/bin/env python3
"""Watch ONE TChat group and drive code-review / build-to-dev from it.

Cầu nối TChat -> Claude Code: lắng nghe đúng MỘT group qua socket realtime,
khi có tin nhắn trông như yêu cầu review MR hoặc build lên dev thì giao nguyên
văn cho một agent `claude -p` headless (full agent, mọi skill — dev-automation,
gitlab, jenkins) chạy ở gốc repo. Kết quả báo về CẢ Telegram LẪN chính group đó.

Khác với listen.py (lo các DM 1-1), tool này CHỈ theo dõi group mục tiêu và
KHÔNG auto-reply chuyện phiếm — nó chỉ kích hoạt khi tin khớp prefilter từ khoá.

Tận dụng hạ tầng có sẵn:
  - Vòng WS + helper đọc lịch sử của tchat (`listen`, `client`, `send`).
  - Hook duyệt nút bấm của remote-control: agent chạy với CLAUDE_TG_BRIDGE=1 +
    `bridge_settings.json`, nên mọi thao tác [WRITE]/build hiện nút Duyệt/Từ chối
    trên Telegram. Bridge daemon (telegram_bridge.py) PHẢI đang chạy để giao nút
    bấm về — tool này không tự poll Telegram (tránh tranh getUpdates với bridge).

Cấu hình (.env ở gốc repo):
  FCHAT_WATCH_GROUP     id group cần theo dõi (bắt buộc; hoặc truyền --group)
  FCHAT_WATCH_KEYWORDS  prefilter, phân tách bằng dấu phẩy (mặc định bên dưới)
  FCHAT_WATCH_TIMEOUT   giây tối đa cho một lượt claude -p (mặc định 1800)
  (dùng lại) TELEGRAM_ALLOWED_CHATS, TELEGRAM_AUTO_APPROVE, CLAUDE_ACCOUNTS, ...

Chạy:
  python group_watch.py                       # group lấy từ FCHAT_WATCH_GROUP
  python group_watch.py --group <id>          # đè group
  python group_watch.py --once "<text>"       # test: xử lý 1 câu, không cần WS
"""

import argparse
import json
import os
import queue
import re
import socket
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import client          # noqa: E402  (tchat)
import config          # noqa: E402  (tchat)
import listen          # noqa: E402  (tchat) — reuse _looks_encrypted/_fetch_history/WS bits
try:
    import crypto       # noqa: E402  E2E decrypt (vắng key → no-op)
except Exception:       # noqa: BLE001
    crypto = None
import send            # noqa: E402  (tchat)
import tokens          # noqa: E402  (tchat)
import ws_client       # noqa: E402  (tchat)

# remote-control tools (Telegram + approval hook + account resolution)
_RC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__),
                                       "..", "..", "remote-control", "tools"))
sys.path.insert(0, _RC_DIR)
import approvals              # noqa: E402  (file-backed approve gate, resolved by bridge)
import rc_config as rccfg     # noqa: E402
import telegram_bridge as tb  # noqa: E402  (reuse settings/account/claude-bin helpers)
import tg_api                 # noqa: E402

# dev-automation: shared health log so `daemon_common.py status` sees this watcher too.
_DEV_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__),
                                        "..", "..", "dev-automation", "tools"))
sys.path.insert(0, _DEV_DIR)
try:
    import daemon_common  # noqa: E402
except Exception:  # noqa: BLE001
    daemon_common = None


def _hlog(event, detail=""):
    if daemon_common is not None:
        daemon_common.health_log("group_watch", event, detail)

# Prefilter rẻ: chỉ tin chứa MỘT trong các cụm này mới được đưa cho agent. Mục
# đích là lọc bớt chuyện phiếm; agent vẫn là người quyết cuối (trả 'SKIP' nếu
# tin không thực sự là yêu cầu). Đè bằng FCHAT_WATCH_KEYWORDS.
DEFAULT_KEYWORDS = [
    "mr", "merge request", "review", "build", "deploy", "lên dev", "len dev",
    "merge", "pipeline", "!review", "!build", "/review", "/build",
]

# Dạy agent bối cảnh + hành vi. Ghép thêm LONGTASK_SYS của bridge (bg_notify cho
# build chạy lâu) để khỏi lặp lại.
WATCH_SYS = (
    "BỐI CẢNH: bạn được kích hoạt bởi MỘT tin nhắn trong group TChat phục vụ "
    "review code và build lên dev. Tin nhắn là yêu cầu của một thành viên nhóm. "
    "Phần 'LỊCH SỬ GẦN ĐÂY' (nếu có) chỉ để tra ngữ cảnh (vd link/số MR nhắc ở "
    "tin trước).\n"
    "QUY TẮC:\n"
    "1. Nếu tin KHÔNG thực sự là yêu cầu review MR hay build/deploy (chỉ là tán "
    "gẫu lỡ khớp từ khoá), hãy trả lời ĐÚNG MỘT TỪ: SKIP — và không làm gì khác.\n"
    "2. REVIEW MR: BẮT BUỘC theo quy trình review CÓ CẤU TRÚC của dev-automation, "
    "KHÔNG được nhận xét sơ sài vài dòng. ĐỌC TRƯỚC 3 file:\n"
    "   - .claude/skills/dev-automation/cookbook/review-merge-request.md\n"
    "   - .claude/skills/dev-automation/prompts/code_review_prompt.md (TEMPLATE bắt buộc theo)\n"
    "   - .claude/skills/dev-automation/cookbook/java-standards.md\n"
    "   Lấy diff bằng `gitlab_api.py mr-changes <iid>` và `gitlab_api.py mr-discussions <iid>`, "
    "đánh giá ĐỦ các khía cạnh: Correctness/Logic, Code Quality, Architecture, Security, "
    "Performance, Testing. ĐĂNG nhận xét ĐẦY ĐỦ theo template (Overview, Critical Issues, "
    "Major Suggestions, Minor/Nitpicks, bảng Standards Compliance, Positive Observations, "
    "verdict APPROVE/REQUEST_CHANGES/COMMENT) lên MR bằng "
    "`gitlab_api.py mr-comment <iid> \"...\"` — markdown đầy đủ là ĐÚNG ở đây (GitLab render được). "
    "Dùng BADGE EMOJI MÀU cho dễ nhìn: verdict 🟢 APPROVE / 🟡 COMMENT / 🔴 REQUEST_CHANGES; "
    "mức độ issue 🔴 Critical · 🟡 Major · 🟢 Minor; bảng Standards Compliance dùng ✅/⚠️/❌ "
    "thay vì chữ pass/fail (GitLab nội bộ thường không ra internet nên KHÔNG dùng badge ảnh "
    "shields.io — sẽ vỡ).\n"
    "3. BUILD/DEPLOY lên dev: luồng này ĐÃ được người dùng chấp nhận → cứ chạy, "
    "KHÔNG chờ duyệt và KHÔNG tự hỏi lại bằng văn xuôi. BẮT BUỘC bọc lệnh build bằng "
    "bg_notify.py (build chạy lâu — xem hướng dẫn dưới) để kết quả tự báo về khi xong.\n"
    "4. Nhận xét ĐẦY ĐỦ nằm Ở TRÊN MR (bước 2). RIÊNG câu trả lời cuối cùng của lượt — "
    "phần sẽ đăng lại vào group chat TChat — phải NGẮN GỌN (2-4 dòng: verdict + vài ý chính "
    "+ link/MR), tiếng Việt, KHÔNG markdown (chat hiển thị plaintext). BẮT BUỘC kết thúc "
    "bằng MỘT dòng riêng đúng dạng: `VERDICT: APPROVE` hoặc `VERDICT: REQUEST_CHANGES` hoặc "
    "`VERDICT: COMMENT` — hệ thống đọc dòng này để quyết định có MỜI DUYỆT MERGE hay không "
    "(chỉ APPROVE mới mời merge).\n"
    "5. Để giao tiếp/giao việc hợp với người gửi, có thể tra kho đội nhóm: "
    "`python .claude/skills/team-registry/tools/team.py get <username>` lấy "
    "personality/interaction rồi điều chỉnh giọng cho phù hợp.\n\n"
    + tb.LONGTASK_SYS
)


def _now():
    return time.strftime("%H:%M:%S")


def _keywords():
    kws = rccfg.get_list("FCHAT_WATCH_KEYWORDS") if rccfg.get("FCHAT_WATCH_KEYWORDS") else None
    return [k.lower() for k in (kws or DEFAULT_KEYWORDS)]


def _looks_like_request(text: str, kws) -> bool:
    t = (text or "").lower()
    return any(k in t for k in kws)


def _guess_intent(text: str) -> str:
    """Phân loại yêu cầu để ĐỊNH TUYẾN (review→cổng duyệt merge | build | xử lý).

    BUILD/DEPLOY được ưu tiên TRƯỚC review: 'deploy MR 412 lên dev' là hành động
    build, không phải review. 'mr'/'merge'/'pr' khớp theo RANH GIỚI TỪ để 'build MR
    412' không bị hiểu nhầm vì chứa substring 'mr'."""
    t = (text or "").lower()
    if any(k in t for k in ("build", "deploy", "lên dev", "len dev", "pipeline", "jenkins")):
        return "build"
    if re.search(r"\b(review|mr|merge|pull request|pr)\b", t):
        return "review"
    return "xử lý"


# Tin báo nhận gửi vào group NGAY khi bắt đầu, để mọi người biết bot đang làm gì.
_ACK = {
    "review": "🤖 Đã nhận yêu cầu — đang review, sẽ báo lại khi xong.",
    "build": "🤖 Đã nhận yêu cầu build lên dev — chờ duyệt rồi chạy, sẽ báo lại khi xong.",
    "xử lý": "🤖 Đã nhận yêu cầu — đang xử lý, sẽ báo lại khi xong.",
}


def _send_group(gid, group_type, text, metadata=None):
    """Gửi 1 tin vào group; trả messageIdInc (để có thể thu hồi) hoặc None."""
    try:
        res = send.send_text(gid, text, group_type, metadata=metadata)
        inc = ((res.get("echo") or {}).get("data") or {}).get("messageIdInc")
        return inc
    except Exception as e:  # noqa: BLE001
        print(f"[{_now()}] [WARN] gửi group lỗi: {e}", file=sys.stderr)
        return None


def _primary_account():
    """(label, home) tài khoản chính theo CLAUDE_ACCOUNTS (mặc định work)."""
    accts = tb._accounts()
    return accts[0] if accts else ("default", None)


def _invoke_agent(text, history_block, gid, group_type, sender_id, sender_name):
    """Chạy một lượt `claude -p` cho yêu cầu này. Trả (result_text, is_error)."""
    settings_path = tb._write_bridge_settings()      # gắn hook duyệt Telegram
    label, home = _primary_account()
    chat = (tg_api.allowed_chats(tg_api.approval_bot()) or [""])[0]   # chat nhận yêu cầu duyệt (bot ops)

    prompt = text
    if history_block:
        prompt = f"{text}\n\n--- LỊCH SỬ GẦN ĐÂY (chỉ để tra ngữ cảnh) ---\n{history_block}"

    argv = [tb._claude_bin(), "-p", prompt,
            "--output-format", "json",
            "--append-system-prompt", WATCH_SYS,
            "--settings", settings_path]
    if rccfg.get("TELEGRAM_AGENT_MODEL"):
        argv += ["--model", rccfg.get("TELEGRAM_AGENT_MODEL")]

    env = dict(os.environ)
    env["CLAUDE_TG_BRIDGE"] = "1"
    env["CLAUDE_TG_CHAT_ID"] = str(chat)
    # Auto-duyệt CHO RIÊNG lượt group_watch: review (post comment) + build lên dev
    # là thao tác đã được người dùng chấp nhận cho luồng này → không hỏi/chờ nút ở
    # bot ops. Chỉ nới các nhóm read/file/bash; 'danger' vẫn luôn phải duyệt. Đè
    # bằng FCHAT_WATCH_AUTO_APPROVE (vd để 'read' nếu muốn build hỏi lại).
    env["CLAUDE_TG_AUTO_APPROVE"] = rccfg.get("FCHAT_WATCH_AUTO_APPROVE") or "read,file,bash"
    # Context để bg_notify.py báo kết quả build (chạy nền) VỀ TChat + tag người
    # yêu cầu, thay vì chỉ về Telegram. Truyền qua env → kế thừa xuống tiến trình
    # bg_notify tách rời.
    env["FCHAT_NOTIFY_GROUP"] = str(gid)
    env["FCHAT_NOTIFY_GROUP_TYPE"] = str(group_type or "")
    env["FCHAT_NOTIFY_USER_ID"] = str(sender_id or "")
    env["FCHAT_NOTIFY_USER_NAME"] = str(sender_name or "")
    if home:
        env["HOME"] = home            # macOS / Linux
        env["USERPROFILE"] = home     # Windows

    timeout = int(rccfg.get("FCHAT_WATCH_TIMEOUT", "1800") or "1800")
    try:
        proc = subprocess.run(argv, cwd=rccfg.repo_root(), env=env,
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout)
    except FileNotFoundError:
        return "❌ Không tìm thấy 'claude' trên PATH (đặt CLAUDE_BIN trong .env).", True
    except subprocess.TimeoutExpired:
        return "⏱️ Agent chạy quá lâu, đã hủy.", True

    out = (proc.stdout or "").strip()
    try:
        obj = json.loads(out)
        result = obj.get("result") or obj.get("error") or out
        return result, bool(obj.get("is_error"))
    except ValueError:
        if proc.returncode != 0:
            return (out + "\n" + (proc.stderr or "")).strip() or "claude lỗi", True
        return out, False


def _post_results(gid, group_type, sender_name, sender_id, original, result_text):
    """Đăng kết quả về group TChat (plaintext), TAG người tạo yêu cầu.

    Bố cục gọn như bản Telegram gốc (vốn 'khá ok'): tag người yêu cầu + trích lại
    YÊU CẦU, một dòng trống, rồi tới NỘI DUNG kết quả — để dễ đọc thay vì dính liền.
    KHÔNG bắn sang Telegram (Telegram chỉ còn dùng cho nút Duyệt build)."""
    quoted = " ".join((original or "").split())          # gộp xuống dòng/space thừa
    if len(quoted) > 160:
        quoted = quoted[:160] + "…"
    # @Tên ở đầu (offset 0) để mention chuẩn; emoji/nội dung phía sau không ảnh hưởng.
    body = f"📋 Kết quả cho yêu cầu “{quoted}”:\n\n{result_text}"
    _post_tagged(gid, group_type, sender_name, sender_id, body)


def _post_tagged(gid, group_type, sender_name, sender_id, text):
    """Đăng MỘT tin vào TChat group, tag người yêu cầu (mention ở offset 0). Trả inc."""
    content, metadata = send.with_mentions_prefix(text, [(sender_name, sender_id)])
    inc = _send_group(gid, group_type, content, metadata=metadata)
    print(f"[{_now()}]        -> đăng group: {'ok inc=' + str(inc) if inc else 'gửi (chưa xác nhận)'}"
          f"{' (đã tag ' + sender_name + ')' if metadata.get('mentions') else ''}")
    return inc


# Hàng đợi xử lý: tin tới trong lúc agent đang chạy sẽ được XẾP HÀNG (không bỏ
# sót). Một worker DUY NHẤT rút từng tin ra xử lý TUẦN TỰ — giữ nguyên ý đồ "mỗi
# lần một lượt claude -p" (tránh hai build/duyệt Telegram chồng nhau).
_work_q = queue.Queue()


def _process_one(gid, group_type, sender_name, sender_id, text, me):
    """Một lượt xử lý đầy đủ cho MỘT tin (chạy trong worker, tuần tự)."""
    print(f"[{_now()}] [TRIGGER] {sender_name}: {text!r} -> giao cho agent…")
    # Báo nhận ngay vào group để mọi người biết bot đang làm gì. Giữ lại inc
    # để thu hồi nếu agent quyết định đây không phải yêu cầu (SKIP).
    ack_inc = _send_group(gid, group_type, _ACK[_guess_intent(text)])
    hist = listen._fetch_history(gid, me, limit=15)
    history_block = "\n".join(hist[:-1]) if len(hist) > 1 else ""  # bỏ chính tin vừa tới
    result, is_error = _invoke_agent(text, history_block, gid, group_type, sender_id, sender_name)
    result = (result or "").strip()
    if not is_error and result.upper().startswith("SKIP"):
        print(f"[{_now()}]        -> agent đánh giá KHÔNG phải yêu cầu (SKIP).")
        if ack_inc:  # thu hồi tin báo nhận để khỏi để lại rác trong group
            try:
                send.recall_message(gid, ack_inc)
                print(f"[{_now()}]        -> đã thu hồi tin báo nhận (inc={ack_inc}).")
            except Exception as e:  # noqa: BLE001
                print(f"[{_now()}] [WARN] thu hồi báo nhận lỗi: {e}", file=sys.stderr)
        return
    if is_error:
        result = "⚠️ " + result
        _post_results(gid, group_type, sender_name, sender_id, text, result)
        return
    # REVIEW: LUÔN báo KẾT QUẢ REVIEW về TChat (tag người yêu cầu), bất kể verdict.
    # Việc DUYỆT MERGE là riêng trên Telegram: chỉ khi verdict == APPROVE mới hỏi; chỉ
    # khi bạn ĐỒNG Ý mới báo thêm 'đã merge' về TChat, từ chối/hết hạn thì im.
    if _guess_intent(text) == "review":
        verdict = _extract_verdict(result)
        mr_iid = _extract_mr_iid(text) or _extract_mr_iid(result)
        _post_results(gid, group_type, sender_name, sender_id, text,
                      result or "(agent không trả về gì)")
        if verdict == "APPROVE":
            threading.Thread(target=_await_merge,
                             args=(gid, group_type, sender_name, sender_id, mr_iid),
                             daemon=True).start()
            print(f"[{_now()}]        -> đã báo review; verdict APPROVE → hỏi duyệt merge (Telegram, nền).")
        else:
            print(f"[{_now()}]        -> đã báo review; verdict={verdict} → không mời merge.")
        return
    # BUILD / khác: đăng kết quả thẳng (build chạy nền tự báo qua bg_notify).
    _post_results(gid, group_type, sender_name, sender_id, text, result or "(agent không trả về gì)")


def _worker_count():
    """Số worker chạy song song (FCHAT_WATCH_WORKERS, mặc định 3, tối thiểu 1)."""
    try:
        return max(1, int(rccfg.get("FCHAT_WATCH_WORKERS", "3") or "3"))
    except ValueError:
        return 3


def _worker():
    """Một worker trong pool: rút từng tin khỏi hàng đợi và xử lý, không bỏ sót.
    Nhiều worker chạy song song → nhiều tác vụ cùng lúc. Lỗi ở một tin KHÔNG giết
    worker (các tin sau vẫn chạy)."""
    while True:
        item = _work_q.get()
        try:
            _process_one(*item)
        except Exception as e:  # noqa: BLE001
            print(f"[{_now()}] [WARN] xử lý tin lỗi, bỏ qua tin này: {e}", file=sys.stderr)
        finally:
            _work_q.task_done()


# ── Lệnh của CHÍNH chủ tài khoản (chống loop vô hạn) ──────────────────────
# Tin bot tự đăng (kết quả/ack) cũng có senderId == me. Nếu xử lý mọi tin của
# mình thì bot sẽ tự kích hoạt lại → loop. Giải pháp: tin của mình CHỈ được xử lý
# khi mở đầu bằng TIỀN TỐ lệnh (mặc định "@bot ") — tin bot tự đăng KHÔNG có tiền
# tố này nên không bao giờ tự trigger.
def _self_prefix():
    return (rccfg.get("FCHAT_SELF_PREFIX") or "@bot").strip().lower()


def _strip_self_prefix(content):
    """Trả phần lệnh (đã bỏ tiền tố) nếu content mở đầu bằng tiền tố; None nếu không."""
    if not content:
        return None
    s = content.strip()
    pfx = _self_prefix()
    if s.lower().startswith(pfx):
        return s[len(pfx):].lstrip(" :\t").strip() or None
    return None


_MR_RE = re.compile(r'(?:\bMR\s*#?|\bmerge\s+request\s*#?|!)\s*(\d{1,7})', re.IGNORECASE)


def _extract_mr_iid(text):
    m = _MR_RE.search(text or "")
    return m.group(1) if m else None


_VERDICT_RE = re.compile(r'VERDICT\s*[:\-]?\s*(APPROVE|REQUEST[_\s]?CHANGES|COMMENT)',
                         re.IGNORECASE)


def _extract_verdict(text):
    """Đọc verdict agent ghi ở cuối câu trả lời chat. Trả APPROVE/REQUEST_CHANGES/
    COMMENT, hoặc None nếu không thấy (→ coi như KHÔNG được merge)."""
    m = _VERDICT_RE.search(text or "")
    if not m:
        return None
    v = m.group(1).upper().replace(" ", "_")
    return "REQUEST_CHANGES" if v.startswith("REQUEST") else v


def _gitlab_merge(mr_iid):
    """Gọi gitlab_api.py merge-mr <iid>. Trả {'ok': bool, 'error': str?}."""
    script = os.path.join(rccfg.repo_root(), ".claude", "skills", "dev-automation",
                          "tools", "gitlab_api.py")
    try:
        r = subprocess.run([sys.executable, script, "merge-mr", str(mr_iid)],
                           cwd=rccfg.repo_root(), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=120)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}
    out = (r.stdout or "").strip()
    obj = {}
    if "{" in out:
        try:
            obj = json.loads(out[out.find("{"):out.rfind("}") + 1])
        except Exception:  # noqa: BLE001
            obj = {}
    if obj.get("error"):
        return {"ok": False, "error": (obj.get("message") or "merge bị từ chối")[:200]}
    if obj.get("state") == "merged":
        return {"ok": True}
    return {"ok": False, "error": (out[:200] or f"exit {r.returncode}")}


def _await_merge(gid, group_type, sender_name, sender_id, mr_iid):
    """(Chạy NỀN) Cổng DUYỆT MERGE — việc RIÊNG trên Telegram. Kết quả review đã được
    báo về TChat TRƯỚC đó (ở _process_one), bất kể verdict.

    - BẠN ĐỒNG Ý merge → merge MR + báo thêm 1 tin 'đã merge' về TChat (tag người
      yêu cầu).
    - TỪ CHỐI / HẾT HẠN → IM LẶNG phía TChat (quyết định merge là việc riêng của bạn).
    Tách thread để worker xử lý tin kế tiếp ngay, không bị chặn suốt thời gian chờ."""
    mr_label = f"MR !{mr_iid}" if mr_iid else "MR"
    appr_bot = tg_api.approval_bot()
    chat = (tg_api.allowed_chats(appr_bot) or [None])[0]
    if not chat:
        print(f"[{_now()}] [merge] không có Telegram để hỏi duyệt — bỏ qua.", file=sys.stderr)
        return
    req_id = approvals.create("merge-mr", f"Merge {mr_label}", risk="write")
    tg_api.send_message(chat, f"👉 Duyệt MERGE {mr_label}? (review: 🟢 APPROVE)",
                        reply_markup=tg_api.approve_keyboard(req_id), bot=appr_bot)
    timeout = int(rccfg.get("FCHAT_MERGE_APPROVE_TIMEOUT", "1800") or "1800")
    status = approvals.wait(req_id, timeout=timeout)

    if status != "approved":
        # từ chối / hết hạn → KHÔNG đụng tới TChat
        print(f"[{_now()}] [merge] {mr_label}: {status} → không merge, không báo TChat.")
        return
    if not mr_iid:
        tg_api.send_message(chat, "⚠️ Đã duyệt nhưng không rõ số MR để merge tự động — merge tay.",
                            bot=appr_bot)
        return
    res = _gitlab_merge(mr_iid)
    if res.get("ok"):
        tg_api.send_message(chat, f"✅ Đã merge {mr_label}.", bot=appr_bot)
        _post_tagged(gid, group_type, sender_name, sender_id, f"✅ {mr_label} đã được merge.")
    else:
        tg_api.send_message(chat, f"⚠️ Merge {mr_label} lỗi: {res.get('error')}", bot=appr_bot)
        _post_tagged(gid, group_type, sender_name, sender_id,
                     f"⚠️ {mr_label} đã duyệt nhưng merge LỖI: {res.get('error')} — cần merge tay.")


def _handle(obj, gid_target, me, kws, seen):
    data = obj.get("data") or {}
    if data.get("type") != "TEXT":
        return
    gid = data.get("groupId")
    if gid != gid_target:
        return
    sender = data.get("senderId")
    if not sender:
        return
    inc = data.get("messageIdInc")
    key = (gid, inc)
    if key in seen:
        return
    seen.add(key)

    content = data.get("content")
    if crypto and listen._looks_encrypted(content):
        content = crypto.decrypt_if_needed(content)   # E2E → plaintext nếu có key
    if listen._looks_encrypted(content):              # vẫn ciphertext (không key/lỗi) → bỏ
        print(f"[{_now()}] [SKIP] tin mã hoá E2E (không giải được) — bỏ qua.")
        return

    # Tin của CHÍNH chủ tài khoản: chỉ xử lý khi có TIỀN TỐ lệnh (vd "@bot review
    # MR 412"). Tin bot tự đăng (ack/kết quả) KHÔNG có tiền tố → bỏ qua, chống loop.
    is_command = False
    if sender == me:
        cmd = _strip_self_prefix(content)
        if cmd is None:
            return  # tin tự đăng / chat thường của mình
        content = cmd
        is_command = True  # lệnh tường minh của chủ → bỏ qua prefilter từ khoá

    group = data.get("group") or {}
    group_type = group.get("type") or group.get("groupType") or "SUPER_PRIVATE"
    sender_name = (data.get("user") or {}).get("displayName") or sender

    if not is_command and not _looks_like_request(content, kws):
        print(f"[{_now()}] [skip] {sender_name}: {content!r}  (không khớp từ khoá)")
        return
    # XẾP HÀNG thay vì spawn-rồi-bỏ: tin tới khi đang bận vẫn được xử lý sau.
    _work_q.put((gid, group_type, sender_name, sender, content, me))
    tag = "[CMD]" if is_command else "[queued]"
    print(f"[{_now()}] {tag} {sender_name}: {content!r}  (chờ xử lý: {_work_q.qsize()})")


def run(gid_target, idle_ping=20):
    me = client.api_get("/user/me").get("id")
    if not me:
        _hlog("fatal", "cannot resolve current user (token bad?)")
        print("[ERROR] không xác định được user hiện tại (token hỏng?)", file=sys.stderr)
        sys.exit(1)
    _hlog("started", f"group={gid_target}")
    kws = _keywords()
    label, _ = _primary_account()
    n_workers = _worker_count()
    print(f"[{_now()}] watching group {gid_target} as {me} | account={label} | "
          f"workers={n_workers} | từ khoá={kws} | Ctrl+C để dừng")
    # Pool worker: chạy SONG SONG tối đa n_workers tác vụ (mỗi worker rút 1 tin khỏi
    # hàng đợi rồi chạy claude -p). queue.Queue thread-safe → không sót, không trùng.
    for i in range(n_workers):
        threading.Thread(target=_worker, name=f"worker-{i+1}", daemon=True).start()
    seen = set()
    backoff = 2
    while True:
        try:
            ws = ws_client.WebSocket(config.ws_url(), subprotocols=[tokens.ensure_fresh()],
                                     origin=config.web_origin(), timeout=idle_ping,
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
                    print(f"[{_now()}] socket đóng; kết nối lại…")
                    break
                if msg in ("pong", "ping"):
                    continue
                try:
                    o = json.loads(msg) if isinstance(msg, str) else None
                except Exception:
                    o = None
                if isinstance(o, dict) and o.get("type") == "message":
                    _handle(o, gid_target, me, kws, seen)
                if time.time() - last_ping > idle_ping:
                    ws.send_text("ping")
                    last_ping = time.time()
            ws.close()
        except KeyboardInterrupt:
            print(f"\n[{_now()}] stopped.")
            return
        except Exception as e:  # noqa: BLE001
            _hlog("transient", f"reconnect in {backoff}s — {e}")
            print(f"[{_now()}] lỗi kết nối: {e}; thử lại sau {backoff}s", file=sys.stderr)
            tokens.refresh(verbose=False)
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)


if __name__ == "__main__":
    missing = config.validate()
    if missing:
        print(f"[ERROR] thiếu config tchat: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)
    p = argparse.ArgumentParser(prog="group_watch.py")
    p.add_argument("--group", dest="group", default=rccfg.get("FCHAT_WATCH_GROUP"),
                   help="id group cần theo dõi (mặc định FCHAT_WATCH_GROUP)")
    p.add_argument("--once", dest="once", default=None,
                   help="test: xử lý đúng một câu lệnh rồi thoát (không mở WS)")
    a = p.parse_args()
    if not a.group:
        print("[ERROR] chưa có group: đặt FCHAT_WATCH_GROUP trong .env hoặc truyền --group",
              file=sys.stderr)
        sys.exit(1)
    if a.once is not None:
        me = client.api_get("/user/me").get("id")
        _process_one(a.group, "SUPER_PRIVATE", "test", "", a.once, me)
        sys.exit(0)
    try:
        run(a.group)
    except KeyboardInterrupt:
        print("\nstopped.")
