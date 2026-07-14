# Implement Feature (Azure DevOps Task)

Execute **Workflow 3** from @.claude/skills/dev-automation/SKILL.md.

**Task ID:** Parse from slash command arguments (e.g. `/implement-feature 7019` → `7019`). If no ID was provided, ask the user.

## Prerequisites

- Run from the **Java project repository** where the feature should be implemented.
- Confirm scope with the user before creating branches or merge requests.

## Steps

Follow @.claude/skills/dev-automation/cookbook/new-feature-workflow.md end to end:

1. `python3 azure_devops.py get <task_id>`
2. Plan using @.claude/skills/dev-automation/prompts/task_analysis_prompt.md
3. `python3 notifier.py started <task_id> <branch_name>`
4. `python3 azure_devops.py state <task_id> Active`
5. `python3 gitlab_api.py create-branch "feature/<task_id>-<short-desc>" develop`
6. Implement per @.claude/skills/dev-automation/cookbook/java-standards.md
7. Create MR and `python3 notifier.py mr-created <task_id> <mr_url>`
8. Self-review the MR

All Python commands run from `.claude/skills/dev-automation/tools/` (use `python` on Windows if needed).
