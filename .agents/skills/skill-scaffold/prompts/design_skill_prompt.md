# Design Skill Prompt

Design a new Claude Code skill from **`temp/tool-inventory.yaml`**.

## Prerequisites

- `temp/tool-inventory.yaml` exists and passed schema validation.
- Read reference: @.claude/skills/dev-automation/SKILL.md

## Your task

Produce **`temp/skill-design.md`** with:

```markdown
# Skill Design: <display_name>

## Folder
`.claude/skills/<folder_name>/`

## Description block (for SKILL.md frontmatter)
<2-4 lines, trigger phrases included — copy-paste ready>

## Feature flags
```yaml
ENABLE_<AREA>: true
```

## Tool mapping

| Inventory tool id | Generated file | Wrapper strategy |
|-----------------|----------------|------------------|
| ... | tools/foo.py | thin CLI / reuse source / HTTP only |

## Workflows

### Workflow 1: <name>
- **Triggers:** ...
- **Cookbook:** cookbook/<file>.md
- **Steps:** (ordered, reference tool ids)
- **Slash command:** /command-name (optional)

(repeat per workflow)

## .env.sample additions
```
VAR=name   # description
```

## Slash commands to add
| Command | Maps to workflow |
|---------|------------------|

## Risks & guardrails
- Tools that need confirmation before run
- Production vs dev endpoints

## prime.md lines to append
<numbered @ paths for new files>
```

## Design principles

1. **One workflow = one cookbook file** (like `fix-bug-workflow.md`).
2. **Bundle tools** into few Python files by domain (not one file per tool unless huge).
3. **CLI-first:** every tool must be invocable as `python3 tools/x.py <subcommand>`.
4. **Triggers** must be natural Vietnamese/English phrases the user already uses.
5. Prefer **read-only workflows** first (list, get) before write workflows (create, delete).

## Output

Write only `temp/skill-design.md`. Ask user to approve before Phase 3 generation.
