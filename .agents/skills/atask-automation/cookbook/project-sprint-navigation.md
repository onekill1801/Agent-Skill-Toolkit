# Cookbook: Project & Sprint Navigation

## Purpose
Browse the aTask hierarchy: workspaces → lists/boards → projects → sprints.
All read-only. Also covers PAT bootstrap instructions.

## Prerequisites
- `.env` configured; PAT needs `project:read`, `sprint:read`, `list:read`, `workspace:read`
- Run from `tools/` directory

## PAT Bootstrap (first-time setup)

The `ATASK_PAT_TOKEN` must be created using your **session JWT** (OAuth2 login),
not an existing PAT. Run this curl in a terminal where you have a valid session:

```bash
curl -X POST "$ATASK_BASE_URL/api/account/tokens" \
  -H "Authorization: Bearer <SESSION_JWT>" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "claude-skill",
    "scopes": [
      "task:read", "task:write", "task:delete",
      "checklist:read", "checklist:write",
      "comment:read", "comment:write",
      "workspace:read", "project:read", "sprint:read", "list:read"
    ]
  }'
```

Copy the returned `token` value into `.env` as `ATASK_PAT_TOKEN=<value>`.
The token is shown **only once**.

To list or revoke existing PATs:
```bash
python3 auth.py list
python3 auth.py revoke <pat_id>
```

## Steps

### 1. List my projects
```bash
cd .claude/skills/atask-automation/tools
python3 projects.py my-projects
python3 projects.py my-projects --filter "Payment" --size 10
```

### 2. Get project details
```bash
python3 projects.py get-project <project_id>
```

### 3. List sprints in a project
```bash
python3 projects.py sprints <project_id>
```

### 4. Get sprint details
```bash
python3 projects.py get-sprint <sprint_id>
```

### 5. Find which project a list/board belongs to
```bash
python3 projects.py project-for-list <list_id>
```

### 6. Get workspace details
```bash
python3 projects.py workspace <workspace_id>
python3 projects.py workspace <workspace_id> --type org
```

### 7. List all boards in a workspace
```bash
python3 projects.py lists <workspace_id>
```

### 8. Get details of a specific list/board
```bash
python3 projects.py get-list <list_id>
```

### 9. My personal lists/boards
```bash
python3 projects.py my-lists
```

## Navigation Map

```
Workspace
└── Lists / Boards      (projects.py lists <workspace_id>)
    └── Tasks           (tasks.py query <list_id>)
        └── Subtasks    (tasks.py subtasks <task_id>)

Project
└── Sprints             (projects.py sprints <project_id>)
    └── Tasks           (tasks.py by-sprint <sprint_id>)
```

## Error Recovery

| Error | Likely cause | Fix |
|---|---|---|
| `401` on PAT bootstrap curl | Session JWT expired | Log into aTask web, grab new cookie |
| `NOT_FOUND` project | Wrong ID or no access | Check `my-projects` list |
| Empty `sprints` | Project has no sprints | Create sprints in aTask web |
