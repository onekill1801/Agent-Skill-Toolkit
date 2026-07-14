# Prompt: Run inside your application repo (copy-paste)

Use this prompt **in Claude Code / Cursor opened on your application repository** (not on fork-repository-skill). It produces a portable artifact you bring back here.

---

## Copy from here

```
You are helping export agent tools for porting to a Claude Code skill.

1. Find every tool the agent can call (functions, MCP descriptors, API routes, CLI commands).
2. For each tool document:
   - id (snake_case)
   - name, description
   - parameters (name, type, required, description)
   - return value
   - side_effects: read_only | writes_data | external_api | spawns_process
   - source file and symbol
   - environment variables used (names only, no values)
3. Group tools into 2–6 workflows with trigger phrases a user would say.
4. Output a single YAML file matching this structure:

---
source:
  type: <python|mcp|openapi|typescript|manual>
  path_or_url: "<this repo path>"
  app_name: "<name>"
tools:
  - id: ...
    name: ...
    description: ...
    parameters: [...]
    ...
workflows:
  - id: ...
    trigger_phrases: [...]
proposed_skill:
  folder_name: kebab-case-name
  display_name: ...
  one_line_purpose: ...
env_vars:
  - name: ...
    secret: true|false
gaps: [...]
---

Save the YAML to a file named `tool-export.yaml` in the repo root (or print it in full if file write is not allowed).

Do not include API keys, passwords, or PAT tokens.
```

---

## After export

1. Copy `tool-export.yaml` to `fork-repository-skill/temp/tool-inventory.yaml`
2. Open `fork-repository-skill` in Claude Code
3. Run `/design-skill` then `/scaffold-skill`
