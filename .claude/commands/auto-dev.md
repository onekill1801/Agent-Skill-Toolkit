# Auto Dev (full pipeline)

Run the **Auto Dev Orchestrator** from @.claude/skills/auto-dev/SKILL.md end to end:
**Intake → Plan → Implement → Test → Deliver**, in **checkpoint mode** (stop for approval at
`after_plan`, `before_mr`, `before_notify`).

**Argument:** Parse from slash command (e.g. `/auto-dev 6955` → task 6955, or a free-text
description for an adhoc request). If nothing was provided, ask the user.

## Steps

Follow @.claude/skills/auto-dev/cookbook/pipeline.md exactly. Use @.claude/skills/auto-dev/cookbook/intake.md to resolve the request.

1. **Intake** — read the task (`azure_devops.py get <id>` or aTask `search.py`/`tasks.py`), pick a `run_id`.
2. `run_log.py init <run_id> --task <id> --project <p> --type <bugfix|feature> --title "..."`
3. **Plan via Agent Debate** (`stage plan active`) — chạy `auto-dev/tools/debate_engine.py run
   --task <id> --desc "..."`: Dev/Architect/Moderator tranh biện → `temp/runs/<id>_plan.xml`
   (`<final_specification>`). Mặc định dùng **CLI bản subscription** (`--backend claude|cursor|agy`;
   cursor = binary `agent`; KHÔNG cần API key; `--dry-run` để thử). Bóc
   `<target_files>` cho bước Implement bằng
   `fork-terminal/tools/agent_parser.py`. `stage plan done` → **✋ checkpoint `after_plan`**
   (trình bày spec dạng Markdown sạch), get approval.
4. **Implement** — create branch, write code + tests in the project's `clone_dir`.
5. **Test gate** — `test_runner.py run --project <p> --kind test`. Retry-fix up to 3×.
   If still red → `stage test failed`, STOP, report. **Never deliver on red tests.**
6. **✋ checkpoint `before_mr`** → `gitlab_api.py create-mr ...`, self-review via `/review-mr`.
7. **✋ checkpoint `before_notify`** → `notifier.py mr-created ...`, update task state, `stage deliver done`.

All Python tools run from `.claude/skills/dev-automation/tools/` (use `python` on Windows).
If `./work/projects.json` does not exist yet, ask the user for the code directory and test command,
then pass `--cwd <dir> --cmd "<test cmd>"` to `test_runner.py` instead of `--project`.

## Related

- Resume an interrupted run: `python run_log.py get <run_id>` → continue from first non-done stage.
- List open runs: `python run_log.py list --open`.
