# Intake — nhận yêu cầu vào pipeline

Hai nguồn đã chọn: **Azure DevOps** và **aTask**. Cộng nguồn **trực tiếp** (người dùng
mô tả yêu cầu / gõ `/auto-dev`). Đây là GĐ1 — intake **theo lệnh**, người dùng kích hoạt.
Poll tự động (cron) là GĐ2, ghi chú ở cuối.

## A. Trực tiếp

Người dùng mô tả yêu cầu hoặc gõ `/auto-dev <mô tả>`. Nếu không gắn với task hệ thống:
tự sinh `run_id` (vd `adhoc-<slug-ngắn>`), `--type` suy từ mô tả (bugfix/feature), không có `--task`.

## B. Azure DevOps

```bash
cd .claude/skills/dev-automation/tools
python azure_devops.py list            # task assigned @Me (chọn task để chạy)
python azure_devops.py get <task_id>   # đọc chi tiết một task
```
Map sang run-log:
- `--task` = work item id
- `--type` = `bugfix` nếu work item type = Bug, ngược lại `feature`
- `--project` = tên project trong registry khớp `azure_project` của work item

Đa project: prefix `AZURE_DEVOPS_PROJECT=<...>` mỗi lệnh.

## C. aTask

```bash
cd .claude/skills/atask-automation/tools
python search.py my-tasks              # task của tôi
python tasks.py get <task_id>          # chi tiết
```
Map: `--task` = aTask task id, `--type` suy từ nhãn/loại task, `--project` khớp registry.
> aTask không gắn trực tiếp với GitLab repo → khi tạo branch/MR vẫn dùng `gitlab_api.py`
> với `GITLAB_PROJECT_ID` của project tương ứng trong `./work/projects.json`.

## Context pack — gom ngữ cảnh đã có (TRƯỚC clarify)

Task trên aTask/Azure hay mô tả sơ sài, nhưng yêu cầu thật thường nằm ở **comment / checklist /
subtask / task cha** (aTask) hoặc **acceptance criteria / root cause / solution** (Azure). Đọc mỗi
`description` = tự bỏ đói. Gom hết vào một *context pack* rồi dùng nó làm `--desc` cho clarify + debate:

```bash
cd .claude/skills/auto-dev/tools
python context_pack.py build --source <atask|azure> --task <task_id>
#   -> temp/runs/<src>-<id>_context.md  + JSON {ac_seeds, signals}
#   [--no-comments|--no-checklist|--no-subtasks]  bỏ bớt fetch nếu chậm/không cần
```
- `ac_seeds` (mỗi checklist item + dòng AC/comment giống định-nghĩa-done) → nạp thẳng vào sổ AC bằng
  `run_log.py ac-add` (xem "Trích acceptance criteria").
- `signals.thin_description=true` + `has_extra_context=false` → task **thực sự** mơ hồ (không có gì cứu)
  → chắc chắn cần hỏi người ở clarify; đừng để chạy auto.
- Mọi sub-fetch lỗi chỉ ghi vào `notes`, pack vẫn sinh ra (không làm sập pipeline).

## Scout — pre-grounding: dò repo TRƯỚC khi lập plan

`grounding.py run` cần `<target_files>` từ plan; nhưng plan mù repo chính vì chưa biết file nào liên
quan. `scout` dò ngược: từ **từ khoá của task** grep `clone_dir` → liệt kê file ứng viên để plan neo
vào code thật:

```bash
# Ưu tiên: dùng `keywords` sạch mà context_pack đã trích (tránh nhiễu từ khung markdown):
python grounding.py scout --run <RID> --root <clone_dir> --keywords "SprintService,permission,export"
#   fallback khi chưa có keywords: --desc-file ../../../../temp/runs/<src>-<id>_context.md (tự bỏ heading md)
#   -> temp/runs/<RID>_scout.md  + JSON {candidates:[{path,score,hits}], verdict}
```
Đưa `*_scout.md` (top file ứng viên) vào `--desc`/ngữ cảnh của debate để plan chọn đúng file. `verdict=fail`
(không file nào khớp) = plan phải tự định vị target, cân nhắc hỏi người.

## Clarify — làm rõ yêu cầu mơ hồ (TRƯỚC triage/debate)

Hầu hết yêu cầu vào đều mơ hồ; debate giỏi mấy mà input mơ hồ vẫn lên plan sai. Chạy `clarify.py`
ngay sau context pack + scout, **trước** triage và debate (dùng pack làm `--desc`):

