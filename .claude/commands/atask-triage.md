# aTask: Triage → Assign or Execute

Đọc một task aTask (hoặc quét task của tôi), **phân tích**, rồi **đề xuất** một
trong hai: giao người (assign) hoặc thực hiện tự động (auto-dev). Người dùng duyệt
trước khi làm bất kỳ thao tác `[WRITE]` nào.

Tham số: `$ARGUMENTS` — một `task_id` cụ thể, hoặc rỗng = chọn từ `my-tasks`.

## Quy trình

1. Đọc @.claude/skills/atask-automation/SKILL.md và @.claude/skills/auto-dev/SKILL.md.
2. **Lấy task:**
   - Có id → `cd .claude/skills/atask-automation/tools && python tasks.py get <id>`.
   - Rỗng → `python search.py my-tasks --format summary`, hỏi người dùng chọn task
     (hoặc gợi ý task chưa hoàn thành, sắp đến hạn).
3. **Phân tích** (read-only): tóm tắt việc cần làm, acceptance, file/khu vực ảnh
   hưởng, độ rõ ràng. Phân loại độ khó với
   `cd .claude/skills/auto-dev/tools && python triage.py classify --title "..." --desc "..."`.
4. **Đề xuất** rõ ràng MỘT hướng (kèm lý do, độ khó):
   - **EXECUTE** — task đủ rõ, là việc code tự động được → chạy auto-dev.
   - **ASSIGN** — cần con người (mơ hồ, quyết định nghiệp vụ, ngoài phạm vi tự động)
     → gợi ý người/role phù hợp.
5. **Hỏi duyệt** người dùng (đừng tự làm). Khi được duyệt:
   - EXECUTE → chạy `/auto-dev <task_id>` (full pipeline, có checkpoint của nó).
   - ASSIGN → báo người đề xuất + lý do. Gán người tự động được: `tasks.py assign-users
     <task> <userId> [--mode add]` (tool aTask `assign_task_users`); cần `atask_id` của người
     trong `team.json`. Hoặc gán sprint: `tasks.py assign-sprint <task> <sprint>`.
     Luồng đóng-task/giao-người đầy đủ (xác minh code) → dùng `/atask-resolve`.

## Ghi chú

- macOS/Linux dùng `python3`.
- Mọi thay đổi task/tạo MR/notify là `[WRITE]` → confirm trước (guardrail CLAUDE.md).
- Bản tự động không người trực: dùng `python atask_watch.py` (poll + duyệt qua Telegram).
