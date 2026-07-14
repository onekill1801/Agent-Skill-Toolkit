---
name: Fork Terminal Skill
description: Fork a terminal session to a new terminal window. Use this when the user requests 'fork terminal' or 'create a new terminal' or 'new terminal: <command>' or 'fork session: <command>'.
---

# Purpose

Fork a terminal session to a new terminal window. Using one agentic coding tools or raw cli commands.
Follow the `Instructions`, execute the `Workflow`, based on the `Cookbook`.

## Variables

ENABLE_RAW_CLI_COMMANDS: true
ENABLE_GEMINI_CLI: true
ENABLE_CODEX_CLI: true
ENABLE_CLAUDE_CODE: true
ENABLE_ANTIGRAVITY_CLI: true
ENABLE_PARALLEL_ISOLATION: true
AGENTIC_CODING_TOOLS: claude-code, codex-cli, gemini-cli, antigravity-cli

## Instructions

- Based on the user's request, follow the `Cookbook` to determine which tool to use.

### Fork Summary User Prompts

- IF: The user requests a fork terminal with a summary. This ONLY works for our agentic coding tools `AGENTIC_CODING_TOOLS`. The tool MUST BE enabled as well.
- THEN: 
  - Read, and REPLACE the `.claude/skills/fork-terminal/prompts/fork_summary_user_prompt.md` with the history of the conversation between you and the user so far. 
  - Include the next users request in the `Next User Request` section.
  - This will be what you pass into the PROMPT parameter of the agentic coding tool.
  - IMPORTANT: To be clear, don't update the file directly, just read it, fill it out IN YOUR MEMORY and use it to craft a new prompt in the structure provided for the new fork agent.
  - Let's be super clear here, the fork_summary_user_prompt.md is a template for you to fill out IN YOUR MEMORY. Once you've filled it out, pass that prompt to the agentic coding tool.
  - XML Tags have been added to let you know exactly what you need to replace. You'll be replacing the <fill in the history here> and <fill in the next user request here> sections.
- EXAMPLES:
  - "fork terminal use claude code to <xyz> summarize work so far"
  - "spin up a new terminal request <xyz> using claude code include summary"
  - "create a new terminal to <xyz> with claude code with summary"

## Workflow

1. Understand the user's request.
2. READ: `.claude/skills/fork-terminal/tools/fork_terminal.py` to understand our tooling.
3. Follow the `Cookbook` to determine which tool to use.
4. Execute the `.claude/skills/fork-terminal/tools/fork_terminal.py: fork_terminal(command: str)` tool.

### Parallel Isolated Agents (worktree)

- IF: The user wants to run MULTIPLE agents in parallel on the same repo without them
  clobbering each other (shared checkout, same port, same DB) AND `ENABLE_PARALLEL_ISOLATION`
  is true. Trigger phrases: "chạy nhiều agent song song", "fork agent cách ly", "parallel agents",
  "isolated workspace", "spawn agent for task <id>".
- THEN: Read and execute: `.claude/skills/fork-terminal/cookbook/parallel-agents.md`
- The orchestrator chains three stdlib tools (no `fork_terminal` call by hand needed):
  1. `tools/worktree_manager.py` — `create_agent_workspace(project_path, task_id, branch_name)`
     makes a sibling worktree `{project}-agent-{task_id}` on branch `feature/task-{task_id}`
     (checks out the branch if it already exists). `remove_agent_workspace(...)` cleans it up.
  2. `tools/runtime_isolator.py` — `isolate_environment(workspace_path, task_id)` rewrites
     PORT/`server.port` to a per-task port in [8000,9000] and DB name to `{db}_task_{task_id}`
     across `.env` / `application.properties` / `application.yml`. Idempotent.
  3. `tools/spawn_isolated_agent.py` — ties them together AND forks the terminal with
     **CWD set to the isolated worktree** (not the source repo). One command:
     `python spawn_isolated_agent.py spawn --project <repo> --task <id> --cmd "<cli>" [--branch b] [--dry-run]`
     Cleanup: `python spawn_isolated_agent.py cleanup --project <repo> --task <id> [--force]`.
- GUARDRAIL: confirm with the user before spawning (it creates branches + windows) and use
  `--dry-run` first to preview the worktree + isolation without launching a terminal.
- EXAMPLES:
  - "fork 3 claude agents song song cho task 5001/5002/5003 trên repo atask"
  - "spawn an isolated claude agent for task 777 on the atask repo"

### Parsing dữ liệu Agent↔Agent (HTML/XML)

- IF: Cần bóc tách dữ liệu có cấu trúc do một agent sinh ra cho agent/tool khác (Plan,
  Target Files, Error Log, lời thoại tranh luận) — giao tiếp nội bộ dùng **thẻ HTML/XML
  nghiêm ngặt**, KHÔNG Markdown (xem `.claude/skills/auto-dev/prompts/SYSTEM_PROMPT.md`).
- THEN: dùng `tools/agent_parser.py` (regex stdlib, không cần BeautifulSoup):
  - `extract_tag_content(text, tag)` — nội dung một thẻ.
  - `extract_list_items(text, parent, child)` — danh sách item trong thẻ cha/con.
  - CLI: `python tools/agent_parser.py list target_files file --file plan.xml`
- GUARDRAIL: thẻ chỉ cho giao tiếp ngầm; đầu ra cuối cho người dùng vẫn Markdown sạch.

## Cookbook

### Raw CLI Commands

- IF: The user requests a non-agentic coding tool AND `ENABLE_RAW_CLI_COMMANDS` is true.
- THEN: Read and execute: `.claude/skills/fork-terminal/cookbook/cli-command.md` 
- EXAMPLES:
  - "Create a new terminal to <xyz> with ffmpeg"
  - "Create a new terminal to <xyz> with curl"
  - "Create a new terminal to <xyz> with python"

### Claude Code

- IF: The user requests a claude code agent to execute the command AND `ENABLE_CLAUDE_CODE` is true.
- THEN: Read and execute: `.claude/skills/fork-terminal/cookbook/claude-code.md`
- EXAMPLES:
  - "fork terminal use claude code to <xyz>"
  - "spin up a new terminal request <xyz> using claude code"
  - "create a new terminal to <xyz> with claude code"

### Codex CLI

- IF: The user requests a codex CLI agent to execute the command AND `ENABLE_CODEX_CLI` is true.
- THEN: Read and execute: `.claude/skills/fork-terminal/cookbook/codex-cli.md`
- EXAMPLES:
  - "fork terminal use codex to <xyz>"
  - "spin up a new terminal request <xyz> using codex"
  - "create a new terminal to <xyz> with codex"

### Gemini CLI

- IF: The user requests a gemini CLI agent to execute the command AND `ENABLE_GEMINI_CLI` is true.
- THEN: Read and execute: `.claude/skills/fork-terminal/cookbook/gemini-cli.md`
- EXAMPLES:
  - "fork terminal use gemini to <xyz>"
  - "spin up a new terminal request <xyz> with gemini"
  - "create a new terminal to <xyz> using gemini"

### Antigravity CLI

- IF: The user requests an antigravity (agy) agent to execute the command AND `ENABLE_ANTIGRAVITY_CLI` is true.
- THEN: Read and execute: `.claude/skills/fork-terminal/cookbook/antigravity-cli.md`
- EXAMPLES:
  - "fork terminal use antigravity to <xyz>"
  - "fork terminal use agy to <xyz>"
  - "create a new terminal to <xyz> with antigravity"
