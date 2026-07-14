# Setup — Telegram bridge

## 1. Tạo bot
1. Telegram → chat với **@BotFather** → `/newbot` → đặt tên → nhận **token** dạng `123456:ABC...`.
2. Dán vào `.env`: `TELEGRAM_BOT_TOKEN=123456:ABC...`

## 2. Lấy chat_id của bạn
- Cách nhanh: chạy bridge tạm với allowlist rỗng sẽ chặn nhưng báo lại chat_id. Hoặc:
  - Nhắn 1 tin cho bot, rồi: `python tg_api.py me` (xác nhận token), tiếp đó khởi động bridge và
    gửi `/whoami` → bot trả `chat_id`.
- Điền: `TELEGRAM_ALLOWED_CHATS=123456789` (nhiều người: `111,222`).

> Allowlist **fail-closed**: rỗng = chặn tất cả. Chỉ chat_id trong danh sách mới điều khiển được.

## 3. Test wiring
```
cd .claude/skills/remote-control/tools
python tg_api.py test        # gửi tin "kết nối OK" tới mọi allowed chat
```

## 4. Chạy daemon (máy hub luôn-bật — máy Windows này)
```
python telegram_bridge.py
```
- Để cửa sổ chạy. Nhắn `/help` cho bot để xem hướng dẫn.
- Mỗi tin nhắn thường → `claude -p` headless trong repo root (full agent, mọi skill).
- Ngữ cảnh hội thoại giữ qua `--resume` (lưu session theo chat ở `temp/tg_sessions.json`).
  `/reset` để bắt đầu phiên mới.

### Chạy nền / tự bật khi mở máy (Windows)
- Nhanh: `Start-Process python -ArgumentList 'telegram_bridge.py' -WindowStyle Hidden` (PowerShell).
- Bền: tạo Task trong **Task Scheduler** → trigger "At log on" → action chạy
  `python <abs>\telegram_bridge.py`. (Tùy chọn nâng cao, làm khi bạn xác nhận.)

## 5. Dừng
Ctrl+C trong cửa sổ daemon.

## (Tùy chọn) Tách 2 bot: CODE + OPS

Mặc định mọi thứ (điều khiển code, thông báo, duyệt) chạy trên **một** bot. Có thể
tách thành 2 bot cho gọn kênh:

| Bot | Vai trò | Poll Telegram? |
|---|---|---|
| 🤖 **CODE** = `TELEGRAM_BOT_TOKEN` (bot mặc định) | nhắn → `claude -p` full agent | poller đầy đủ (`--mode full`) |
| 🛠️ **OPS** = `TELEGRAM_BOT_TOKEN_OPS` | monitor + thông báo + **nút Duyệt** | poller nhẹ (`--mode approvals-only`) |

**Vì sao 2 token:** Telegram chỉ cho **một** tiến trình `getUpdates` mỗi token (cái thứ
hai bị `409 Conflict`). Bot nào cần *nhận* (lệnh hoặc nút bấm) phải có poller riêng →
token riêng. Gửi thông báo một chiều thì KHÔNG cần token riêng (nhiều tiến trình gửi
chung một token thoải mái) — bot OPS gom luôn cả notify lẫn duyệt.

1. Tạo bot thứ 2 ở @BotFather (như mục 1), điền `.env`:
   ```
   TELEGRAM_OPS_BOT=ops                 # công tắc bật tách
   TELEGRAM_BOT_TOKEN_OPS=<token bot ops>
   TELEGRAM_ALLOWED_CHATS_OPS=<chat_id kênh ops>
   ```
   > `TELEGRAM_OPS_BOT` rỗng = tắt tách (mọi thứ về bot mặc định, đúng hành vi cũ).
   > Lấy `chat_id` kênh ops: nhắn cho bot ops rồi `python tg_api.py updates ops`,
   > hoặc khởi động poller ops và gửi `/whoami`.
2. Test riêng từng bot:
   ```
   python tg_api.py me ops      # xác nhận token bot ops
   python tg_api.py test ops    # ping chat ops
   ```
3. Chạy **2** daemon (mỗi bot một tiến trình):
   ```
   python telegram_bridge.py                                  # 🤖 CODE (full)
   python telegram_bridge.py --bot ops --mode approvals-only  # 🛠️ OPS (poller nhẹ)
   ```

**Khớp nối duyệt:** hook `telegram_approve.py` chạy *trong* agent của bot CODE nhưng
gửi thẻ duyệt qua token OPS và chờ **file quyết định** (`approvals.py`). Poller OPS
bắt nút → ghi file → hook đọc file. Vì vậy tách bot vẫn thông.

**Lưu ý chat group:** `chat_id` của *chat riêng tư* = user id, ổn định qua mọi bot.
Nhưng nếu kênh OPS là **group**, bot OPS phải được **add vào group đó** thì mới gửi
được (group id giống nhau qua các bot, nhưng bot phải là thành viên).

Muốn **3 bot** (tách notify khỏi duyệt): đặt thêm `TELEGRAM_NOTIFY_BOT` /
`TELEGRAM_APPROVAL_BOT` (rỗng = theo `TELEGRAM_OPS_BOT`), tạo token tương ứng, và
chạy poller `approvals-only` cho bot duyệt.

## Lưu ý vận hành
- Một lượt agent/chat chạy tuần tự: đang xử lý mà nhắn tiếp → bot báo "đang chạy".
- `claude -p` quá `TELEGRAM_AGENT_TIMEOUT` (mặc định 1800s) → hủy và báo lại.
- Output Telegram tự cắt 4000 ký tự/đoạn; HTML lỗi tag thì tự gửi lại dạng thường.
- `--settings` cấp file hook riêng cho agent bridge. Nếu bản Claude Code của bạn không nhận cờ này,
  xem `approval-flow.md` phần "Phương án dự phòng".  <!-- [Unverified] tên cờ tùy phiên bản CLI -->
