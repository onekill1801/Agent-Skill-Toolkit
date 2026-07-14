# Purpose

Understand this codebase and report your understanding.

## Workflow

### Step 1: Read the project overview
1. @README.md

### Step 2: Fork Terminal Skill
2. @.claude/skills/fork-terminal/SKILL.md
3. @.claude/skills/fork-terminal/cookbook/cli-command.md
4. @.claude/skills/fork-terminal/cookbook/claude-code.md
5. @.claude/skills/fork-terminal/cookbook/codex-cli.md
6. @.claude/skills/fork-terminal/cookbook/gemini-cli.md
7. @.claude/skills/fork-terminal/cookbook/antigravity-cli.md
8. @.claude/skills/fork-terminal/tools/fork_terminal.py
9. @.claude/skills/fork-terminal/prompts/fork_summary_user_prompt.md

### Step 3: Dev Automation Skill
10. @.claude/skills/dev-automation/SKILL.md
11. @.claude/skills/dev-automation/tools/config.py
12. @.claude/skills/dev-automation/tools/azure_devops.py
13. @.claude/skills/dev-automation/tools/gitlab_api.py
14. @.claude/skills/dev-automation/tools/notifier.py
15. @.claude/skills/dev-automation/cookbook/review-merge-request.md
16. @.claude/skills/dev-automation/cookbook/fix-bug-workflow.md
17. @.claude/skills/dev-automation/cookbook/new-feature-workflow.md
18. @.claude/skills/dev-automation/cookbook/java-standards.md
19. @.claude/skills/dev-automation/prompts/task_analysis_prompt.md
20. @.claude/skills/dev-automation/prompts/code_review_prompt.md
21. @.claude/skills/dev-automation/prompts/notify_tester_prompt.md

### Step 4: aTask Automation Skill
22. @.claude/skills/atask-automation/SKILL.md
23. @.claude/skills/atask-automation/tools/config.py
24. @.claude/skills/atask-automation/tools/client.py
25. @.claude/skills/atask-automation/tools/tasks.py
26. @.claude/skills/atask-automation/tools/checklists.py
27. @.claude/skills/atask-automation/tools/projects.py
28. @.claude/skills/atask-automation/tools/search.py
29. @.claude/skills/atask-automation/tools/analytics.py

### Step 5: Skill Scaffold (import tools from other apps)
22. @.claude/skills/skill-scaffold/SKILL.md
23. @.claude/skills/skill-scaffold/prompts/extract_tools_prompt.md
24. @.claude/skills/skill-scaffold/prompts/tool_inventory_schema.md
25. @.claude/skills/skill-scaffold/prompts/design_skill_prompt.md
26. @.claude/skills/skill-scaffold/prompts/generate_skill_prompt.md
27. @.claude/skills/skill-scaffold/prompts/run_in_source_app_prompt.md

### Step 5: Slash commands
28. List every file in `.claude/commands/` and summarize what each `/command` does.

## Report

After reading, confirm:
- Available skills and when each triggers
- Available slash commands (`/prime`, `/list-tasks`, `/review-mr`, etc.)
- How to run Python tools (`cd` to `tools/`, `python3` vs `python`, `.env` + `SSL_VERIFY`)
- Platform notes for `fork_terminal.py` (Windows, macOS; Linux not yet implemented)
