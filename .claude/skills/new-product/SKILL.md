---
name: New Product Scaffolder
description: >
  Create a BRAND-NEW product/project from a requirement description: clarify scope,
  design an architecture, scaffold a skeleton for a chosen tech stack, verify it
  builds, make the first commit, and register it so `auto-dev` can take over feature
  work. This is the greenfield counterpart to `auto-dev` (which works on EXISTING
  repos). Trigger phrases: "tạo sản phẩm mới", "tạo project mới", "dựng app mới",
  "khởi tạo dự án", "new product", "new project", "scaffold a new app", "bootstrap
  a project", "create a project from scratch", "start a greenfield project".
---

# New Product Scaffolder

Takes a free-text product requirement and drives it through
**Intake → Clarify → Design → Scaffold → Verify → Deliver**, ending with a
committed skeleton registered in `work/projects.json`. From that point on,
`/auto-dev` handles feature-by-feature development on the new repo.

> Scope boundary: this skill produces the **first working skeleton** (structure,
> dependencies, health endpoint, a green build). It does **not** implement the full
> feature set — that is `auto-dev`'s job. Keep the scaffold minimal and correct.

## Tools

All under `tools/` (this skill). Stdlib only; each returns JSON, errors as
`{"error": true, "message": ...}`. Read a tool's source before first use.

| Tool | Role |
|------|------|
| `intake.py build --name <n>` | **Ingest a long requirement** from `--file` / `--text` / STDIN → normalized `requirement.md` + a `brief.md` template with auto-detected capabilities. Use this instead of cramming a detailed spec into the slash-command argument |
| `scaffold.py recipes` | List supported stacks + scaffolding method |
| `scaffold.py check <stack>` | Is this stack scaffoldable here? (probes CLI availability) |
| `scaffold.py run <stack> --name --dir` | **[WRITE]** generate the skeleton. Java/Spring via `start.spring.io` (no CLI); Next/Vite via `npx`/`npm`; Express/FastAPI/CLI/static via offline embedded templates |
| `repo_init.py init <dir>` | **[WRITE]** `git init` + `.gitignore` + first commit (pre-checks git identity) |
| `repo_init.py create-gitlab <name>` | **[WRITE → remote]** create a GitLab project via API (reuses dev-automation `config.py`) |
| `repo_init.py push <dir> <url>` | **[WRITE → remote]** add origin + push |
| `registry.py add <name> --clone-dir ...` | **[WRITE]** register the new project in `work/projects.json` (hand-off to auto-dev) |
| `registry.py list` / `get` / `remove` | inspect / clean up the registry |
| `../dev-automation/tools/doctor.py` | which stack CLIs are installed on this machine |

## The pipeline

```
Intake ──> Clarify ──> [✋ approve design] ──> Scaffold ──> Verify (build/test)
   ──> [✋ before commit] ──> git init + commit ──> [✋ before remote] ──> GitLab + push
   ──> register in projects.json ──> hand off to /auto-dev
```

### Steps

1. **Intake.** Pick a short kebab-case product `name` (also the `work/<name>/` dir and
   GitLab path leaf). Then capture the requirement — **do not force a long spec through
   the slash-command argument.** Ingest it from whichever source the user has:
   - **A file** (`REQUIREMENT.md`, a `.docx` exported to text, etc.):
     `intake.py build --name <name> --file <path>`
   - **Pasted / piped text** (long paste, or an Azure/aTask task piped through):
     `... | intake.py build --name <name> -` (e.g. pipe `azure_devops.py get <id>` or
     aTask `tasks.py get-detail <id>`)
   - **A short one-liner:** `intake.py build --name <name> --text "..."`

   This writes `temp/products/<name>/requirement.md` (verbatim) + `brief.md` (template
   with auto-detected capability bullets). **Read `requirement.md` in full** — a long
   spec is exactly when you must not skim.

2. **Clarify.** Ask only what changes the scaffold. Always resolve the **stack**
   here (the user has NOT pre-picked one) — run `scaffold.py recipes`, then
   `scaffold.py check <candidate>` to confirm it's buildable on this machine, and
   propose one. Other blocking questions: persistence (DB engine?), public
   surface (REST API / web UI / CLI / library?), and whether it goes to GitLab now
   or stays local. Keep it to 2–4 questions.

3. **Design (✋ checkpoint).** Fill in `temp/products/<name>/brief.md` (created at
   Intake): chosen stack + why, data/entities, and — the key step for a long spec —
   **split the capabilities into `Skeleton scope` (build now, minimal) vs `Deferred
   backlog` (one feature per future `/auto-dev` run).** A detailed requirement almost
   always describes far more than the first skeleton should contain; the backlog is
   how the rest gets built without bloating the scaffold. **Present the brief and get
   approval before writing any files.**

4. **Scaffold.** `scaffold.py run <stack> --name <name> --dir work/<name> [opts]`.
   For `java-spring` pass `--deps`/`--group`/`--java-version`. If a `cli` stack's
   generator is missing (`check` said not ready), either install it or fall back to
   an offline stack, or hand-write the skeleton — never pretend it ran. After the
   generator, add the minimal design-specific code (health check, one endpoint,
   one test) by editing files directly. Follow `../dev-automation/cookbook/java-standards.md`
   for Java.

5. **Verify.** Run the stack's build + test command (from `scaffold.py run`'s
   `cmds`). A greenfield skeleton must build clean and its one placeholder test must
   pass before you commit. Report the real output; if it's red, fix and re-run.

6. **Deliver (✋ checkpoints).**
   - **Before first commit:** `repo_init.py init work/<name>`. (Requires git identity;
     the tool returns the exact fix if it's unset.)
   - **Before any remote push:** confirm, then optionally `repo_init.py create-gitlab
     <name>` → `repo_init.py push work/<name> <http_url>`. Never push to a remote
     without explicit approval — this is outward-facing.
   - **Register:** `registry.py add <name> --clone-dir work/<name> --stack <stack>
     --default-target-branch main [--gitlab-project-id <id> --gitlab-path <path>]
     [--build-cmd --test-cmd --lint-cmd]` so `auto-dev` can pick it up.

7. **Hand off.** Tell the user the skeleton is ready and that
   `/auto-dev "<first feature> in <name>"` will now build features on it.

## Routing

| User says... | Action |
|---|---|
| "tạo sản phẩm mới: <mô tả>" / "new product: <desc>" | Run the pipeline from Intake |
| "here's the spec: <file>" / "requirement dài" | `intake.py build --name <n> --file <path>` (or pipe/paste via `-`), then continue |
| "which stacks can you scaffold?" | `scaffold.py recipes` + `doctor.py` |
| "just scaffold, don't push" | Run through step 6 local commit only; skip GitLab |

Slash command: `/new-product <description>`.

## Guardrails (inherits CLAUDE.md)

1. **Ask before every `[WRITE]`** the first time in an automated run — especially
   `create-gitlab` / `push` (outward-facing, hard to undo) and the design checkpoint.
2. Never push to a remote or create a GitLab project without explicit approval.
3. Never claim a build/test passed without showing the real command output.
4. Never hardcode tokens/URLs — GitLab creds flow through `config.py`.
5. Keep the scaffold minimal; defer feature work to `auto-dev`. Don't over-build.
