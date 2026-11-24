# aTask AI — Năng lực của Claude & Mô hình Governance

> Cập nhật theo tầng **AI-agent governance + capability + virtual-model** (MR !621). Mọi tool route qua
> `POST /api/ai/execute` (PAT auth), enforce governance **server-side** — skill chỉ là client.

---

## 1. Claude làm được gì với aTask (capability map)

| Nhóm | Việc Claude làm được | Tool / lệnh skill |
|---|---|---|
| **Đọc & tra cứu** | Xem task/subtask, project, sprint, list/board, workspace, comment, checklist, attachment, lịch sử | `tasks.py get/subtasks` · `projects.py get-*/sprints/workspace/lists` · `checklists.py list/comments` |
| **Tìm kiếm nâng cao** | Task của tôi, quá hạn, theo sprint/status/priority, **truy vấn DSL an toàn cross-project**, gợi ý ứng viên | `search.py my-tasks/tasks/dashboard/candidates` · `governed_search.py` |
| **Tạo & cập nhật** *(WRITE)* | Tạo/sửa/hoàn thành/xoá/di chuyển task; gán người/org/group/sprint; comment; checklist; **tạo project/sprint/list, start/complete sprint** | `tasks.py create/update/complete/move/assign-sprint/delete` · `checklists.py` · `projects.py create-project/create-sprint/start-sprint/complete-sprint/create-list` |
| **Phân tích & dashboard** | Thống kê, biểu đồ status/priority, xu hướng hoàn thành, tỷ lệ finish, task chưa giao, dashboard cá nhân/dự án/tổ chức, **workload theo người** | `analytics.py stats/by-status/by-priority/trends/finish-rates/unassigned/*-dashboard/workload/by-metric` |
| **Tự động hoá luồng** | Poll task của tôi → phân tích → đề xuất assign/execute → duyệt → auto-dev | `atask_watch.py` (xem skill auto-dev / atask-triage) |

**Bộ tool server-side hiện có (~64)** thuộc các lớp: TaskTools, CommentTools, ChecklistTools, ProjectTools,
ListTaskTools, dashboard/metric tools, và **GovernedQueryTools** (`governed_search`).

---

## 2. Mô hình Governance (BẮT BUỘC hiểu khi dùng)

### 2.1. Xác thực & phân quyền (fail-closed)
- **PAT** mang **scope** dạng `SCOPE_<entity>:<verb>` → map thành permission verb `{read, write, delete}`
  (whitelist; scope dị dạng/verb lạ bị **bỏ** — fail-closed). PAT **không có scope nào ⇒ không chạy được tool nào**.
- **JWT thường** (ROLE_USER) chỉ dùng được tool khi `atask.aiagent.jwt-tool-access=true` (**chỉ bật ở dev**;
  prod đặt tường minh `false`).
- Tool khai báo `requiredPermissions` (vd `task:write`) → registry chặn nếu thiếu (`PERMISSION_DENIED`).

### 2.2. READ cross-project → `governed_search` (đường an toàn)
- DSL whitelist: chỉ **entity/field/op** trong `GovernedQuerySchema` (`task`/`project`/`list_task`). Mọi value
  **bind param**, `IN` cap `MAX_IN_SIZE=100`, `LIKE` prefix-escape.
- **Tenant inject server** (`custId`/`orgIn`). Task xuyên project **bắt buộc** giới hạn theo bạn
  (`isMine=true`/`createdByMe=true`) hoặc khoá `projectId` mình là thành viên → nếu không sẽ bị từ chối.
- Field nhạy cảm theo persona (D11): non-manager không lọc được field `SELF_OR_MANAGER`/`NEVER_CROSS_PERSON`.

### 2.3. WRITE → authz theo entity (chống IDOR)
- Mọi thao tác ghi theo id (update/delete/complete/move/assign task; comment; checklist; sprint start/complete)
  **kiểm tra: cùng tenant + là thành viên project** chứa tài nguyên. Không đủ quyền → `PERMISSION_DENIED`;
  không tồn tại → `NOT_FOUND`. ⇒ **không sửa/xoá được dữ liệu khác tenant/khác project**.
- Tạo mới (create_task/comment/checklist/project/sprint/list) **set tenant server-side** từ người gọi.

### 2.4. Tool tổng hợp-theo-người
- **Aggregate (nhiều người)** vd `get_org_workload`, `get_task_count_by_assignee`: non-manager → **DEGRADE**
  (k-anon — ẩn per-person, chỉ trả cohort size/chỉ số tổng; suppress nếu cohort `< k`).
- **Single-subject** vd `get_user_dashboard_summary`: bị **chặn** với non-manager. *Manager-bypass đang
  DEFER (fail-closed)* — chưa scope được theo org subtree của subject, nên tạm chặn cả manager.

