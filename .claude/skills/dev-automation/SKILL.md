---
name: Dev Automation Skill
description: >
  Automate development workflows: review GitLab merge requests, read Azure DevOps tasks,
  fix bugs, implement features, create merge requests, and notify testers.
  Trigger phrases: "review MR", "check merge request", "fix bug", "fix task",
  "implement task", "new feature", "notify tester", "deploy to dev",
  "read Azure task", "list my tasks".
---

# Dev Automation Skill

## Feature Flags

```yaml
ENABLE_AZURE_DEVOPS: true
ENABLE_GITLAB: true
ENABLE_NOTIFICATIONS: true
```

## Available Tools

All tools are in `tools/` relative to this SKILL.md. They use only Python stdlib (zero pip dependencies).
The agent MUST read the tool source before first use to understand function signatures.

| Tool | Purpose |
|------|---------|
| `tools/config.py` | Configuration loader — reads `.env` for API tokens |
| `tools/azure_devops.py` | Azure DevOps Work Items API — read tasks, update state, add comments |
| `tools/gitlab_api.py` | GitLab API — branches, merge requests, diffs, comments, `whoami`, `my-review-mrs` |
| `tools/mr_watch.py` | Poll GitLab for MRs assigned to me → auto-launch a Claude review (Workflow 5) |
| `tools/notifier.py` | Send formatted notifications via Azure DevOps comments |

## Workflows

### Workflow 1: Review Merge Request

**Triggers:** "review MR #123", "check merge request", "review code in MR"

**Steps:**
1. Read `cookbook/review-merge-request.md` for review standards and checklist
2. Read `cookbook/java-standards.md` for language-specific rules
3. Get MR diff: `python gitlab_api.py mr-changes <mr_iid>`
4. Get existing discussions: `python gitlab_api.py mr-discussions <mr_iid>`
5. Analyze each changed file against the review checklist
6. Read `prompts/code_review_prompt.md` for output format
7. Post review as MR comment: `python gitlab_api.py mr-comment <mr_iid> "<review>"`
8. If linked to Azure DevOps task, notify: `python notifier.py review-done <work_item_id> <mr_url> "<summary>"`

### Workflow 2: Fix Bug

**Triggers:** "fix bug task #456", "fix Azure task #456", "resolve bug"

**Steps:**
1. Read `cookbook/fix-bug-workflow.md` for the full workflow
2. Get task details: `python azure_devops.py get <task_id>`
3. Read `prompts/task_analysis_prompt.md` to structure the analysis
4. Analyze the bug description, repro steps, and acceptance criteria
5. Notify task started: `python notifier.py started <task_id> <branch_name>`
6. Update task state to Active: `python azure_devops.py state <task_id> Active`
7. Create branch: `python gitlab_api.py create-branch "bugfix/<task_id>-<short-desc>" develop`
8. Implement the fix following `cookbook/java-standards.md`
9. Create MR: `python gitlab_api.py create-mr "bugfix/<task_id>-<short-desc>" "Fix: <title>" develop`
10. Notify MR created: `python notifier.py mr-created <task_id> <mr_url>`
11. Self-review the MR using Workflow 1

### Workflow 3: Implement New Feature

**Triggers:** "implement task #789", "new feature task #789", "develop feature"

**Steps:**
1. Read `cookbook/new-feature-workflow.md` for the full workflow
2. Get task details: `python azure_devops.py get <task_id>`
3. Read `prompts/task_analysis_prompt.md` to structure the analysis
4. Break down the feature into sub-tasks and create an implementation plan
5. Notify task started: `python notifier.py started <task_id> <branch_name>`
6. Update task state to Active: `python azure_devops.py state <task_id> Active`
7. Create branch: `python gitlab_api.py create-branch "feature/<task_id>-<short-desc>" develop`
8. Implement the feature following `cookbook/java-standards.md`
9. Create MR: `python gitlab_api.py create-mr "feature/<task_id>-<short-desc>" "Feature: <title>" develop`
10. Notify MR created: `python notifier.py mr-created <task_id> <mr_url>`
11. Self-review the MR using Workflow 1

### Workflow 4: Notify Tester

**Triggers:** "notify tester", "tell QA to test", "deployment done"

**Steps:**
1. Read `prompts/notify_tester_prompt.md` for message format
2. Gather info: MR link, dev environment URL, acceptance criteria from the task
3. Post notification: `python notifier.py deploy-done <task_id> <env_url>`

### Workflow 5: Auto-watch & review MRs assigned to me

**Triggers:** "auto review MR", "watch my MRs", "review MR gắn tên tôi", "tự động review MR"

