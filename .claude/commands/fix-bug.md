# Fix Bug (Azure DevOps Task)

Execute **Workflow 2** from @.claude/skills/dev-automation/SKILL.md.

**Task ID:** Parse from slash command arguments (e.g. `/fix-bug 6955` → `6955`). If no ID was provided, ask the user.

## Prerequisites

- Run from the **Java project repository** (aTask), not only this skills repo, if the goal is to change application code.
- Copy `.env` and `.claude/skills/dev-automation/` into that project if needed.
- Confirm with the user before creating branches or merge requests if the environment is production-sensitive.

## Steps

Follow @.claude/skills/dev-automation/cookbook/fix-bug-workflow.md end to end:

1. `python3 azure_devops.py get <task_id>`
2. Analyze using @.claude/skills/dev-automation/prompts/task_analysis_prompt.md
3. `python3 notifier.py started <task_id> <branch_name>`
4. `python3 azure_devops.py state <task_id> Active`
5. `python3 gitlab_api.py create-branch "bugfix/<task_id>-<short-desc>" develop`
6. Implement fix per @.claude/skills/dev-automation/cookbook/java-standards.md
7. Commit, push, then `python3 gitlab_api.py create-mr ...`
8. `python3 notifier.py mr-created <task_id> <mr_url>`
9. Self-review via Workflow 1 (or `/review-mr <mr_iid>`)

All Python commands run from `.claude/skills/dev-automation/tools/` (use `python` on Windows if needed).
