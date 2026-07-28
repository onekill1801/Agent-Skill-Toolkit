# New Product (greenfield scaffold)

Run the **New Product Scaffolder** from @.claude/skills/new-product/SKILL.md end to end:
**Intake → Clarify → Design → Scaffold → Verify → Deliver**, in checkpoint mode (stop for
approval at the design, before the first commit, and before any remote push).

**Argument:** a short product requirement, OR a path to a requirements file (e.g.
`/new-product ./REQUIREMENT.md`), OR nothing (then ask). **Long/detailed specs should
NOT be pasted into the argument** — put them in a file or paste them into the chat and
ingest with `intake.py build --name <n> --file <path>` (or `-` for stdin). An Azure/aTask
task can be piped in: `azure_devops.py get <id> | intake.py build --name <n> -`.

## Steps

Follow @.claude/skills/new-product/cookbook/workflow.md. Stacks: @.claude/skills/new-product/cookbook/stacks.md.

1. **Intake** — pick a short kebab-case `name`; ingest the requirement via
   `intake.py build --name <name> {--file <path> | --text "..." | -}` →
   `temp/products/<name>/{requirement.md,brief.md}`. Read `requirement.md` in full.
2. **Clarify** — resolve the STACK (`scaffold.py recipes` + `scaffold.py check <candidate>`),
   persistence, public surface, and GitLab-now-or-later. Keep to 2–4 questions.
3. **Design (✋)** — fill `temp/products/<name>/brief.md` (stack + entities; SPLIT capabilities
   into skeleton-scope vs deferred backlog). Present it; get approval before writing files.
4. **Scaffold** — `scaffold.py run <stack> --name <name> --dir work/<name> [opts]`, then add the
   minimal design-specific code + one test by editing files.
5. **Verify** — run the build + test `cmds` returned by `run`. Must be green. Show real output.
6. **Deliver (✋)** — `repo_init.py init work/<name>`; ask before `create-gitlab` + `push`
   (outward-facing); then `registry.py add <name> --clone-dir work/<name> --stack <stack> ...`.
7. **Hand off** — tell the user `/auto-dev "<feature> in <name>"` now builds features on it.

All Python tools run from `.claude/skills/new-product/tools/` (use `python` on Windows,
`python3` on macOS/Linux). GitLab creds flow through dev-automation `config.py` / `.env`.

## Guardrails

- Ask before every `[WRITE]` the first time — especially `create-gitlab` / `push`.
- Never claim a build/test passed without the real output.
- Keep the skeleton minimal; defer feature work to `/auto-dev`.
