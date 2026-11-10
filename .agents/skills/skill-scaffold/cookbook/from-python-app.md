# Extract tools from a Python application

## When

Source is a Python repo with agent tools as functions, classes, or CLI entry points.

## Search patterns

```text
rg -l "def .*tool|@tool|register_tool|ToolDefinition|BaseTool" <repo>
rg "if __name__" --glob "*.py"
glob: **/tools/**/*.py, **/agents/**/*.py
```

## Read order

1. Main agent entry (`agent.py`, `main.py`, `runner.py`)
2. Tool registry or dict mapping tool name → callable
3. Each tool module — signature, docstring, dependencies
4. `config.py`, `.env.example`, `settings.py` for env var names

## Map to inventory

| Source | inventory field |
|--------|-----------------|
| Function name | `tools[].id` (snake_case) |
| Docstring first line | `description` |
| `inspect.signature` / type hints | `parameters` |
| `os.getenv("X")` | add `X` to `env_vars` |
| subprocess / httpx call | `cli_equivalent` or note in `gaps` |

## Wrapper strategy for Phase 3

- **Thin wrapper:** new `tools/<domain>.py` calls same HTTP as original via urllib.
- **Subprocess:** `subprocess.run([sys.executable, "-m", "app.tools.foo", ...])` if app exposes CLI.
- **Copy:** only if license allows and code is small — prefer thin wrapper.

## Prompt to run

Fill `extract_tools_prompt.md` with `source_type: python` and `source_location: <absolute path>`.
