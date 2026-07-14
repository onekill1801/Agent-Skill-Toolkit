---
name: remote-control
description: Điều khiển agent từ xa qua Telegram và fan-out lệnh ra các máy LAN qua SSH. Telegram bridge chạy headless `claude -p` (full agent, mọi skill) với chế độ DUYỆT bằng nút bấm cho thao tác ghi/SSH/nguy hiểm. Trigger phrases: "telegram bridge", "điều khiển qua telegram", "remote control", "ssh tới máy", "chạy lệnh trên máy LAN", "fan-out ssh", "start bridge", "khởi động bridge".
---

# Remote Control (Telegram + SSH)

Hai mặt phẳng:
- **Control plane — Telegram bridge**: nhắn tin → `claude -p` headless trong repo → trả kết quả. Full agent: mọi skill (auto-dev, atask, gitlab…) dùng được từ điện thoại.
- **Action plane — SSH fan-out**: chạy lệnh trên các máy LAN trong `work/hosts.json` (allowlist, key-based).

**Mô hình tin cậy: full agent + chế độ DUYỆT (cấu hình được).** Mặc định (`TELEGRAM_AUTO_APPROVE=read,file`):
đọc + **đọc/ghi file local → chạy tự do**; lệnh shell ghi (git push, restart, **SSH ra máy khác**, xóa)
→ đẩy nút **✅ Duyệt / ❌ Từ chối** qua Telegram, chờ bạn bấm; lệnh **nguy hiểm** (rm -rf, shutdown,
drop table…) → **LUÔN hỏi**, không bao giờ tự duyệt. Không phản hồi trong `TELEGRAM_APPROVAL_TIMEOUT`
(mặc định 300s) → tự **từ chối**. Muốn toàn quyền không hỏi: `TELEGRAM_AUTO_APPROVE=read,file,bash`.

## Cấu hình (.env)

```
TELEGRAM_BOT_TOKEN=123456:ABC...        # từ @BotFather
TELEGRAM_ALLOWED_CHATS=123456789        # chat_id được phép (cách nhau dấu phẩy). Trống = chặn tất.
TELEGRAM_APPROVAL_TIMEOUT=300           # giây chờ bấm nút trước khi auto-deny
TELEGRAM_AGENT_TIMEOUT=1800             # giây tối đa cho một lượt agent
TELEGRAM_AGENT_MODEL=                   # rỗng = model mặc định; vd 'sonnet' cho nhanh/rẻ
CLAUDE_BIN=                             # rỗng = 'claude' trên PATH
```

> Lấy `chat_id`: nhắn bot bất kỳ, rồi `python tg_api.py me` + xem update, hoặc khởi động bridge rồi
> gửi `/whoami`. Chat chưa có trong allowlist bị chặn và bridge báo lại chat_id để bạn thêm.

> **Tách 2 bot (tùy chọn):** một bot CODE (nhắn → agent) + một bot OPS (monitor + thông báo +
> nút Duyệt) cho gọn kênh. Đặt `TELEGRAM_OPS_BOT=ops` + `TELEGRAM_BOT_TOKEN_OPS` +
> `TELEGRAM_ALLOWED_CHATS_OPS`, rồi chạy thêm `python telegram_bridge.py --bot ops --mode
> approvals-only`. Rỗng = mọi thứ trên 1 bot (như cũ). Chi tiết: `cookbook/setup.md`.

## Workflow

### Khởi động bridge (trên máy hub luôn-bật)
1. Điền `.env` (token + allowed chats). Test kết nối: `python tg_api.py test`.
2. Chạy daemon: `python telegram_bridge.py` (Windows: `python`). Để cửa sổ chạy nền.
3. Nhắn `/help` cho bot → kiểm tra thông luồng. Xong: nhắn yêu cầu thật.
4. GUARDRAIL: lần đầu chạy, xác nhận với người dùng trước khi để bridge nhận lệnh ghi.
   Chi tiết: `cookbook/setup.md`.

### SSH fan-out
1. Tạo `work/hosts.json` (mẫu: `hosts.sample.json` trong skill này). Allowlist = chỉ host trong file.
2. Kiểm: `python ssh_exec.py list` · `python ssh_exec.py ping <alias>`.
3. Chạy: `python ssh_exec.py run <alias> "<lệnh>" [--dry-run]`. Phân loại rủi ro tự động
   (`read`/`write`/`danger`) — `danger` luôn cần duyệt khi đi qua bridge.
   Chi tiết: `cookbook/ssh-fanout.md`.

### Quản lý phiên (context) trong chat dài
Mỗi chat giữ một phiên `claude` liên tục (`--resume`) → ngữ cảnh tích lũy qua các lượt. Chat càng dài,
ngữ cảnh càng lớn (chậm/loãng/tốn token). Lệnh trong Telegram:
- `/new` (alias `/reset`, `/clear`) — **tạo phiên mới, xóa ngữ cảnh**, bắt đầu sạch.
- `/session` — xem phiên hiện tại: số lượt + chạy bao lâu + ID rút gọn.
- Khi phiên chạm `TELEGRAM_LONG_SESSION_TURNS` (mặc định 20) lượt, bridge **tự nhắc một lần** gợi ý `/new`.
- Phiên hỏng (resume thất bại) → bridge tự rớt về phiên mới và báo `♻️`.