### 2.5. Audit & quyền riêng tư
- **Mọi** tool-call (kể cả lỗi) ghi `pat_audit_log`: token, user, tool, subject, decision, duration.
- **Default-redact**: value input bị che (`***`) cho mọi tool **trừ** tool phân loại `OPERATIONAL`
  (tool chưa phân loại → vẫn redact). Không lưu free-text (title/search/email) dạng cleartext.

---

## 2bis. Virtual model (semantic layer) — đằng sau `governed_search`

Lớp ngữ nghĩa: **entity/field LOGIC** ánh xạ cột vật lý (bảng sharded), server tự **route SQL↔ES**, tự inject
tenant + ACL, và **tự điền** field selectable (join/port) sau khi query. Đây là cách truy vấn linh hoạt + an
toàn nhất (thay cho việc gọi nhiều tool đọc rời rạc).

**Entity `task`**
| Field logic | Loại | Op | Ghi chú |
|---|---|---|---|
| `id`, `status`, `priority`, `listTaskId`, `parentId`, `projectId` | string | EQ / IN | `projectId` EQ → route **SQL 1-shard** (live) |
| `name` | string | CONTAINS | prefix LIKE (escape) |
| `startDate`, `dueDate` | date | GT/GTE/LT/LTE | |
| `daysOverdue` | number *(computed từ due_date)* | GT/GTE/LT/LTE | |
| `isMine` | bool | EQ | **ES-only** (cross-project) — assignee = bạn |
| `createdByMe` | bool | EQ | created_by = bạn |
| `projectName` | string *(join project.name)* | — *(selectable, tự điền)* | tenant-safe (`findByIdAndCustId`) |
| `creatorName` | string *(port USER)* | — *(selectable, tự điền)* | chỉ lộ tên **cùng tenant** |

**Entity `project`**: `id`, `name` (CONTAINS), `code`, `status`, `startDate` — scope = **chỉ project mình là thành viên**.
**Entity `list_task`**: `id`, `name` (CONTAINS), `priority`, `startDate`, `dueDate`, `template` (EQ) — scope theo **list-task của mình**.
**Entity `sprint`**: `id`, `name` (CONTAINS), `projectId`, `status`, `startDate`, `endDate` — scope theo project mình là thành viên.

**Routing tự động:** task có `projectId` EQ → SQL 1-shard; task **cross-project** (bắt buộc `isMine`/`createdByMe=true`)
→ **ES read-model**; project/list_task/sprint → reference/scoped SQL. Field/op ngoài whitelist → `GOVERNED_QUERY_REJECTED`.

**Quan hệ (relationship)** trả trong `describe` (vd task→project, project→tasks/sprints) — engine **không join**;
agent tự query nhiều bước phẳng theo `navigate` hint.

**Discovery (đừng dò bằng lỗi — nạp vào prompt chatbot):**
```
python3 governed_search.py list-entities              # entity + schemaVersion
python3 governed_search.py describe --entity task     # field/ops/kind/selectable/sensitivity/relationships (persona-aware)
```
**Query:**
```
python3 governed_search.py search --entity task --filter "isMine:EQ:true" --filter "daysOverdue:GTE:3"
python3 governed_search.py search --entity project   --filter "name:CONTAINS:kpi"
python3 governed_search.py search --entity list_task --filter "template:EQ:false"
```
> `describe` **lọc field theo persona** người gọi (non-manager chỉ thấy field `ALWAYS`) — C4 đã sẵn ở server.
> Output kèm `schemaVersion` để chatbot phát hiện schema đổi.

## 3. Quy ước khi Claude thao tác aTask
1. **Đọc/tra cứu** thì ưu tiên tool đọc + `governed_search` (đừng tự suy diễn dữ liệu).
2. **Trước thao tác WRITE** (tạo/sửa/xoá/gán/đổi trạng thái): theo guardrail CLAUDE.md — **hỏi xác nhận** lần đầu
   trong một mạch tự động; **luôn confirm** trước xoá và trước khi đưa task sang trạng thái cuối.
3. Lỗi trả JSON `{"error":true,...}` hoặc `PERMISSION_DENIED`/`NOT_FOUND`/`TOOL_RESTRICTED`/`GOVERNED_QUERY_REJECTED`
   — **đọc và xử lý**, đừng coi là crash; nếu bị từ chối quyền thì báo người dùng, không retry mù.
4. Quyền thật vẫn enforce ở tầng service theo danh tính user của PAT — Claude **không** vượt quyền user.

---

## 4. Giới hạn hiện tại (đã biết)
- **Manager-bypass** tool per-person: tắt (defer) tới khi có resolver `user_id→orgIn`.
- **`complete_task`** đặt status literal `"DONE"`, chưa đi qua workflow completion (cascade subtask, trigger
  scoring eKPI) — đủ cho thao tác đơn giản.
- Một số tool server (vd `get_attachments`, `update_comment`, single `get_comment`) chưa có lệnh skill riêng —
  có thể gọi trực tiếp qua `client.execute_tool("<tool>", {...})`.