```bash
cd .claude/skills/auto-dev/tools
python clarify.py analyze --type <bugfix|feature> --title "<tiêu đề>" \
  --desc-file ../../../../temp/runs/<src>-<id>_context.md \
  --context-file ../../../../temp/runs/<RID>_scout.md --backend claude
#   --context-file: nạp scout candidates (+pack) để agent ĐỀ XUẤT câu trả lời có căn cứ, không chỉ hỏi.
#   --backend: khuyến nghị BẬT cho task thật (câu hỏi sát + có `proposed`); lỗi agent -> tự fallback heuristic.
```
Mỗi câu hỏi giờ có thêm trường **`proposed`** = câu trả lời cụ thể agent rút từ ngữ cảnh (file/field/hành vi
thật), hoặc bằng `assumption` khi không đủ căn cứ. **Trình bày cho người: "Câu hỏi → [proposed] — xác nhận/sửa?"**
→ người chỉ bấm xác nhận thay vì soạn từ đầu (giảm ma sát, nên hay được trả lời hơn → plan no thông tin hơn).
Trả `{verdict, blocking_count, questions:[{category, ask, why, blocking, assumption}]}`:
- `category` ∈ scope · io · acceptance · edge_case · non_functional.
- `blocking=true` → **không trả lời thì gần như chắc code sai** (scope / I-O contract / định-nghĩa-done).
  `blocking=false` → chạy được với giả định mặc định.
- `verdict=needs_clarification` khi còn câu blocking; `pass` khi không.
- **Type-aware:** bugfix rõ ràng thường `pass` (AC ngầm = bug hết repro); feature mơ hồ dồn nhiều câu blocking.

**Xử lý (theo mode — đồng bộ gate `clarity` của stage plan):**
1. **Hỏi câu BLOCKING** cho người dùng (chỉ blocking; phần còn lại để giả định). Ở **auto mode**
   (tier trivial) thì KHÔNG hỏi — `needs_clarification` làm gate `clarity` **fail → chặn debate**
   (task quá mơ hồ không được tự chạy). Ở **checkpoint mode** thì hỏi người, gấp câu trả lời.
2. Gấp trả lời + giả định thành brief:
   ```bash
   python clarify.py brief --title "<...>" --desc-file task.txt \
     --answers-file answers.json --out ../../../../temp/runs/<task_id>_brief.md
   #   answers.json: [{"ask":"...","answer":"..."}, {"ask":"...","assumption":"..."}]
   ```
   Brief (`temp/runs/<task_id>_brief.md`) thành `--desc` cho debate; `acceptance_seeds` → `ac-add`.
3. Ghi gate cho run-log (required trên stage plan):
   ```bash
   python ../../dev-automation/tools/run_log.py record-gate <RID> clarity --verdict pass --summary "0 blocking"
   # còn câu blocking chưa giải -> --verdict fail
   ```

## Triage — phân tier/mode (Hybrid autonomy)

Chạy **SAU clarify** (để biết task có mơ hồ không), phân loại để chọn mức tự động:

```bash
cd .claude/skills/auto-dev/tools
python triage.py classify --type <bugfix|feature> --title "<tiêu đề>" \
  --desc-file ../../../../temp/runs/<src>-<id>_context.md \
  --clarity <pass|needs_clarification> --backend claude
#   --clarity: verdict từ clarify. needs_clarification -> KHÔNG cho chạy auto (hạ về checkpoint).
#   [--plan <path>]  đếm <target_files> nếu đã có spec
```
Trả `{tier, mode, reason, skip_debate, clarity_downgrade?}`:
- `mode=auto` **chỉ khi** `tier=trivial` VÀ `clarity=pass` — task mơ hồ (còn câu blocking) luôn giữ người
  trong vòng lặp (`clarity_downgrade` ghi lại việc hạ auto→checkpoint).
- `standard|complex` → `mode=checkpoint`, giữ 3 mốc duyệt.
- Tín hiệu rủi ro (schema/migration/bảo mật/thanh toán/đồng thời...) → ép `complex`.

Override tay khi cần: `--force-tier` / `--force-mode` (áp CUỐI, thắng cả clarity downgrade).

## Trích acceptance criteria

