# Extract Tools from External Application

Execute **Phase 1** from @.claude/skills/skill-scaffold/SKILL.md.

## Input

Parse from user message:
- **source_type:** python | mcp | openapi | typescript | manual
- **source_location:** repo path, MCP server folder, OpenAPI path, or pasted JSON

If missing, ask before scanning.

## Steps

1. Read @.claude/skills/skill-scaffold/prompts/extract_tools_prompt.md
2. Read @.claude/skills/skill-scaffold/prompts/tool_inventory_schema.md
3. Follow the matching cookbook:
   - python → @.claude/skills/skill-scaffold/cookbook/from-python-app.md
   - mcp → @.claude/skills/skill-scaffold/cookbook/from-mcp-server.md
   - openapi → @.claude/skills/skill-scaffold/cookbook/from-openapi.md
   - typescript → @.claude/skills/skill-scaffold/cookbook/from-typescript-sdk.md
   - manual → @.claude/skills/skill-scaffold/cookbook/from-manual-inventory.md
4. Write @temp/tool-inventory.yaml (create `temp/` if needed)
5. Summarize: tool count, proposed skill name, workflows, gaps

Do not generate SKILL.md files in this phase unless the user asks for full scaffold.
