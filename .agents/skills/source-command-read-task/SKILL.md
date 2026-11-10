---
name: "source-command-read-task"
description: "Migrated source command `read-task`"
---

# source-command-read-task

Use this skill when the user asks to run the migrated source command `read-task`.

## Command Template

# Read Azure DevOps Task

Fetch full details for one work item.

**Task ID:** Parse from slash command arguments (e.g. `/read-task 6955` → `6955`). If no ID was provided, ask the user.

1. From `.Codex/skills/dev-automation/tools/`, run:
   - `python3 azure_devops.py get <task_id>`
2. Summarize: title, type, state, description, acceptance criteria, assignee, relations, URL.
3. Suggest the appropriate next command:
   - Bug → `/fix-bug <id>`
   - User Story / Task / Feature → `/implement-feature <id>`

Use `python` instead of `python3` on Windows if `python3` is not available.
