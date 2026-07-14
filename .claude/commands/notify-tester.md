# Notify Tester (Azure DevOps Comment)

Execute **Workflow 4** from @.claude/skills/dev-automation/SKILL.md.

**Arguments:** `/notify-tester <work_item_id> [dev_env_url]`

- **work_item_id** (required): Azure DevOps work item ID
- **dev_env_url** (optional): Dev environment URL for testers

## Steps

1. Read @.claude/skills/dev-automation/prompts/notify_tester_prompt.md
2. If needed, `python3 azure_devops.py get <work_item_id>` for acceptance criteria
3. `python3 notifier.py deploy-done <work_item_id> <dev_env_url>`

Run commands from `.claude/skills/dev-automation/tools/` (use `python` on Windows if needed).
