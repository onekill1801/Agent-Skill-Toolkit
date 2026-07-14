# Cookbook: Checklist & Comment Management

## Purpose
Manage checklist items (todo sub-items), read file attachments, and
manage comments on tasks.

## Prerequisites
- `.env` configured with `ATASK_BASE_URL` and `ATASK_PAT_TOKEN`
- PAT needs `checklist:read` + `checklist:write` + `comment:read` + `comment:write`
- Run from `tools/` directory

## ⚠️ Guardrails
- `delete` (checklist item or comment) is irreversible — confirm with user first

## Checklist Steps

### 1. View all checklist items for a task
```bash
cd .claude/skills/atask-automation/tools
python3 checklists.py list <task_id>
```

### 2. Quick checklist progress count
```bash
python3 checklists.py count <task_id>
# Returns: { "task_id": "...", "count": 5 }
```

### 3. Get a single checklist item
```bash
python3 checklists.py get <checklist_id>
```

### 4. Add a checklist item
```bash
python3 checklists.py create <task_id> "Write unit tests"
python3 checklists.py create <task_id> "Deploy to staging" --value checked
```

### 5. Delete a checklist item ⚠️
```bash
# Confirm with user first, then:
python3 checklists.py delete <checklist_id>
```

### 6. View file attachments
```bash
python3 checklists.py attachments <task_id>
```
> Note: File *upload* is not available via API tools — use the aTask web UI.

## Comment Steps

### 7. View all comments on a task
```bash
python3 checklists.py comments <task_id>
```

### 8. Get a single comment
```bash
python3 checklists.py comment <comment_id>
```

### 9. Post a comment
```bash
python3 checklists.py add-comment <task_id> "Deployment completed at 14:30 UTC."
```

### 10. Count comments
```bash
python3 checklists.py count-comments <task_id>
```

### 11. Delete a comment ⚠️
```bash
# Confirm with user first, then:
python3 checklists.py del-comment <comment_id>
```

## Error Recovery

| Error | Likely cause | Fix |
|---|---|---|
| `403 Forbidden` | Missing `checklist:write` or `comment:write` scope | Re-create PAT with correct scopes |
| `NOT_FOUND` | Wrong checklist/comment ID | Verify ID with `list` command first |
| Empty `attachments` | No files uploaded | Upload via aTask web UI |
