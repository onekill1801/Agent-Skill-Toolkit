# Scaffold New Skill from Inventory + Design

Execute **Phase 3** from @.claude/skills/skill-scaffold/SKILL.md.

## Prerequisites

- @temp/tool-inventory.yaml
- @temp/skill-design.md (user approved)

## Steps

1. Read @.claude/skills/skill-scaffold/prompts/generate_skill_prompt.md
2. Read templates in @.claude/skills/skill-scaffold/templates/
3. Generate `.claude/skills/<folder_name>/` per design
4. Update `.env.sample` with placeholder env vars only
5. Add slash commands if listed in design
6. Update @.claude/commands/prime.md and @.claude/commands/all_skills.md
7. Run a quick smoke test on generated `tools/*.py` (--help or config validate)
8. Report files created and example triggers
