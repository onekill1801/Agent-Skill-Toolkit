# Extract tools from TypeScript / SDK tool registry

## When

Tools are registered in TypeScript (Cursor SDK, Vercel AI SDK, LangChain, custom).

## Search patterns

```text
rg "tools:\\s*\\[" --glob "*.{ts,tsx}"
rg "z\\.object\\(|tool\\(|createTool" --glob "*.{ts,tsx}"
rg "parameters:\\s*\\{" --glob "*.{ts,tsx}"
```

## Extract

- Tool `name`, `description`, Zod/JSON schema → inventory `parameters`
- Import path → `source.file`
- If implementation calls `fetch()` or SDK client, trace to HTTP method + path for `cli_equivalent` planning

## Phase 3 note

Python skill cannot run TS directly — generate Python urllib wrappers from traced HTTP contracts, or document `npx tsx scripts/run-tool.ts` in `gaps`.

## Prompt to run

`source_type: typescript`, `source_location: <repo path>`.
