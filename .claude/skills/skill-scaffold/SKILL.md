---
name: Skill Scaffold
description: >
  Extract agent tools from an external application and scaffold a new Claude Code skill
  in this repo (SKILL.md, tools/, cookbook/, prompts/, slash commands).
  Trigger phrases: "extract tools", "import tools", "scaffold skill", "create skill from app",
  "generate skill from tools", "port tools to skill".
---

# Skill Scaffold

## Purpose

Turn **agent tools** defined in your application into a **Claude Code skill** compatible with this repository's layout (`fork-terminal`, `dev-automation` pattern).

## When to use

| Source type | Cookbook |
|-------------|----------|
| Python modules / CLI scripts | `cookbook/from-python-app.md` |
| MCP server tool descriptors | `cookbook/from-mcp-server.md` |
| OpenAPI / REST API | `cookbook/from-openapi.md` |
| TypeScript / SDK tool registry | `cookbook/from-typescript-sdk.md` |
| Manual list (user provides JSON) | `cookbook/from-manual-inventory.md` |

## Workflow (3 phases)

### Phase 1 — Extract tool inventory

1. User provides: path to app repo, URL to MCP, OpenAPI spec path, or pasted tool JSON.
2. Read `prompts/extract_tools_prompt.md` and `prompts/tool_inventory_schema.md`.
3. Scan the source and produce **`temp/tool-inventory.yaml`** (do not commit secrets).

### Phase 2 — Design the skill

1. Read `prompts/design_skill_prompt.md`.
2. Group tools into workflows; name the skill (kebab-case folder name).
3. Produce **`temp/skill-design.md`** (workflows, triggers, env vars, risks).

### Phase 3 — Generate files in this repo

1. Read `prompts/generate_skill_prompt.md` and `templates/*`.
2. Create under `.claude/skills/<skill-name>/`:
   - `SKILL.md`
   - `tools/*.py` (or document HTTP-only tools)
   - `cookbook/*.md` per workflow
   - `prompts/*.md` if needed
3. Optionally add `.claude/commands/<command>.md` slash commands.
4. Update `.env.sample` with new variables (placeholders only).
5. Update `.claude/commands/prime.md` and `all_skills.md` if user confirms.

## Output rules

- **Never** copy API keys or secrets into generated files — use `.env` placeholders.
- Prefer **Python stdlib** wrappers in `tools/` (same as `dev-automation`).
- Match naming: `bugfix/<id>-desc` style only for git workflows; skill folder = `kebab-case`.
- Generated skill must be **runnable on Windows, Linux, macOS** (`python3` / `python`, forward slashes in docs).

## Reference skills (read before generating)

- @.claude/skills/dev-automation/SKILL.md — multi-tool + workflows + slash commands
- @.claude/skills/fork-terminal/SKILL.md — single tool + cookbook routing

## Slash commands

| Command | Action |
|---------|--------|
| `/extract-tools` | Phase 1 only → `temp/tool-inventory.yaml` |
| `/design-skill` | Phase 2 from inventory → `temp/skill-design.md` |
| `/scaffold-skill` | Phase 3 full generation (needs inventory + design) |
