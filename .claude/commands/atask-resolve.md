# aTask: Resolve — verify-in-code → close or hand off

Luồng **poller xử-lý-task** cho task giao cho tôi ở **2 nhóm trạng thái `todo` (Chưa làm)
+ `processing` (Đang làm)**: đọc nội dung task, **xác minh trong code** (`work/<project>`)
đã fix chưa, rồi **đóng** (Hoàn thành / Chờ phê duyệt) hoặc **giao người** + chỉnh estimate.
Mọi thao tác `[WRITE]` đều **duyệt qua Telegram bằng nút bấm**.

Tham số: `$ARGUMENTS` — một `task_id` cụ thể (xử lý đúng 1 task rồi thoát), hoặc rỗng = chạy poller.

## Quy trình

1. Đọc @.claude/skills/atask-automation/SKILL.md (Workflow 8) và
   @.claude/skills/atask-automation/cookbook/task-resolve.md **TRƯỚC**.
2. Kiểm tra điều kiện: `.env` có cấu hình aTask + Telegram (`TELEGRAM_ALLOWED_CHATS`),
   `work/projects.json` map đúng project→`clone_dir`, `work/team.json` có `atask_id` cho
   người sẽ nhận giao việc. Bridge Telegram (ops/approval bot) đang chạy để nhận nút bấm.
3. **Chạy:**
   - Có id → `cd .claude/skills/atask-automation/tools && python task_resolver.py --task <id>`.
   - Rỗng → `python task_resolver.py` (baseline lần đầu, rồi poll mỗi 10').
     Một vòng rồi thoát: `--once`. Chỉ liệt kê không ghi: `--no-act`.
4. Với mỗi task, tool tự: phân tích (claude -p, chỉ đọc) → verdict `fixed|not_fixed|unclear`
   → gửi thẻ duyệt Telegram → khi DUYỆT mới ghi (đổi trạng thái / gán người / estimate / comment).

## Quyết định (tóm tắt)

| Verdict | Hành động (sau khi duyệt) |
|---|---|
| **fixed** | `✅ Hoàn thành` → `complete_task` · `🕓 Chờ phê duyệt` → `update_task status` |
| **not_fixed** | `👤 Tôi tự làm` → giữ tôi + chỉnh estimate + comment · `➡️ Giao người khác` → `✅ Giao <tên>` (`assign_task_users`) hoặc `❌ Bỏ qua` |
| **unclear** | chỉ báo Telegram, KHÔNG ghi |

## Ghi chú

- macOS/Linux dùng `python3`.
- Chống lấy trùng / gán nhiều người 1 task: STATE `temp/atask_resolved.json` (khoá theo
  task→statusType + `assigned_to`) + `_inflight` + kiểm assignee hiện có trước khi gán.
- ⚠️ Mã trạng thái "chờ phê duyệt" (`ATASK_RESOLVE_STATUS_REVIEW`) **[Unverified]** — đặt đúng
  theo workflow project của bạn trước khi dùng nhánh đó.
