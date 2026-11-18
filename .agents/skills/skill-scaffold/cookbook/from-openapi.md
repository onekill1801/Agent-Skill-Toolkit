# Extract tools from OpenAPI / REST

## When

The application exposes REST APIs documented by OpenAPI 3.x or Swagger 2.

## Input

- `openapi.yaml` / `swagger.json` path, or URL (fetch once, save to `temp/openapi-snapshot.json`)

## Mapping

| OpenAPI | inventory |
|---------|-----------|
| `operationId` or `method + path` | `tools[].id` |
| `summary` + `description` | `description` |
| `parameters` + `requestBody.schema` | `parameters` |
| `tags[0]` | workflow grouping |

## Tool boundaries

- One tool per **operation** (GET/POST/... + path).
- Skip pure health checks (`/health`, `/ping`) unless user asks.
- Mark `side_effects: writes_data` for POST, PUT, PATCH, DELETE.

## Phase 3 wrapper

Generate `tools/<api>.py` using urllib + paths from spec; base URL from env var.

## Prompt to run

`source_type: openapi`, `source_location: <path or url>`.
