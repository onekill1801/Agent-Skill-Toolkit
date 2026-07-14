---
name: TChat Automation
description: >
  Read-only automation over TChat (internal corporate messenger) via its REST
  API (base URL in `FCHAT_BASE_API_URL`): list conversations, read message history & shared
  media, search the user directory, browse todos, check session. Auth is a
  session Bearer token + x-app/x-lang/x-request-id headers.
  Trigger phrases: "tchat", "list my conversations", "đọc tin nhắn tchat",
  "fchat conversations", "tchat todos", "search tchat user", "my tchat groups".
---

# TChat Automation

## Purpose

Query TChat from the agent: conversations, participants, message history,
shared files/links/media, todos, and the user directory. **Read-only** — this
skill does not send messages or mutate data.

> Reverse-engineered from an authenticated traffic capture (chrome://net-export)
> + public JS fingerprinting. Endpoint paths/methods/params are verified;
> response **body shapes are not guaranteed** — treat tool output as raw server JSON.

## Scope & hard limits (read first)

- **Read + two verified writes:** `todos.py create` (REST) and `send.py text`
  (send a chat message over the SocketCluster realtime socket). Both are gated
  behind `--yes` and dry-run by default. Other create/update/delete is unwired.
- **Confirm before writing:** only pass `--yes` after the user explicitly
  approves (repo `[WRITE]` guardrail). `send.py` posts a real message people see.
- **E2E limit:** `send.py` sends `content` as PLAINTEXT — correct only for
  NON-secure conversations. Secure groups encrypt content (beatchat); that crypto
  is NOT implemented, so do not send into secure/encrypted groups.
- **Message content is END-TO-END ENCRYPTED** (beatchat, RSA-OAEP-2048/SHA-256).
  `crypto.py` giải mã được KHI có private key của bạn (`work/secrets/fchat_private.pem`,
  gitignored). `messages.py list`, `listen`, `group_watch` **tự giải mã** nếu key có
  mặt; vắng key → trả ciphertext như cũ. Xem mục "Giải mã E2E" bên dưới.
- **Sending messages is NOT here** — it rides a SocketCluster realtime socket
  (`FCHAT_WS_URL`), which this REST skill does not touch.
- **Authorization:** TChat is an internal E2E messenger. Use this on your own
  account; clear any broader/automated use with the TChat team.

## Configuration (.env at repo root)

| Key | Purpose |
|---|---|
| `FCHAT_BASE_API_URL` | REST base (chat API host — required) |
| `FCHAT_WEB_ORIGIN` | Origin/Referer of the chat web app (required) |
| `FCHAT_BEARER_TOKEN` | Session JWT (secret). Copy from a logged-in chat web tab → DevTools → any API request → Request Headers → `authorization: Bearer …` |
| `FCHAT_X_APP` | Value of the `x-app` request header (copy from the same request) |
| `FCHAT_LANG` | `vi` or `en` (default `vi`) |
| `FCHAT_VERIFY_SSL` | `true` default; `false` only for internal certs (warns) |

The token is short-lived: on `401` the tools tell you to refresh it.

## Available tools (`.claude/skills/tchat-automation/tools/`)

`cd` into `tools/` first. Windows: `python` · macOS/Linux: `python3`.

```
python config.py                                  # validate .env
python auth.py whoami                             # verify token (GET /user/me)

python users.py me | setting | server-key
python users.py search [--q TEXT] [--group ID] [--limit N] [--page N]   # directory (GET)
python users.py lookup --ids ID1,ID2                                    # batch resolve (POST)

python groups.py list [--limit N] [--before ISO] [--filter DIRECT_CHAT] # ISO = latestMessageAt cursor
python groups.py search [--q TEXT] [--limit N] [--page N]               # global conversation search
python groups.py get <group_id>
python groups.py participants <group_id> [--limit N] [--page N]
python groups.py setting <group_id>
python groups.py folders

python messages.py list <group_id> [--limit N]    # bodies are E2E ciphertext
python messages.py scheduled <group_id>
python messages.py media <group_id> --type MEDIA|FILE|LINK|VOICE [--limit N]
python messages.py marked [--status UNDONE] [--limit N]
python messages.py count-marked [--status UNDONE]

python todos.py list [--type BY_ME|TO_ME|IMPORTANT] [--group ID] [--filter EXPIRED]
python todos.py count-expired
python todos.py create --group ID --title T [--detail D] [--due ISO] [--important] [--yes]  # [WRITE] dry-run without --yes
python todos.py delete --id TODO_ID [--yes]                                  # [WRITE] delete a todo; dry-run without --yes

python send.py text   --group ID --content "..." [--group-type TYPE] [--yes] # [WRITE] send message over WS; dry-run without --yes
python send.py recall --group ID --inc MESSAGE_ID_INC [--yes]                # [WRITE] recall (delete for everyone); dry-run without --yes
python notify_group.py --group ID --text "..." [--group-type T] [--user-id UID --user-name "Tên"]  # [WRITE] đăng tin (tự tag user) vào group; cho tool skill khác gọi (vd bg_notify báo build xong)

python listen.py [--reply claude|notify|off]   # long-running: route incoming msgs; DM -> per-conversation worker terminal
python reply_worker.py <group_id>              # (auto-launched by listen.py) per-conversation auto-reply worker
python group_watch.py [--group ID] [--once "text"]   # long-running: watch ONE group -> review MR / build dev qua claude -p
python task_digest.py [--interval 10] [--no-telegram] [--atask [--atask-list ID]] [--test "text"]   # long-running: nghe DM+nhóm -> trích việc cần làm -> digest.md + Telegram (+aTask nếu bật) (KHÔNG trả lời)
python auth.py refresh | token-status                                        # token mgmt (auto-refresh is automatic)

python style_profile.py list | path <gid> | get <gid>                        # kho "giọng nhắn" theo từng hội thoại (gitignored)
python style_profile.py gather <gid> [--limit N]                             # raw material đọc được để phân tích giọng
python style_profile.py save <gid> --file F | --stdin [--name "Tên"]         # ghi profile (agent điền sau khi phân tích)
python style_profile.py template [--gid G] [--name N]                        # khung profile trống
```

Realtime send uses `ws_client.py` (a minimal stdlib WebSocket client) against
`FCHAT_WS_URL` (set in .env), authenticating
with the JWT as `Sec-WebSocket-Protocol`.

```

python common.py settings | notification-setting | stickers
```

All tools print JSON; errors print `{"error": true, "status": ..., "message": ...}`
to stdout and exit non-zero — read and handle, don't treat as a crash.

## Listener + auto-reply (one terminal per conversation)

`listen.py` keeps a realtime WS connection and routes incoming messages:
- **Direct (1-1) from others** → appended to a per-conversation queue
  (`temp/fchat_incoming/queue_<gid>.jsonl`). If no worker is alive for that
  conversation, it opens **one terminal** running `reply_worker.py <gid>`;
  otherwise the already-open worker picks the message up. So each person/group =
  **one terminal**, reused for the whole back-and-forth.
- **Group messages** → logged only, never auto-replied.
- Own messages / typing / seen → ignored (`senderId == me` prevents reply loops).

`reply_worker.py` (per conversation, in its own terminal):
- **Debounce:** after a message it waits `FCHAT_REPLY_DEBOUNCE` seconds (default 10)
  of silence so the other person can finish; consecutive messages are then answered
  with **one combined reply**.
- Fetches the **conversation history** (labelled `[Tôi]` vs the other person) and
  asks `claude -p --model sonnet` (Pro subscription, no API key) to draft a reply
  **in your style**.
- Shows the draft and asks **`Gửi? [y/N]`** — sends via `send.py` only on `y`.
- E2E-encrypted messages are flagged unreadable and skipped.
- Exits & cleans up **15 minutes** after the last message (lock + queue removed).

Listener modes: `--reply claude` (default), `--reply notify` (print only),
`--reply off` (log only). Reply model: env `FCHAT_REPLY_MODEL` (default `sonnet`).
Heartbeat + auto-reconnect + token refresh are built in. Stop with Ctrl+C.

## Giải mã E2E (`crypto.py`)

Đọc được hội thoại **mã hoá** (cờ `isSecure: true`) bằng private key của chính bạn.
Cần `cryptography` (pip/pacman). Scheme (đã xác minh từ web client beatchat):

- Nội dung tin = base64. Dài = 256B → **RSA-OAEP-SHA256** decrypt trực tiếp; dài hơn →
  256B đầu RSA-OAEP ra `[aesKey32][iv12]`, phần sau = **AES-GCM** (hybrid).
- Private key gói trong IndexedDB `BeatchatDB > user_keys[<user>_private_CVX]`: lớp
  **AES-GCM**, password = `clientKey` (`/user/me`); PBKDF2-HMAC-SHA256, 300k vòng;
  blob `[iv12][ct‖tag][salt16]`. Plaintext = base64(pkcs8). (Blob QR-login cũng mở
  được bằng secretkey: `unwrap-qr`, bỏ 21 byte đầu.)

**Dựng key (một lần):** lấy giá trị IndexedDB `<user>_private_CVX` từ Console trình
duyệt (đang đăng nhập web chat), rồi:
```
python crypto.py unwrap-indexeddb --value '<base64>' --client-key <clientKey> --save
python crypto.py conv <group_id>          # kiểm chứng: giải mã lịch sử 1 hội thoại
python crypto.py decrypt --content '<base64>'   # giải mã 1 content
```
Key lưu `work/secrets/fchat_private.pem` (gitignored). Sau đó `messages.py list`,
`listen`, `group_watch` tự giải mã. Tắt: `messages.py list --no-decrypt`.

> ⚠️ Chỉ dùng trên tài khoản của CHÍNH bạn, key của chính bạn (được uỷ quyền).
> KHÔNG commit key/PEM. `looks_encrypted()` nhận diện ciphertext để chỉ giải khi cần.

## Group watcher → review MR / build dev (`group_watch.py`)

Theo dõi **MỘT group** (vd "New Group") để lái việc review code & build lên dev:
- Lắng nghe group qua socket realtime (như `listen.py` nhưng chỉ 1 group, không
  auto-reply chuyện phiếm). Tin khớp **prefilter từ khoá** (`FCHAT_WATCH_KEYWORDS`,
  mặc định mr/review/build/deploy/lên dev/…) mới được đưa cho agent.
- Mỗi tin khớp → **báo nhận ngay vào group** ("đang review…" / "đang build…")
  rồi spawn `claude -p` headless ở gốc repo (full agent: dev-automation, gitlab,
  jenkins). Agent tự trả `SKIP` nếu tin thực ra không phải yêu cầu — khi đó tin
  báo nhận được **thu hồi** để khỏi để lại rác.
- **Duyệt build:** agent chạy với `CLAUDE_TG_BRIDGE=1` + hook `telegram_approve.py`,
  nên build/[WRITE] hiện nút Duyệt/Từ chối trên Telegram. **Cần bridge daemon
  (`telegram_bridge.py`) đang chạy** để giao nút bấm về (watcher KHÔNG tự poll
  Telegram → không tranh getUpdates với bridge). Telegram giờ **chỉ** dùng cho nút
  duyệt — KHÔNG còn echo kết quả review/build sang Telegram.
- **Review → (chỉ APPROVE mới) duyệt merge → báo:** agent đăng nhận xét ĐẦY ĐỦ (theo
  template `code_review_prompt.md`: logic/architecture/performance/security/testing…) lên
  MR bằng `gitlab_api.py mr-comment`, dùng **badge emoji màu** (🟢/🟡/🔴 verdict, ✅/⚠️/❌
  compliance — KHÔNG dùng shields.io vì GitLab nội bộ không ra internet). Agent ghi
  `VERDICT: APPROVE|REQUEST_CHANGES|COMMENT` ở cuối câu trả lời. Tách bạch 2 việc:
  - **Kết quả review → LUÔN báo về TChat** (tag người yêu cầu), bất kể verdict.
  - **Quyết định merge → việc RIÊNG trên Telegram.** Chỉ verdict **APPROVE** mới **hỏi
    DUYỆT merge trên Telegram** (nút qua `approvals`, bridge daemon resolve). **Đồng ý**
    → `gitlab_api.py merge-mr <iid>` rồi báo thêm 1 tin "đã merge" về TChat. **Từ chối
    / Hết hạn** → KHÔNG merge và **IM LẶNG phía TChat** (kết quả review đã báo rồi).
  Chờ duyệt chạy ở **thread nền** → worker xử lý tin kế tiếp ngay.
- **Chạy song song:** pool `FCHAT_WATCH_WORKERS` worker (mặc định 3) rút từ cùng hàng đợi
  → nhiều review/build cùng lúc, không sót, không trùng. ⚠️ Vì build cũng song song, hai
  build cùng môi trường có thể đụng nhau (đặt `FCHAT_WATCH_WORKERS=1` để quay lại tuần tự).
- **Tự ra lệnh từ chính tài khoản:** tin của chính chủ (senderId == me) chỉ được xử lý
  khi mở đầu bằng **tiền tố `@bot `** (đè bằng `FCHAT_SELF_PREFIX`), vd `@bot review MR 412`
  → bỏ tiền tố, bỏ qua prefilter từ khoá, xử lý như yêu cầu. Tin bot tự đăng (ack/kết quả)
  KHÔNG có tiền tố → không tự kích hoạt → **chống loop vô hạn**.
- **Build chạy nền** (qua `bg_notify.py`): khi build XONG, kết quả ✅/❌ + thời lượng
  được đăng **về chính group TChat và tag người yêu cầu** (không chỉ Telegram). Cơ
  chế: `group_watch` truyền `FCHAT_NOTIFY_GROUP/_GROUP_TYPE/_USER_ID/_USER_NAME` qua env
  cho agent → `bg_notify` đọc env, gọi `notify_group.py` đăng về TChat. Build báo
  **cả hai kênh**: TChat (tag người yêu cầu) + Telegram (khép vòng sau khi bấm Duyệt).
  Telegram-bridge thuần không set env → chỉ báo Telegram như cũ.
- Tài khoản claude lấy từ `CLAUDE_ACCOUNTS` (mặc định `work`).

Config: `FCHAT_WATCH_GROUP` (id group), `FCHAT_WATCH_KEYWORDS`, `FCHAT_WATCH_TIMEOUT`,
`FCHAT_SELF_PREFIX` (mặc định `@bot`), `FCHAT_MERGE_APPROVE_TIMEOUT` (giây chờ duyệt merge,
mặc định 1800), `FCHAT_WATCH_WORKERS` (số tác vụ chạy SONG SONG, mặc định 3).
Test khô một câu: `python group_watch.py --once "review giúp MR 412"`.

> ⚠️ Group đích phải **non-secure (plaintext)** thì mới đọc lệnh & post lại được.
> Group secure/E2E → nội dung là ciphertext, hướng này không áp dụng.

## Task digest (lắng nghe → tổng hợp việc cần làm, KHÔNG trả lời) — `task_digest.py`

Một bridge **chỉ đọc** để chưng cất công việc của chủ tài khoản từ chat — không gửi
lại bất kỳ tin nào:
- Giữ kết nối WS realtime (cùng transport `listen.py`) và **buffer mọi tin TEXT đọc
  được** (DM + tất cả nhóm); bỏ tin của chính mình, tin non-TEXT, và tin **E2E mã hoá**
  (ciphertext → không đọc được, chỉ đếm rồi bỏ).
- Mỗi `--interval` phút (mặc định 10): gửi lô tin cho `claude -p` để **rút action item
  giao cho bạn** → ghi mục có ngày giờ vào `temp/fchat_tasks/digest.md` (danh sách việc
  dạng checkbox, có người nhờ + deadline + độ ưu tiên) và **đẩy tóm tắt việc MỚI sang
  Telegram** (bot của skill remote-control).
- Log thô mọi tin đọc được vào `temp/fchat_tasks/inbox.jsonl` (bền qua restart).
- **Tuỳ chọn tạo task trên aTask** (`--atask`) — **CÓ DUYỆT, không tự tạo**: mỗi lần
  flush, gửi **MỖI việc MỘT thẻ duyệt riêng (nút ✅ Duyệt / ❌ Từ chối) qua Telegram**;
  việc nào bạn bấm **Duyệt** thì tạo NGAY task đó (shell ra
  `atask-automation/tasks.py create`), việc bị **Từ chối / hết giờ** chỉ bỏ riêng việc
  đó — không ảnh hưởng việc khác. Tên = việc, ưu tiên map `cao→HIGH · vừa→MEDIUM ·
  thấp→LOW`, nguồn + deadline nhét vào mô tả. **Fail-safe:** không có bridge / chưa cấu
  hình chat duyệt → **bỏ qua, KHÔNG tạo**. Cần list đích: `--atask-list
  <ID>` hoặc env `FCHAT_DIGEST_ATASK_LIST`. Cơ chế duyệt dùng chung store
  `temp/tg_approvals/` với hook — **cần bridge remote-control đang chạy** để xử lý nút bấm
  (`TELEGRAM_APPROVAL_BOT`/`TELEGRAM_OPS_BOT`, `TELEGRAM_APPROVAL_CHAT`). **Thời gian chờ
  bấm duyệt** dùng `FCHAT_DIGEST_APPROVAL_TIMEOUT` (giây; rỗng = theo
  `TELEGRAM_APPROVAL_TIMEOUT` = 300s) — vd `86400` = 24h mới auto-huỷ. Việc chờ duyệt do
  **một worker nền duy nhất** poll (số thread cố định = 1 dù nhiều thẻ chờ song song),
  nên **không chặn** luồng đọc tin nhắn lẫn luồng trích lô mới. Đổi giá trị phải
  **restart** `task_digest.py`. Không bật `--atask` → chỉ digest.md như cũ.
- **Bỏ qua tin từ nhóm tên "New Group"** (không buffer, không trích).

> ⚠️ **Giới hạn cứng:** chỉ trích được việc từ hội thoại **non-secure (plaintext)**.
> Nhóm/DM secure → nội dung mã hoá E2E, bridge chỉ thấy metadata, **không đọc được nội dung**.

Config thêm: `FCHAT_DIGEST_MODEL` (model cho `claude -p`, mặc định `sonnet`) ·
`FCHAT_DIGEST_ATASK_LIST` (list đích cho `--atask` khi không truyền `--atask-list`).
**Account:** spawn `claude -p` dưới account chính của `CLAUDE_ACCOUNTS` (mặc định `work`)
bằng cách set `HOME`/`USERPROFILE` về home của account đó — tránh kế thừa creds sai gây
401; tự fallback sang account kế tiếp khi gặp 401/quota (dùng lại logic `telegram_bridge`).
Telegram dùng `TELEGRAM_BOT_TOKEN` / `TELEGRAM_ALLOWED_CHATS` (của remote-control);
thiếu thì tự bỏ push, vẫn ghi `digest.md`. Test khô khâu trích:
`python task_digest.py --test "anh review giúp MR 412 trước trưa nay"`.

## Mention / tag người trong tin (`@`)

**[Đã xác minh từ tin thật]** TChat tag người bằng HAI phần song song:
- `content`: chèn chữ `@<DisplayName>` ngay trong nội dung.
- `metadata.mentions`: mảng `{userId, target, length, offset}` —
  `userId` = id người được tag (hoặc `"EVERYONE"` cho @All); `target` = tên hiển thị
  (không có `@`); `offset` = vị trí ký tự `@` trong content (đếm theo **code point**);
  `length` = độ dài chuỗi `@`+tên. Thông báo (ping) đến từ `userId`.

`send.py` hỗ trợ qua `send_text(..., metadata=...)` và helper
`send.with_mentions_prefix(body, [(display_name, user_id), ...])` → trả `(content, metadata)`
đã chèn tiền tố `@Tên` và tính sẵn offset/length đúng. `group_watch.py` dùng helper này để
**tag người tạo yêu cầu** khi đăng kết quả về group.

## Style profiles (giọng nhắn theo từng người)

Lưu cách chủ tài khoản nhắn với **từng hội thoại** để bản nháp/auto-reply soạn đúng giọng,
ổn định. Profile ở `profiles/<gid>.md` (**gitignored** — cá nhân, từ chat). `reply_worker.py`
tự nạp profile của hội thoại đó vào prompt (ưu tiên hơn lịch sử).

**Học/cập nhật giọng với 1 người (hybrid):**
1. `python style_profile.py gather <gid>` — lấy phần đọc được (tin '[Tôi]' = bạn gửi, là nguồn
   chính; tin E2E mã hoá bị bỏ qua).
2. Agent phân tích → điền template (`style_profile.py template`) → người dùng chỉnh.
3. `python style_profile.py save <gid> --file <profile.md>` (hoặc `--stdin`).
> ⚠️ Nội dung chat E2E mã hoá → KHÔNG tự đọc được; auto-learn chỉ từ plaintext + tin bạn gửi,
> phần còn lại điền tay.

## Workflows

1. **Browse conversations & history** → `cookbook/read-conversations.md`
   `groups.py list` → pick id → `groups.py get` / `participants` → `messages.py list` / `media`.
2. **Todos overview** → `cookbook/todos-overview.md`
   `todos.py list --type TO_ME` and `--filter EXPIRED`, `todos.py count-expired`.
3. **Headless QR login (best-effort)** → `cookbook/headless-login.md`
   Only if you can't copy a token; bodies unverified.

## Routing

| User intent | Tool |
|---|---|
| "list my chats / conversations", "nhóm của tôi" | `groups.py list` |
| "search conversations / find a group", "tìm nhóm" | `groups.py search --q` |
| "who's in <group>" | `groups.py participants` |
| "create a todo / task in <group>", "tạo việc" | `todos.py create` (confirm → `--yes`) |
| "delete task / xoá task" | `todos.py delete --id` (confirm → `--yes`) |
| "send a message to <group>", "gửi tin nhắn" | `send.py text` (confirm → `--yes`; non-secure only) |
| "recall / thu hồi tin nhắn" | `send.py recall --group --inc` (confirm → `--yes`) |
| "read messages in <group>", "đọc tin nhắn" | `messages.py list` |
| "lắng nghe chat → tổng hợp việc của tôi", "bridge tổng hợp công việc", "không cần trả lời" | `task_digest.py` |
| "học/lưu giọng nhắn với <người>", "phong cách nhắn tin" | `style_profile.py gather` → phân tích → `save` |
| "soạn/trả lời theo giọng của tôi" | `reply_worker` tự nạp `style_profile.py get <gid>` |
| "files/links/media in <group>" | `messages.py media --type ...` |
| "find user X", "tìm người dùng" | `users.py search --q X` |
| "my todos", "việc cần làm", "expired tasks" | `todos.py list` / `count-expired` |
| "am I logged in / who am I" | `auth.py whoami` |

## Slash command

- `/tchat` → list conversations and summarize unread/todos (see `.claude/commands/tchat.md`).

## Important rules

1. Confirm before any `--yes` write (`todos.py create`, `send.py text`). Dry-run first, show the user the frame/body, send only on explicit approval.
2. `send.py` is plaintext-only → never target a secure/encrypted conversation with it.
3. Respect rate limits — on `429`, back off (Retry-After is surfaced in the error).
4. Never hardcode the token; always via `config.py` / `.env`.
5. Don't log full message payloads to shared places — they may be sensitive even when encrypted.
