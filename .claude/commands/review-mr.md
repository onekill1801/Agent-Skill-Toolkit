# Review Merge Request

Execute **Workflow 1** from @.claude/skills/dev-automation/SKILL.md.

**MR IID:** Parse from slash command arguments (e.g. `/review-mr 524` → `524`). If no number was provided, ask the user.

## Steps

1. Read @.claude/skills/dev-automation/cookbook/review-merge-request.md
2. Read @.claude/skills/dev-automation/cookbook/java-standards.md
3. Read @.claude/skills/dev-automation/prompts/code_review_prompt.md
4. From `.claude/skills/dev-automation/tools/`, run:
   - `python3 gitlab_api.py mr-changes <mr_iid>` (use `python` on Windows if `python3` is unavailable)
   - `python3 gitlab_api.py mr-discussions <mr_iid>`
5. Analyze each changed file against the review checklist
6. Post review: `python3 gitlab_api.py mr-comment <mr_iid> "<review body>"`
7. If an Azure DevOps work item is linked, notify: `python3 notifier.py review-done <work_item_id> <mr_url> "<summary>"`

Report the review verdict (APPROVE / REQUEST_CHANGES / COMMENT) to the user.
