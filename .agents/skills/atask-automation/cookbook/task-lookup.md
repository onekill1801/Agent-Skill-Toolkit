# Cookbook: Task Lookup & Navigation

## Purpose
Find, browse, and inspect tasks in aTask — read-only operations, no risk of data loss.

## Prerequisites
- `.env` has `ATASK_BASE_URL` and `ATASK_PAT_TOKEN`
- Run `python3 config.py` to verify; run from `tools/` directory

## Output shaping (read this first)

List/read tools default to a lean **`summary`** view — ~6KB for 30 tasks instead of ~1.9MB of raw
JSON. Status is shown via `statusType` (Chưa làm / Đang làm / Đã duyệt / Hoàn thành / Đã đóng), not
the opaque per-list status ID.

- `--format summary` (default for lists) — compact block per task.
- `--format table [--fields id,name,status,due,priority,project]` — aligned columns; pick your own.
- `--format json` — full raw record (every field; use when you need `assignTaskList`, `orgIn`, etc.).
- `tasks.py get <id>` defaults to `json` (a single record is small and you usually want full detail).

Applies to: `search.py` tasks/my-tasks/dashboard/candidates · `tasks.py` query/subtasks/by-sprint.

## Steps

### 1. Find my assigned tasks (quickest start)
```bash
cd .claude/skills/atask-automation/tools
python3 search.py my-tasks                       # lean summary (default)
python3 search.py my-tasks --format table        # aligned columns
```
Optionally filter:
```bash
python3 search.py my-tasks --status OPEN,IN_PROGRESS --size 50
python3 search.py my-tasks --query "payment" --start 2026-01-01T00:00:00Z
python3 search.py my-tasks --format table --fields id,name,status,due,project
```

### 2. Broad task search (all accessible tasks)
```bash
python3 search.py tasks --query "login bug" --priority HIGH,URGENT
python3 search.py tasks --list <list_task_id> --status IN_PROGRESS
python3 search.py tasks --project <project_id> --created-by johndoe --page 0 --size 20
```

### 3. Get full task details
```bash
python3 tasks.py get <task_id>
```

### 4. List subtasks (children of a task)
```bash
python3 tasks.py subtasks <parent_task_id>
```

### 5. Query all tasks in a specific list/board
```bash
python3 tasks.py query <list_task_id>
python3 tasks.py query <list_task_id> --status <status_id>      # status_id = a per-list group_task id
python3 tasks.py query <list_task_id> --parent <parent_id>
python3 tasks.py query <list_task_id> --page 0 --size 50        # paginated (server caps size at 200)
```
> Paginated server-side: response is `{data, page, size, totalRecords, totalPages}` (same shape as
> search). `get_comments`/`get_checklists` are paginated too (`page`/`size`, default 50).
> ⚠️ `--status` here is a **per-list status ID** (a `group_task` id like `00002qOI...`), NOT a literal
> like `OPEN`/`DONE` (those never match → empty result). For status-**group** filtering
> (todo/processing/approved/completed/closed) use `search.py my-tasks/tasks --status-type ...` instead.
> `query`/`subtasks`/`by-sprint`/`get` resolve the status into a readable `statusName`/`statusType`
> (once the backend change ships), so the summary view no longer shows a bare `[-]`.

### 6. Get tasks in a sprint
```bash
python3 tasks.py by-sprint <sprint_id>
```
If you don't know the sprint ID:
```bash
python3 projects.py my-projects           # find project
python3 projects.py sprints <project_id>  # list sprints
python3 tasks.py by-sprint <sprint_id>
```

### 7. Search in workspace dashboard
```bash
python3 search.py dashboard <workspace_id>
python3 search.py dashboard <workspace_id> --status OPEN,IN_PROGRESS --query "urgent"
```

### 8. Find linkable tasks (for cross-task referencing)
```bash
python3 search.py candidates --query "invoice" --exclude <id1>,<id2>
```

## Error Recovery

| Error | Likely cause | Fix |
|---|---|---|
| `401 Unauthorized` | Expired or missing PAT | Re-generate token; update `ATASK_PAT_TOKEN` in `.env` |
| `Connection refused` | Wrong `ATASK_BASE_URL` | Check URL in `.env`; confirm server is running |
| `SSL: CERTIFICATE_VERIFY_FAILED` | Self-signed cert | Set `ATASK_VERIFY_SSL=false` in `.env` |
| `error: Elasticsearch` | ES cluster is down | Fall back to `tasks.py query <list_id>` (uses DB directly) |
| Empty results | User has no assignments | Try `search.py tasks` with explicit filters |
