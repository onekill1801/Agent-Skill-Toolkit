# Extract tools from manual input

## When

User pastes tool definitions (JSON Schema, Anthropic tool format, or a spreadsheet).

## Steps

1. Paste into user message or save as `temp/source-tools.json`.
2. Validate JSON.
3. Map each entry to `tool_inventory_schema.md` fields.
4. Ask user for missing: `side_effects`, env vars, example CLI.
5. Write `temp/tool-inventory.yaml`.

## Anthropic tool format mapping

```json
{
  "name": "get_weather",
  "description": "...",
  "input_schema": { "type": "object", "properties": { ... } }
}
```

→ `id: get_weather`, `parameters` from `properties` + `required`.

## Prompt to run

`source_type: manual`, paste content in `source_location: inline`.
