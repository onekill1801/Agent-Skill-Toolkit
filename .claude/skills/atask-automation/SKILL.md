---
name: aTask Automation
description: >
  Manage tasks, checklists, comments, projects, sprints, workspaces, and analytics
  in the aTask platform via its AI-agent REST API (PAT auth).
  Trigger phrases: "create task", "update task", "search tasks", "my tasks",
  "add comment", "add checklist", "show statistics", "overdue tasks",
  "list my projects", "show workspace", "get sprint", "tạo task", "task của tôi".
---

# aTask Automation

## Feature Flags

```yaml
ENABLE_TASK_WRITE: true      # create / update / delete / move tasks
ENABLE_ANALYTICS: true       # statistics + dashboard tools
ENABLE_SEARCH: true          # Elasticsearch-backed search tools
ENABLE_PAT_MGMT: false       # PAT token management (disabled – bootstrap manually)
```

## Available Tools

All tools are in `tools/` relative to this SKILL.md. The agent **MUST** read tool
source before first use to understand exact function signatures.

| Tool | Purpose |
|------|---------|
| `tools/config.py` | Config loader — reads `.env` for `ATASK_BASE_URL`, `ATASK_PAT_TOKEN`, `ATASK_VERIFY_SSL` |
| `tools/client.py` | Shared urllib HTTP client — PAT via `X-aTask-PAT` header, SSL toggle |
| `tools/tasks.py` | Task CRUD: get, query, create, update, delete, complete, move, sprint ops |
| `tools/checklists.py` | Checklist items, comments, and file attachments |
| `tools/projects.py` | Projects, sprints, workspaces, lists/boards — read + write (create project/sprint/list, start/complete sprint) |
| `tools/search.py` | Elasticsearch-backed task search (full-text + filters, incl. `--status-type`) |
| `tools/view.py` | Shared output shaping — lean `summary`/`table`/`json` views for list & read tools |
| `tools/analytics.py` | Statistics, trends, overdue, finish rates + dashboard summaries / workload / drill-down |
| `tools/governed_search.py` | Safe DSL query (`governed_search`) — whitelisted entity/field/op, read-only |
| `tools/auth.py` | PAT management (list/revoke — create requires session JWT) |
| `tools/atask_watch.py` | **Triage watcher** — poll my-tasks → phân tích (claude -p) → đề xuất assign/execute → duyệt Telegram → mở auto-dev hoặc báo người. Slash: `/atask-triage` |
| `tools/task_resolver.py` | **Resolver watcher** — poll my-tasks (mọi trạng thái CHƯA completed: todo/processing/approved) → xác minh trong code (`work/<project>`) đã fix chưa → FIXED: Hoàn thành/Chờ phê duyệt · NOT_FIXED: tôi tự làm / giao người + chỉnh estimate. Duyệt Telegram (nút bấm). Slash: `/atask-resolve` |

## Workflows

### Workflow 1: Task Lookup & Navigation _(read-only)_

**Triggers:** "show my tasks", "find task", "search tasks", "what tasks do I have",
"list tasks in sprint", "show task details", "get subtasks", "task của tôi"

**Steps:**
1. Read `cookbook/task-lookup.md` for guidance
2. Start with `search_my_assigned_tasks` when user says "my tasks" (no args)
3. Use `search_tasks` for broader query: `python3 search.py my-tasks`
4. Drill into task: `python3 tasks.py get <task_id>`
5. Get subtasks: `python3 tasks.py subtasks <parent_task_id>`
6. Sprint scope: `python3 tasks.py by-sprint <sprint_id>`
7. List-scoped: `python3 tasks.py query <list_task_id> [--status STATUS]`

> **Output shaping (list & read tools):** `search.py` (tasks/my-tasks/dashboard/candidates) and
> `tasks.py` (query/subtasks/by-sprint) default to a lean **`summary`** view (~6KB vs ~1.9MB raw for
> 30 tasks). Add `--format table [--fields id,name,status,due,priority,project]` for columns, or
> `--format json` for the full raw record. Status shows `statusType` (Chưa làm/Đang làm/Đã duyệt/
> Hoàn thành/Đã đóng). `tasks.py get` defaults to `json` (single record = full detail).

