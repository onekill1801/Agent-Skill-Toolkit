# Tool Inventory Schema

Use this schema for **`temp/tool-inventory.yaml`** after extracting tools from the source application.

```yaml
# Metadata
source:
  type: python | mcp | openapi | typescript | manual
  path_or_url: "<repo path, MCP server name, or spec file>"
  app_name: "<human-readable app name>"
  extracted_at: "<ISO date>"

# New skill target
proposed_skill:
  folder_name: "<kebab-case>"          # e.g. my-app-automation
  display_name: "<Title Case Name>"
  one_line_purpose: "<what the skill does>"

# Environment (placeholders for .env.sample — NO real secrets)
env_vars:
  - name: EXAMPLE_API_URL
    description: Base URL for the service
    required: true
  - name: EXAMPLE_API_TOKEN
    description: Bearer or API token
    required: true
    secret: true

# Flat list of tools
tools:
  - id: unique_tool_id              # snake_case
    name: "Human readable name"
    description: "<what the agent uses this for>"
    source:
      file: "<path in source app>"
      symbol: "<function/class/MCP tool name>"
      line_hint: "<optional line number>"
    parameters:
      - name: param_name
        type: string | number | boolean | object | array
        required: true
        description: "<meaning>"
    returns:
      type: object | string | void
      description: "<what success looks like>"
    side_effects:
      - read_only | writes_data | external_api | spawns_process
    errors:
      - code: "<HTTP or app error>"
        meaning: "<when it happens>"
    cli_equivalent: |               # optional: how to call from shell
      python tools/example.py action arg1

# Suggested workflows (group tools)
workflows:
  - id: workflow_id
    name: "Workflow title"
    trigger_phrases:
      - "example phrase"
    steps:
      - tool_id: unique_tool_id
        note: "when/why in this step"
    cookbook_file: "<proposed-kebab-name>.md"

# Gaps / manual follow-up
gaps:
  - "<tool needs auth flow not visible in code>"
  - "<tool is UI-only, needs wrapper script>"
```

## Validation checklist

- [ ] Every `tools[].id` is unique snake_case
- [ ] Every workflow step references a valid `tool_id`
- [ ] No secret values in the YAML — only env var **names**
- [ ] `cli_equivalent` is testable from `tools/` directory
- [ ] Side effects marked for tools that mutate production data
