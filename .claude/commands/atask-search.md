# aTask: Search Tasks

Search for tasks in aTask using Elasticsearch-powered full-text and filter search.

## Arguments

`$ARGUMENTS` — optional free-text query. Examples:
- `/atask-search payment timeout` → search all tasks matching "payment timeout"
- `/atask-search` (no args) → show my assigned tasks

## Steps

1. Read `@.claude/skills/atask-automation/SKILL.md` for routing rules
2. Read `@.claude/skills/atask-automation/cookbook/task-lookup.md`
3. Read `@.claude/skills/atask-automation/tools/search.py`
4. If `$ARGUMENTS` is empty → run `python3 search.py my-tasks`
5. Otherwise → run `python3 search.py tasks --query "$ARGUMENTS"`
6. Display results as a formatted table: ID | Name | Status | Priority | Due Date
7. Ask: "Want to drill into any of these tasks?"