### Workflow 2: Task CRUD _(write — confirm destructive ops)_

**Triggers:** "create task", "add task", "update task", "complete task", "done with task",
"delete task", "move task to", "assign task to sprint", "tạo task", "cập nhật task"

**Steps:**
1. Read `cookbook/task-crud.md` for workflow details
2. Discover list ID if unknown: `python3 projects.py my-lists`
3. **Create:** `python3 tasks.py create --name "NAME" --list LIST_ID [--priority 1-4] [--due DATE]`
4. **Update:** `python3 tasks.py update TASK_ID [--name X] [--status X] [--priority 1-4]`
   - **Priority** = số `1-4`: `1`=Khẩn cấp · `2`=Cao · `3`=Trung bình · `4`=Thấp (cũng nhận nhãn URGENT/HIGH/MEDIUM/LOW).
5. **Complete:** `python3 tasks.py complete TASK_ID`
6. **Move:** `python3 tasks.py move TASK_ID TARGET_LIST_ID`
7. **Sprint:** `python3 tasks.py assign-sprint TASK_ID SPRINT_ID`
8. **Delete:** ⚠️ Confirm with user first → `python3 tasks.py delete TASK_ID`

### Workflow 3: Checklist & Comment Management _(read + write)_

**Triggers:** "add checklist item", "check off item", "add comment", "post comment",
"view comments", "list attachments", "thêm checklist", "bình luận task"

**Steps:**
1. Read `cookbook/checklist-comments.md`
2. View checklists: `python3 checklists.py list TASK_ID`
3. Add checklist: `python3 checklists.py create TASK_ID "ITEM NAME"`
4. Delete item: ⚠️ Confirm → `python3 checklists.py delete CHECKLIST_ID`
5. View files: `python3 checklists.py attachments TASK_ID`
6. Read comments: `python3 checklists.py comments TASK_ID`
7. Post comment: `python3 checklists.py add-comment TASK_ID "CONTENT"`
8. Delete comment: ⚠️ Confirm → `python3 checklists.py del-comment COMMENT_ID`

### Workflow 4: Project & Sprint Navigation _(read + write)_

**Triggers:** "list my projects", "show sprints", "get sprint details",
"which project is this list in", "show workspace", "list boards", "dự án của tôi",
"create project", "create sprint", "start/complete sprint", "create list/board"

**Steps (read):**
1. Read `cookbook/project-sprint-navigation.md`
2. My projects: `python3 projects.py my-projects`
3. Project details: `python3 projects.py get-project PROJECT_ID`
4. Sprints in project: `python3 projects.py sprints PROJECT_ID`
5. Sprint details: `python3 projects.py get-sprint SPRINT_ID`
6. Project from list: `python3 projects.py project-for-list LIST_ID`
7. Workspace: `python3 projects.py workspace WORKSPACE_ID`
8. Lists in workspace: `python3 projects.py lists WORKSPACE_ID`
9. My lists: `python3 projects.py my-lists`

**Steps (write — confirm trước; server enforce scope write + membership):**
- Create project: `python3 projects.py create-project "Tên" [--code DM] [--priority HIGH]`
- Create sprint: `python3 projects.py create-sprint PROJECT_ID "Sprint 1" [--goal ...]`
- Start / complete sprint: `python3 projects.py start-sprint SPRINT_ID` · `complete-sprint SPRINT_ID`
- Create list/board: `python3 projects.py create-list "Tên list"`

### Workflow 5: Analytics & Reporting _(read-only)_

**Triggers:** "show statistics", "task stats", "how many tasks done", "overdue tasks",
"completion trend", "tasks by priority", "unassigned tasks", "task history",
"báo cáo task", "thống kê"

