# Extract tools from an MCP server

## When

Tools are defined as MCP tool descriptors (JSON) or discovered via MCP `tools/list`.

## Locations

- Cursor/Claude: `mcps/<server>/tools/*.json`
- Custom server: repo `src/tools/`, manifest in server README

## Per descriptor file

Extract:

| JSON field | inventory field |
|------------|-----------------|
| `name` | `id` (normalize to snake_case) |
| `description` | `description` |
| `arguments` / `inputSchema.properties` | `parameters` |
| required array in schema | `required: true` on params |

## Auth

- If server `STATUS.md` or instructions mention `mcp_auth`, add to `gaps` — skill may need env token instead of MCP at runtime.
- Claude Code skill tools run as **Python CLI**, not in-process MCP — plan HTTP or REST equivalent if MCP wraps an API.

## Prompt to run

`source_type: mcp`, `source_location: mcps/<server-name>` or path to server repo.
