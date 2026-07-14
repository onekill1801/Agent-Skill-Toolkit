# aTask: Prep — LUỒNG CHUẨN BỊ: bổ sung thông tin + duyệt solution (chưa code)

Luồng 1 trong 2 luồng tách biệt: làm giàu task → làm rõ với người → lập plan có phản
biện → sinh kịch bản verify → **người duyệt solution qua Telegram** → item sang
`approved`, vào hàng THỰC THI (luồng 2 = `/atask-run`, worker tự chạy).
Prep **không đụng code/repo** (chỉ đọc) → chạy được **nhiều task song song**, không cần lock.

Tham số: `$ARGUMENTS` = task_id (một hoặc nhiều, cách nhau khoảng trắng); trống =
`task_queue.py list --state ready` + `scan` để chọn.

Tool: `.claude/skills/auto-dev/tools/` + `.claude/skills/dev-automation/tools/`.
`<RID>` = `atask-<task_id>` = qid.

## Quy trình cho MỖI task

1. **Intake**: `task_queue.py intake --source atask --task <id> --project <P> --backend claude`
   (đã trong queue thì dùng lại item — `show <qid>`). Cần `ATASK_BEARER_TOKEN` còn hạn
   để `get-detail` lấy đủ description.
2. **✋ Làm rõ (nếu `needs_clarification`)**: `task_queue.py ask-tg <qid>` → người trả
   lời tự do qua Telegram → `task_queue.py reply <qid> --text "<nguyên văn>"`
   (tự sync brief lên aTask). → `ready`.
3. **Plan**: triage (`--clarity` từ intake) → `run_log.py init <RID> --tier <t> --mode checkpoint`
   → `ac-add` từ `ac_seeds` + AC rút từ description → `record-gate clarity` →
   debate (`--desc-file <brief|pack>` — **kèm nội dung scout** để plan neo tên file thật,
   `--corrections-file` nếu recall có) → `plan.xml`.
4. **Kịch bản verify**: `verify_gen.py run --run <RID> --plan <plan.xml> --root <clone_dir>
   --context-file <scout>` → `touches_runtime` → `run_log.py require <RID> verify`.
5. **✋ Duyệt solution**: `tg_gate.py send --run <RID> --gate after_plan --title "..."`
   (mục: plan tóm tắt · kịch bản verify + AC coverage · từng `needs_review`/`endpoints_untested`)
   → `tg_gate.py wait --run <RID> --gate after_plan` → mục có comment = CHỈNH plan/scenario
   + `feedback.py add --action edited`; approved hết →
   `run_log.py checkpoint <RID> after_plan approved` và:
   ```bash
   python task_queue.py approve <qid> --plan <plan.xml> --verify temp/runs/<RID>_verify.json
   ```
   → item `approved` = **nằm trong hàng thực thi**, hết việc của prep.

## Resume (autopilot gọi lại / người trả lời trễ)

Chạy lại prep cho task đã dở dang: **DÙNG LẠI artifact, đừng làm lại từ đầu** —
`temp/runs/<task_id>_plan.xml` đã có → bỏ qua debate; `<RID>_gate_after_plan.json` đã
gửi → chỉ `tg_gate.py wait` (thêm `--poll-updates` nếu telegram_bridge không chạy);
câu hỏi clarify đã gửi (`questions_sent_tg`) → chỉ chờ `reply`. Hết giờ chờ → để
nguyên trạng thái item rồi thoát (autopilot sẽ nhắc người và quay lại sau).

## Ghi chú

- Nhiều task: lặp bước 1-5 từng task (hoặc gửi gộp các gate lên Telegram để người duyệt một lượt).
- Batch từ resolver: `python task_resolver.py --enqueue` chỉ làm bước 1 hàng loạt —
  vẫn phải qua prep (2-5) trước khi task được thực thi.
- macOS/Linux dùng `python3`. Mọi [WRITE] lên aTask (sync brief, post questions) theo guardrail.
