# Extract Tools Prompt

You are extracting **agent tools** from a user's application so they can be ported into a Claude Code skill in `fork-repository-skill`.

## Input (fill from user message)

```yaml
source_type: <python | mcp | openapi | typescript | manual>
source_location: <path, URL, server name, or pasted JSON>
focus: <all tools | only namespace X | only tools matching pattern>
app_context: <one paragraph: what the app does>
```

## Your task

1. **Discover** all tools the agent can call:
   - Search for: `tools`, `register_tool`, `@tool`, `ToolDefinition`, `function_declarations`, MCP `tools/*.json`, OpenAPI `paths`, CLI subcommands.
2. **Read** implementation (not only names): parameters, defaults, auth, HTTP method, idempotency.
3. **Normalize** each tool into `prompts/tool_inventory_schema.md`.
4. **Write** output to `temp/tool-inventory.yaml` in the workspace (create `temp/` if missing).

## Extraction rules by source type

### Python application

- Find modules under `tools/`, `agents/`, `handlers/`, or `*_tool.py`.
- For each public function used as an agent tool, capture: signature, docstring, env vars read (`os.environ`, `config`), HTTP calls (`requests`, `urllib`, `httpx`).
- If tools are only exposed via FastAPI/Flask routes, map route → logical tool id.
- Note `if __name__ == "__main__"` CLI patterns for wrapper scripts.

### MCP server

- Read tool descriptor JSON under `mcps/<server>/tools/` or server startup config.
- Map `name`, `description`, `inputSchema.properties` → inventory parameters.
- Note `server` identifier and required auth (`mcp_auth`).

### OpenAPI / REST

- Each meaningful `POST/PUT/PATCH/DELETE` (and key `GET`) → one tool.
- Group by tag; propose workflows per tag.
- Auth: header name only (e.g. `Authorization`), never example tokens.

### TypeScript / SDK

- Find `tools: [...]`, `createTool`, zod schemas, or `@anthropic-ai/sdk` tool definitions.
- Resolve types to JSON-schema-like parameters.

### Manual

- User pasted JSON/YAML: validate and enrich with missing `side_effects` and `cli_equivalent`.

## Quality bar

| Good | Bad |
|------|-----|
| "Creates GitLab MR via POST /projects/:id/merge_requests" | "gitlab tool" |
| Parameters with types and required flags | Empty parameters |
| `side_effects: writes_data` on POST | Omitting side effects |

## Deliverables

1. **`temp/tool-inventory.yaml`** — complete per schema
2. **Short summary** to user: tool count, proposed skill name, top 3 workflows, blockers in `gaps`

Do **not** generate SKILL.md yet unless the user also asked for Phase 3.
