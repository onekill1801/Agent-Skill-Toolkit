# Todos / Tasks Overview

## Purpose
Browse TChat todos (tasks created from chat messages): assigned to me, by me,
important, and expired.

## Prerequisites
- `.env` configured; `python config.py` OK.
- `cd .claude/skills/tchat-automation/tools`.

## Steps

1. **Tasks assigned to me:**
   ```
   python todos.py list --type TO_ME --limit 30
   ```
2. **Tasks I created / assigned to others:**
   ```
   python todos.py list --type BY_ME
   ```
3. **Important:**
   ```
   python todos.py list --type IMPORTANT
   ```
4. **Expired in a specific conversation:**
   ```
   python todos.py list --group <group_id> --filter EXPIRED
   ```
5. **How many expired overall:**
   ```
   python todos.py count-expired
   ```

## Notes
- Combine with `groups.py list` to resolve `groupId` → conversation name.
- Creating todos is **not** wired (POST body unverified) — read-only by design.

## Error recovery
| Symptom | Fix |
|---|---|
| `401` | refresh `FCHAT_BEARER_TOKEN` |
| `429` | back off, honor Retry-After |
| empty list | no todos match the filter — try without `--filter` |