**Steps:**
1. Read `cookbook/analytics-reporting.md`
2. High-level overview: `python3 analytics.py stats`
3. Branch by user intent:
   - Status counts: `python3 analytics.py by-status [--scope my|org]`
   - By assignee: `python3 analytics.py by-assignee [--list LIST_ID]`
   - By department: `python3 analytics.py by-org [--scope my|org]`
   - Priority chart: `python3 analytics.py by-priority`
   - Overdue: `python3 analytics.py overdue LIST_ID`
   - Trends: `python3 analytics.py trends --start DATE --end DATE --period day|week|month`
   - Finish rates: `python3 analytics.py finish-rates`
   - Unassigned count: `python3 analytics.py unassigned [--list LIST_ID]`
   - Activity history: `python3 analytics.py history [--scope my|org]`
   - **Dashboard summary (cá nhân/tổ chức/user/project):** `python3 analytics.py my-dashboard` ·
     `org-dashboard [--org ORG]` · `user-dashboard <user_id>` · `project-dashboard <project_id>`
   - **Workload theo nhân sự:** `python3 analytics.py workload [--org ORG] [--q TEXT]`
   - **Drill-down 1 metric:** `python3 analytics.py by-metric --scope my|org|user|project --metric overdue|expiringSoon|urgent|completed|inProgress [--ref-id ID]`
   - **Task gần đây:** `python3 analytics.py recent [--scope my|org] [--size N]`

### Workflow 6: Governed Search / Virtual model _(read-only, an toàn)_

**Semantic layer**: 4 entity logic — `task` / `project` / `list_task` / `sprint` — field ánh xạ cột vật lý
(sharded), server tự **route SQL↔ES**, inject tenant + ACL, tự điền field selectable (`projectName`/`creatorName`).
- `task`: id/status/priority/listTaskId/parentId/projectId (EQ/IN), name (CONTAINS), startDate/dueDate
  (GT/GTE/LT/LTE), `daysOverdue` (computed), `isMine`/`createdByMe` (EQ). `projectId` EQ → SQL 1-shard;
  cross-project (cần `isMine`/`createdByMe=true`) → ES.
- `project`: id/name/code/status/startDate · `list_task`: id/name/priority/dates/template · `sprint`: id/name/projectId/status/dates.

**Discovery (nạp vào prompt chatbot — đừng dò bằng lỗi):**
```
python3 governed_search.py list-entities            # entity + schemaVersion
python3 governed_search.py describe --entity task   # field/ops/kind/selectable/sensitivity/relationships (persona-aware)
```
**Query:**
```
python3 governed_search.py search --entity task --filter "isMine:EQ:true" --filter "daysOverdue:GTE:3"
python3 governed_search.py search --entity project   --filter "name:CONTAINS:kpi"
python3 governed_search.py search --entity list_task --filter "template:EQ:false"
```
Field/op ngoài whitelist → `GOVERNED_QUERY_REJECTED`. Chi tiết bảng field: `cookbook/ai-capabilities-governance.md` (§2bis).

### Workflow 7: AI Capabilities & Governance _(hiểu trước khi dùng nâng cao)_

Tổng quan **Claude làm được gì với aTask** + mô hình **governance** (scope/permission, governed-query READ
an toàn, write-authz theo entity chống IDOR, degrade/manager cho tool per-person, audit + default-redact,
fail-closed). Đọc khi: gặp `PERMISSION_DENIED`/`TOOL_RESTRICTED`/`GOVERNED_QUERY_REJECTED`, cần biết phạm vi
năng lực, hoặc lên kế hoạch thao tác WRITE/aggregate.
→ **`cookbook/ai-capabilities-governance.md`**

### Workflow 8: Task Resolver — verify-in-code → close / hand off _(write — duyệt Telegram)_

**Triggers:** "resolve task", "xử lý task được giao", "poller task", "task resolver",
"kiểm task đã fix chưa", "đóng task hoặc giao người", "watch & resolve my tasks"

Luồng poller xử-lý-task tự động cho **task giao cho tôi ở MỌI trạng thái CHƯA `completed`**
(todo/processing/approved — loại completed/closed/cancelled). Hẹp lại bằng `ATASK_RESOLVE_STATUS_TYPES`.
1. Đọc `cookbook/task-resolve.md` TRƯỚC (chi tiết quyết định + giả định cần confirm).
2. Poll: `python task_resolver.py` (baseline lần đầu). Xử lý cả **backlog đang có**: `--resolve-existing`.
3. Mỗi task mới/đổi trạng thái → spawn `claude -p` (CHỈ ĐỌC) xác minh trong `work/<project>` đã fix chưa.
4. **FIXED** → thẻ Telegram: `✅ Hoàn thành` (complete_task) | `🕓 Chờ phê duyệt` (update status).
   Tín hiệu đã-validate: `percent==1.0` hoặc `statusType ∈ {approved, completed}`.