`tools/mr_watch.py` is a long-running poller. Every interval it lists OPENED MRs
where you are the reviewer (or assignee) across all accessible projects
(`gitlab_api.list_review_merge_requests`). For each MR not yet reviewed at its
current head SHA it:
1. maps the MR's project → the registered clone in `work/projects.json`;
2. **pulls the MR's latest code** into an ISOLATED git worktree at the exact MR
   SHA (`git fetch origin merge-requests/<iid>/head` + `git worktree add --detach`)
   — your working branch in the clone is never touched;
3. **opens one terminal** running headless `claude -p --dangerously-skip-permissions
   --model <MR_REVIEW_MODEL|sonnet>` IN that worktree — it reviews the MR against the
   real code (full repo context + canonical diff via `gitlab_api.py mr-changes`),
   saves the review to `temp/mr_reviews/mr_<pid>_<iid>.md`, **auto-posts** it as an
   MR comment, prints a summary, and exits;
4. the terminal **auto-closes** (`cmd /c`) and the worktree is removed.

Fully automatic (no prompts) — `claude -p` runs the whole review and exits, so the
window closes itself; output is logged to `temp/mr_reviews/mr_<pid>_<iid>.log`.
If the project has no registered/cloned entry, it falls back to an API-diff-only
review. Re-reviews when commits change the SHA. State in `temp/mr_reviewed.json`;
worktrees under `temp/mr_worktrees/` (pruned on start); instructions in `temp/mr_incoming/`.

```
python gitlab_api.py whoami                         # current GitLab user
python gitlab_api.py my-review-mrs [reviewer|assignee|both]   # MRs tagged to me
python mr_watch.py [--who reviewer|assignee|both] [--interval 300] [--max-per-cycle 3] \
                   [--review-existing] [--once] [--no-spawn] [--include-drafts]
```

Defaults: `--who reviewer`, poll every 5 min, spawn Claude with confirm-before-post.
**First run = baseline** (marks current MRs seen, doesn't review the backlog → no
terminal flood); only NEW/updated MRs are auto-reviewed thereafter. `--review-existing`
to work through the backlog (throttled by `--max-per-cycle`, default 3 per poll).

## Routing Rules

When the user makes a request, match it to the correct workflow:

| User says... | Workflow | Cookbook |
|---|---|---|
| "review MR #X" / "check merge request" | Workflow 1 | `cookbook/review-merge-request.md` |
| "fix bug #X" / "fix task #X" (type=Bug) | Workflow 2 | `cookbook/fix-bug-workflow.md` |
| "implement #X" / "new feature #X" | Workflow 3 | `cookbook/new-feature-workflow.md` |
| "notify tester" / "deploy done" | Workflow 4 | `prompts/notify_tester_prompt.md` |
| "list my tasks" / "show assigned tasks" | Direct call | `python azure_devops.py list` |
| "list open MRs" | Direct call | `python gitlab_api.py list-mrs` |
| "auto review MR" / "watch my MRs" / "review MR gắn tên tôi" | Workflow 5 | `python mr_watch.py` |

## Slash Commands (explicit invocation)

Same workflows as above, via `.claude/commands/`:

| Command | Workflow |
|---------|----------|
| `/list-tasks` | List assigned Azure DevOps work items |
| `/read-task <id>` | Get one work item |
| `/review-mr <iid>` | Workflow 1 |
| `/fix-bug <id>` | Workflow 2 |
| `/implement-feature <id>` | Workflow 3 |
| `/notify-tester <id> [url]` | Workflow 4 |
| `/watch-mrs [who] [interval]` | Workflow 5 (start the MR auto-review watcher) |

Run Python tools from `tools/` relative to this skill. Use `python3` on Linux/macOS; `python` on Windows if `python3` is unavailable.

## Important Rules

1. **Always read the relevant cookbook BEFORE starting a workflow** — it contains detailed instructions.
2. **Always read tool source code before first use** to understand exact function signatures.
3. **Never hardcode tokens or URLs** — always use `config.py` to load from `.env`.
4. **Branch naming convention:** `<type>/<task_id>-<short-kebab-description>` (e.g., `bugfix/12345-fix-null-pointer`).
5. **MR title convention:** `<Type>: <Task Title>` (e.g., `Fix: NullPointerException in UserService`).
6. **Always self-review** code changes using Workflow 1 before notifying testers.
7. **Post progress updates** to Azure DevOps work items at each major step.
8. **Follow Java/Spring Boot standards** from `cookbook/java-standards.md` for all code changes.