Bóc tiêu chí nghiệm thu từ mô tả/repro/AC của task vào sổ AC (đối chiếu ở Deliver):
```bash
python run_log.py ac-add <RID> --text "GET /api/report/export trả 200 + CSV đúng cột"
python run_log.py ac-add <RID> --text "input độc hại không gây SQLi"
# ... mỗi tiêu chí một dòng. Sổ trống = không ép (task adhoc/trivial).
```

## Chuẩn hoá sau intake

Bất kể nguồn nào, sau intake phải có đủ:

| Field | Lấy từ |
|---|---|
| `run_id` | `<project>-<task_id>` hoặc `adhoc-<slug>` |
| `task_id` | id nguồn (rỗng nếu adhoc) |
| `tier` / `mode` | `triage.py` (hoặc override tay) |
| `project` | khớp `./work/projects.json` (hỏi nếu chưa có registry) |
| `type` | bugfix \| feature |
| `title` | tiêu đề task |
| `acceptance_criteria` | `ac-add` từ mô tả/AC của task |
| `clone_dir` + `test_cmd` | registry, hoặc hỏi người dùng |

Thiếu `project`/`clone_dir` mà registry chưa tồn tại → **hỏi người dùng** đường dẫn code và
lệnh test trước khi tiếp tục (đừng đoán).

## Queue — nạp nhiều task, luồng resolver xử lý TUẦN TỰ (chống xung đột repo)

Nhiều run tự động cùng lúc trên một repo = nguy cơ giẫm branch/worktree của nhau.
`task_queue.py` tách intake (rẻ, làm hàng loạt) khỏi xử lý (đắt, **một-task-một-lúc
TRÊN LUỒNG `task_resolver`** — lock theo owner, KHÔNG chặn người làm tay).
`task_resolver.py` claim/release đúng lock này quanh mỗi task nó xử lý. Slash: `/atask-queue`.

```bash
cd .claude/skills/auto-dev/tools
python task_queue.py scan --limit 10                     # task aTask của tôi chưa vào queue (read-only)
python task_queue.py intake --source atask --task <id> --project <P> --type bugfix --backend claude
#   = context_pack -> scout -> feedback recall -> clarify, lưu artifact paths vào item.
#   [--post-questions]  [WRITE] comment câu hỏi blocking + proposed lên task aTask -> confirm trước.
python task_queue.py list [--state ready]                # tổng quan (priority -> tuổi) + locks theo owner
python task_queue.py answer <qid> --accept-proposed      # hoặc --answers-file ans.json -> brief, item ready
#   answer MẶC ĐỊNH đồng bộ brief lên aTask ([WRITE] comment "Yêu cầu đã làm rõ") -> task đủ
#   ngữ cảnh để BÀN GIAO cho người khác (assign qua /atask-triage). --no-sync để bỏ.
python task_queue.py next [--owner task_resolver]        # lấy task đầu hàng + giữ lock CỦA LUỒNG đó
python task_queue.py done <qid> --result ok|fail         # nhả lock -> task kế của luồng mới chạy
python task_queue.py release [--owner ...]               # cứu lock kẹt (phiên chết giữa chừng)
```
- Trạng thái: `needs_clarification → ready → processing → done|failed` (`requeue` từ failed/done).
- **Thin guard**: mô tả rỗng + không comment/checklist/subtask → ép `needs_clarification`
  (đúng quy tắc ở mục Context pack), kể cả khi heuristic clarify không thấy câu blocking.
- Item lưu ở `work/queue/items/<qid>.json` (gitignored); artifact ở `temp/runs/<qid>_*.md`;
  lock ở `work/queue/lock_<owner>.json`.
- `next` trả `pipeline_hint` + `run_id` gợi ý — chạy pipeline.md với `artifacts.brief`/`.pack`
  làm `--desc-file`, `artifacts.corrections` cho debate, `ac_seeds` → `ac-add`.

## GĐ2 — poll tự động (chưa bật)

Khi muốn pipeline tự nhận task mới (không cần gõ lệnh), dùng `/loop` hoặc `/schedule` để
chạy định kỳ: `azure_devops.py list` + `search.py my-tasks` → so với run-log đã có
(`run_log.py list`) → task mới nào chưa có run thì khởi tạo. **Vẫn giữ checkpoint** —
poll chỉ tự *nhận*, không tự *giao*. Đây là việc của GĐ2, không bật ở GĐ1.