5. **NOT_FIXED** → thẻ: `👤 Tôi tự làm` (giữ tôi + chỉnh estimate + comment) | `➡️ Giao người khác`
   → thẻ tiếp `✅ Giao <tên>` (`assign_task_users` + comment + estimate) | `❌ Bỏ qua`.
6. Chống trùng: STATE `temp/atask_resolved.json` khoá (task→statusType + assigned_to); `_inflight`
   chống bốc lại; kiểm assignee hiện có trước khi gán.

> Cần bridge Telegram (ops/approval bot) đang chạy để nhận nút bấm. Một lần: `--task <id>`.
> ⚠️ `update_task(status=)` nhận **status-ID theo từng list** (không phải keyword) — tool tự tra qua
> `search_tasks(..., status_type=)`; assignee đọc từ `assignTaskList` của record search (get_task không trả).

## Routing Rules

| User says... | Workflow | Cookbook |
|---|---|---|
| "my tasks" / "show assigned tasks" / "task của tôi" | Workflow 1 | `cookbook/task-lookup.md` |
| "find task X" / "search tasks" / "tìm task" | Workflow 1 | `cookbook/task-lookup.md` |
| "create task" / "add task" / "tạo task" | Workflow 2 | `cookbook/task-crud.md` |
| "update task" / "complete task" / "delete task" | Workflow 2 | `cookbook/task-crud.md` |
| "add checklist" / "add comment" / "view comments" | Workflow 3 | `cookbook/checklist-comments.md` |
| "list projects" / "show sprints" / "workspace" | Workflow 4 | `cookbook/project-sprint-navigation.md` |
| "create project/sprint/list" / "start/complete sprint" | Workflow 4 (write) | `cookbook/ai-capabilities-governance.md` |
| "what can you do with atask" / "permission denied" / "governance" / "Claude làm được gì" | Workflow 7 | `cookbook/ai-capabilities-governance.md` |
| "statistics" / "task stats" / "overdue" / "thống kê" | Workflow 5 | `cookbook/analytics-reporting.md` |
| "resolve task" / "xử lý task được giao" / "poller task" / "task đã fix chưa" | Workflow 8 | `cookbook/task-resolve.md` |

## Slash Commands

| Command | Workflow |
|---------|----------|
| `/atask-search [query]` | Workflow 1 – Task Lookup |
| `/atask-create <name> [list_id]` | Workflow 2 – Create Task |
| `/atask-projects` | Workflow 4 – Project Navigation |
| `/atask-stats [scope]` | Workflow 5 – Analytics |
| `/atask-resolve [task_id]` | Workflow 8 – Task Resolver |

Run Python from `tools/` — `python3` on Linux/macOS, `python` on Windows if `python3` unavailable.

## Important Rules

1. **Always read the relevant cookbook BEFORE starting a workflow** — it contains detailed steps.
2. **Always read tool source code before first use** to understand exact signatures.
3. **Never hardcode tokens or URLs** — always use `config.py` to load from `.env`.
4. **Confirm before destructive operations:** `delete_task`, `delete_comment`, `delete_checklist`.
5. **Confirm before terminal status changes:** updating a task to DONE/CANCELLED — show current status first.
6. **SSL:** if `ATASK_VERIFY_SSL=false`, warn the user that certificate validation is disabled.
7. **Search fallback:** if Elasticsearch is down, fall back to `tasks.py query` (direct DB via API).
8. **Cap bulk operations** at 20 items per agent turn — ask user to confirm if more needed.
9. **PAT bootstrap:** `auth.py create` requires a session JWT, not a PAT — user must run the curl in `cookbook/project-sprint-navigation.md` manually first.
10. **Governance (server-enforced):** tool route qua governance — gặp `PERMISSION_DENIED` (thiếu scope / không phải thành viên project), `TOOL_RESTRICTED` (tool per-person, non-manager), `GOVERNED_QUERY_REJECTED` (field/op ngoài whitelist) → **báo người dùng, KHÔNG retry mù**. Chi tiết + năng lực đầy đủ: `cookbook/ai-capabilities-governance.md`.
