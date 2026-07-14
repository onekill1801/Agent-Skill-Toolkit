# aTask: Create Task

Create a new task in aTask.

## Arguments

`$ARGUMENTS` — `<task name> [list_id]`. Examples:
- `/atask-create Fix login timeout` → prompts for list ID
- `/atask-create "Deploy v2.1" list-abc123` → creates immediately

## Steps

1. Read `@.claude/skills/atask-automation/SKILL.md`
2. Read `@.claude/skills/atask-automation/cookbook/task-crud.md`
3. Read `@.claude/skills/atask-automation/tools/tasks.py`
4. Parse `$ARGUMENTS` for task name and optional list ID
5. If list ID is missing:
   - Run `python3 projects.py my-lists` (cd to `tools/` first)
   - Show available lists; ask user to pick one
6. Ask user for optional fields: priority, due date, description
   - **Priority** là số `1-4`: `1`=Khẩn cấp, `2`=Cao, `3`=Trung bình, `4`=Thấp (mặc định để trống nếu người dùng không nêu). Có thể nhập nhãn URGENT/HIGH/MEDIUM/LOW — tool tự map sang số.
7. Run `python3 tasks.py create --name "..." --list <list_id> [optional flags]`
8. Show created task ID and name; offer to add checklist items or comments
