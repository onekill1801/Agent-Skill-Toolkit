# aTask: Run — LUỒNG THỰC THI: chỉ code, từ task ĐÃ DUYỆT solution

Luồng 2 trong 2 luồng tách biệt (luồng 1 = `/atask-prep` — bổ sung thông tin + duyệt
plan). Vào đây nghĩa là: item đã `approved`, plan + kịch bản verify **đã được người
duyệt** — chỉ còn: checkout → code theo plan → test → chạy thật + soi DB → merge.
Chạy TUẦN TỰ (flow lock `task_resolver`), vì thế **không cần người trong lúc chạy** —
mọi quyết định đã chốt ở prep; kẹt thì PARK + báo Telegram, không chờ.

Tham số: `$ARGUMENTS` = task_id (+ "batch" khi do `queue_worker.py` spawn).
`<RID>` = qid = `atask-<task_id>`. Plan/verify lấy từ `task_queue.py show <qid>`
→ `artifacts.plan` / `artifacts.verify` (KHÔNG tự lập plan mới — chưa duyệt = quay về prep).

## Quy trình

1. **Claim**: `task_queue.py next` (nếu worker chưa claim sẵn) — item phải là chính
   task này và `approved`; `mark <qid> --to processing` [đã opt-in khi approve].
2. **Đồng bộ nhánh gốc** (`<TB>` = `project_config.target_branch(<P>,<env>)`):
   ```bash
   git -C <clone_dir> checkout <TB> && git -C <clone_dir> pull --ff-only origin <TB>
   # pull fail vì gốc local đã chứa merge các task trước -> bỏ pull, đi tiếp từ gốc local
   git -C <clone_dir> checkout -b <type>/<task_id>-<slug> <TB>
   ```
3. **Implement**: `run_log.py stage <RID> implement active` → `grounding.py run --run <RID>
   --root <clone_dir>` + `record-gate grounding` (map tên file generic trong plan sang file
   thật) → viết code đúng theo plan ĐÃ DUYỆT + unit test chứng minh → commit →
   `advance implement`.
4. **Test + Verify**: `test_runner` test/lint + `record-gate` →
   `fix_loop.py run --run <RID> --project <P> --kind verify` (scenario = `artifacts.verify`;
   app chạy bằng `app_run_cmd`; đỏ → tự sửa theo mode của run; run prep đặt
   `mode=checkpoint` thì diff chờ duyệt qua `tg_gate --gate fix_diff`, batch/auto thì tự áp
   + gửi diff xem sau) → `record-gate verify --json` → `ac-map --verify-json` →
   `advance test`.
5. **Merge LOCAL vào nhánh gốc** (KHÔNG push, KHÔNG MR — người quyết cho cả đợt sau):
   ```bash
   git -C <clone_dir> checkout <TB>
   git -C <clone_dir> merge --no-ff <type>/<task_id>-<slug> -m "task <task_id>: <title>"
   # conflict (hiếm — tuần tự từ gốc mới): merge --abort -> PARK + báo Telegram
   ```
6. **Đóng**: comment lên aTask "đã code + verify xanh, chờ push/MR đợt" →
   `task_queue.py done <qid> --result ok` (nhả lock) → Telegram ✅ 1 dòng.
   Task kế (worker tự lấy) sẽ checkout từ `<TB>` **đã chứa code task này**.

## PARK (không chờ, không kẹt hàng)

fix_loop đỏ 3 lần · verify không xanh · merge conflict · app không boot · token hết hạn
→ `task_queue.py done <qid> --result fail --note "<lý do>"` + Telegram báo (không chờ
trả lời) → worker chạy task kế. Người xem lại → `requeue` (quay về `approved` vì plan
đã duyệt) hoặc sửa tay.

## Ghi chú

- Chưa `approved` mà bị gọi → dừng, chỉ về `/atask-prep`. Đứt phiên → `run_log.py get <RID>`.
- macOS/Linux dùng `python3`.
