# List My Azure DevOps Tasks

List work items assigned to the authenticated user.

1. Read @.claude/skills/dev-automation/SKILL.md (routing: list tasks)
2. From `.claude/skills/dev-automation/tools/`, run:
   - `python3 azure_devops.py list`
3. Present results in a readable table (ID, title, state, type) grouped by state.
4. Offer next actions: `/read-task <id>`, `/fix-bug <id>`, `/implement-feature <id>`, `/review-mr <iid>`

Use `python` instead of `python3` on Windows if `python3` is not available.