### Nhiều tài khoản Claude + fallback (đa nền tảng)
Khai báo `CLAUDE_ACCOUNTS=work,personal` (ưu tiên trái→phải). Bridge chạy account đầu; gặp lỗi
**giới hạn/đăng nhập** (usage/rate limit, `/login`, auth…) → tự **nhảy account kế** và báo `⚠️`.
- **Đổi account = set biến home theo OS** cho tiến trình `claude`: `HOME` (macOS/Linux) + `USERPROFILE`
  (Windows) → claude đọc `<home>/.claude` tương ứng. Chạy như nhau trên Windows/Ubuntu/macOS.
- Mỗi label `X` → home mặc định `<base>/.claude-X`; override bằng `CLAUDE_HOME_X=<đường dẫn tuyệt đối>`.
- Chỉ giữ account có sẵn `.claude`. Máy thường (chỉ `~/.claude`) → tự dùng account `default`, không cần
  cấu hình gì.
- Phiên **gắn account** (sid account này không resume trên account khác) — đổi account thì bắt đầu phiên
  mới; các lượt sau giữ account đang dùng tới khi `/new`. `/session` cho biết account hiện tại.

> Tương đương trên Unix của `claude-work.cmd`/`claude-personal.cmd`: một script `HOME=$HOME/.claude-work claude …`.
> Nhưng bridge tự set biến home khi spawn nên **không cần** wrapper để fallback hoạt động.

### Form lựa chọn (plan/quyết định) → nút bấm
Agent headless không có giao diện chọn tương tác. Bridge dạy agent (qua `--append-system-prompt`)
phát một khối `[[TG_CHOICE]] … [[/TG_CHOICE]]` khi cần bạn quyết định; `choices.py` parse khối đó
→ render **nút inline bấm được**. Bấm nút → bridge **resume phiên** với phương án đã chọn (như bạn gõ tay).
- Một lượt chỉ một khối lựa chọn (tối đa 8 phương án). Bấm lại nút cũ không kích hoạt lại (idempotent).
- Không có khối → trả lời text bình thường.

### Tác vụ dài / chờ-nền (build, compile, test) → `bg_notify.py`
Mỗi tin nhắn = một phiên `claude -p` **headless one-shot**: agent kết thúc lượt là tiến
trình thoát NGAY. Vì vậy mẫu "spawn poll nền rồi hẹn báo sau" (vốn chạy được trong Claude
Code tương tác nhờ `<task-notification>`) sẽ **HỎNG dưới bridge**: tiến trình nền mồ côi,
không còn agent sống để báo về → người dùng không nhận được kết quả khi build/compile xong.

→ Với mọi tác vụ dài, agent phải dùng `dev-automation/tools/bg_notify.py`: nó tách rời khỏi
`claude -p` (Windows DETACHED_PROCESS / POSIX setsid), chạy lệnh tới khi xong rồi **tự gửi
kết quả** (✅/❌ + nhãn + thời lượng + đuôi log) thẳng về chat Telegram. Agent in `{"detached":
true,…}`, báo "đã chạy nền, sẽ nhắn khi xong" rồi kết thúc lượt — KHÔNG block, KHÔNG hẹn suông.
```
python bg_notify.py --label "Build dev atask" -- python jenkins.py build --project atask --env dev --wait
```
- Chat đích lấy từ env `CLAUDE_TG_CHAT_ID` (bridge set sẵn cho agent). Tự bật tách rời khi
  `CLAUDE_TG_BRIDGE=1`; chạy tay ngoài bridge thì mặc định đồng bộ (debug), không có chat → chỉ ghi log.
- Đoán SUCCESS/FAILURE từ JSON `passed`/`result`/`error` (không chỉ dựa exit code — `jenkins.py`
  trả exit 0 cả khi build FAILURE).

### Luồng duyệt (đọc trước khi sửa hook)
- PreToolUse hook `telegram_approve.py` CHỈ kích hoạt khi `CLAUDE_TG_BRIDGE=1` (bridge tự đặt khi
  spawn agent). Phiên Claude Code tương tác của bạn KHÔNG bị ảnh hưởng.
  Chi tiết: `cookbook/approval-flow.md`.

## Tools (`.claude/skills/remote-control/tools/`)

```
python tg_api.py me|test|send <chat> "text" [bot]  # Telegram Bot API (kiểm tra wiring; [bot]=tên bot)
python telegram_bridge.py [--bot ops] [--mode full|approvals-only] [--test]   # daemon (1/token)
python telegram_approve.py                          # PreToolUse hook (bridge gọi, không gọi tay)
python ssh_exec.py list|ping <a>|run <a> "<cmd>" [--dry-run]|classify "<cmd>"
```

## Guardrail (BẮT BUỘC)

1. **Allowlist là bắt buộc.** `TELEGRAM_ALLOWED_CHATS` rỗng → bridge chặn tất (fail-closed).
   `work/hosts.json` rỗng → không SSH được tới đâu.
2. **Lệnh shell ghi / SSH ra máy khác / nguy hiểm phải được DUYỆT** bằng nút Telegram (đọc & ghi file
   local thì tự chạy theo `TELEGRAM_AUTO_APPROVE`). Hook fail-safe: Telegram lỗi/timeout → **từ chối**.
   `danger` luôn hỏi dù cấu hình thế nào.
3. **Lệnh `danger`** (rm -rf, mkfs, shutdown, drop table…) bị gắn cờ ⚠️ trong thẻ duyệt — đọc kỹ
   trước khi bấm.
4. **Không hardcode token/host** — qua `.env` và `work/hosts.json`.
5. Bridge để máy hub trở thành cửa điều khiển từ xa — coi token Telegram như mật khẩu, đừng commit.
