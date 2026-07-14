# aTask: Queue — nạp task, làm rõ yêu cầu, xử lý TUẦN TỰ

Đọc task từ aTask → làm giàu + làm rõ yêu cầu (context pack / scout / clarify) →
**cập nhật ngược brief lên task aTask** (đủ ngữ cảnh để bàn giao người khác) → xếp
hàng đợi → luồng `task_resolver` xử lý **một task tại một thời điểm** (lock theo
luồng, tránh xung đột code khi nhiều task tự động chạy cùng repo; người làm tay
KHÔNG bị chặn).

Tham số: `$ARGUMENTS` — trống = xem queue + quét task mới; `scan` / `next` / một
`task_id` để intake; `answer <qid>` để chốt câu hỏi.

Tool: `.claude/skills/auto-dev/tools/task_queue.py` (chi tiết luồng:
@.claude/skills/auto-dev/cookbook/intake.md mục "Queue").

## Quy trình

1. **Xem trạng thái:** `cd .claude/skills/auto-dev/tools && python task_queue.py list`
   — trình bảng: qid · state · priority · title; nêu rõ item đang giữ lock (nếu có).
2. **Quét task mới:** `python task_queue.py scan --limit 10` (read-only) → liệt kê
   task aTask của tôi chưa vào queue. Người dùng chọn → intake từng cái:
   `python task_queue.py intake --source atask --task <id> --project <P> --type <t> --backend claude`
   - `--project` = tên trong `work/projects.json` (bật scout + recall). `--backend claude`
     cho câu hỏi sát + `proposed` (bỏ = heuristic, không tốn token).
   - `--post-questions` là `[WRITE]` (comment lên aTask, team thấy) → **hỏi trước**.
3. **Làm rõ + cập nhật ngược (kênh chính: TELEGRAM, trả lời TỰ DO):**
   item `needs_clarification` → `python task_queue.py ask-tg <qid>` gửi danh sách mục
   đánh số (❗ = blocking) kèm đề xuất qua Telegram — **người dùng trả lời bằng comment
   tự do**, KHÔNG phải nút chọn, dạng: `<qid> 1: ok; 2: dùng 409 thay vì 400; 3: chỉ parent`.
   Nhận được tin nhắn đó (qua bridge hoặc dán vào phiên) →
   `python task_queue.py reply <qid> --text "<nguyên văn>"` — mục 'ok'/bỏ qua = nhận đề
   xuất, text tự do = câu chốt; tự gấp thành brief + **comment lên task aTask** (`[WRITE]`,
   `--no-sync` để bỏ) + báo xác nhận ngược về Telegram.
   Ngồi tại terminal thì vẫn dùng được `answer <qid> --answers-file|--accept-proposed`.
   Task đã đủ ngữ cảnh → muốn giao người khác thì dùng `/atask-triage` (assign-users).
4. **HAI LUỒNG TÁCH BIỆT:** `ready` (đủ thông tin) **chưa được thực thi** — phải qua
   **/atask-prep** (plan + verify + người duyệt qua Telegram) → `task_queue.py approve <qid>`
   → `approved`. Luồng thực thi (**/atask-run** hoặc `queue_worker.py run`) dùng
   `python task_queue.py next` — CHỈ nhặt `approved` (lock owner `task_resolver`,
   tuần tự). Bắt đầu code → `mark <qid> --to processing` `[WRITE]`; xong đợt có MR →
   `mark <qid> --to approved --comment "MR: <url>"` theo workflow.
   Chạy pipeline @.claude/skills/auto-dev/cookbook/pipeline.md với ngữ cảnh có sẵn
   trong item: `artifacts.brief`/`.pack` làm `--desc-file`, `artifacts.corrections`
   cho debate, `ac_seeds` → `run_log.py ac-add`, `run_id` như JSON gợi ý.
5. **Kết thúc:** `python task_queue.py done <qid> --result ok|fail --note "..."` —
   nhả lock, task kế tiếp mới lấy được. Fail muốn làm lại: `requeue <qid>`.

## Ghi chú

- macOS/Linux dùng `python3`.
- Lock theo LUỒNG (owner), không toàn cục: daemon `task_resolver.py` và `next` mặc định
  dùng chung owner `task_resolver` → không bao giờ 2 task tự động chạy cùng lúc; luồng
  khác truyền `--owner` riêng. Lock kẹt (phiên chết giữa chừng) →
  `python task_queue.py release [--owner ...]` rồi `next` lại.
- Task mô tả rỗng + không comment/checklist bị ép `needs_clarification` (thin guard)
  — đừng ép `--ready`, hãy hỏi người tạo task.
- `remove <qid>` là thao tác xoá → confirm trước (guardrail CLAUDE.md).
