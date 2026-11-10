# Cookbook: Analytics & Reporting

## Purpose
Generate task statistics, trends, and dashboard summaries from aTask.
All read-only. Relies on the DashboardAllService and DashboardService backend.

## Prerequisites
- `.env` configured; PAT needs `workspace:read`, `task:read`
- Run from `tools/` directory

## Steps

### 1. High-level overview (start here)
```bash
cd .claude/skills/atask-automation/tools
python3 analytics.py stats
# Returns: total count, breakdown by status, breakdown by organization
```

### 2. Task counts by status
```bash
# My tasks
python3 analytics.py by-status

# Org-wide
python3 analytics.py by-status --scope org

# With date range
python3 analytics.py by-status --scope my --start 2026-05-01T00:00:00Z --end 2026-05-31T23:59:59Z
```

### 3. Task counts by assignee (workload distribution)
```bash
python3 analytics.py by-assignee
python3 analytics.py by-assignee --list <list_task_id>
python3 analytics.py by-assignee --start 2026-05-01T00:00:00Z
```

### 4. Task counts by department / organization
```bash
python3 analytics.py by-org
python3 analytics.py by-org --scope org
```

### 5. Priority chart (last 30 days default)
```bash
python3 analytics.py by-priority
python3 analytics.py by-priority --start 2026-01-01T00:00:00Z --end 2026-05-31T23:59:59Z
```

### 6. Overdue tasks in a list/board
```bash
python3 analytics.py overdue <list_task_id>
python3 analytics.py overdue <list_task_id> --start 2026-01-01T00:00:00Z
```

### 7. Completion trends (burndown / velocity)
```bash
# Daily trend for May 2026
python3 analytics.py trends \
    --start 2026-05-01T00:00:00Z \
    --end   2026-05-31T23:59:59Z \
    --period day

# Weekly trend
python3 analytics.py trends \
    --start 2026-01-01T00:00:00Z \
    --end   2026-05-31T23:59:59Z \
    --period week

# Monthly trend
python3 analytics.py trends \
    --start 2025-06-01T00:00:00Z \
    --end   2026-05-31T23:59:59Z \
    --period month
```

### 8. Finish / completion rates
```bash
python3 analytics.py finish-rates
python3 analytics.py finish-rates --dept "IT Department"
python3 analytics.py finish-rates --start 2026-05-01T00:00:00Z --end 2026-05-31T23:59:59Z
```

### 9. Unassigned task count (capacity gaps)
```bash
# All tasks without assignees (current user scope)
python3 analytics.py unassigned

# For a specific list/board
python3 analytics.py unassigned --list <list_task_id>
```

### 10. Recent task activity / audit trail
```bash
# My recent activity
python3 analytics.py history

# Org-wide activity
python3 analytics.py history --scope org --size 50

# With pagination
python3 analytics.py history --page 1 --size 20
```

## Reporting Tips

- Combine `by-status` + `overdue` for a quick sprint health check
- Use `trends --period week` to generate a weekly velocity chart
- `unassigned` + `by-assignee` together shows workload imbalance
- `finish-rates` is the KPI metric for management dashboards

## Error Recovery

| Error | Likely cause | Fix |
|---|---|---|
| `403` on `stats` / `overdue` | PAT missing `workspace:read` | Re-create PAT with required scopes |
| `INVALID_INPUT` on `trends` | Missing `--start`, `--end`, or `--period` | All three are required for this tool |
| Empty results | No tasks in the date range | Widen the date range |
| Slow response on `by-org` | Large org tree | Normal — org aggregation is expensive |
