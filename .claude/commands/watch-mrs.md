# Watch & Auto-Review MRs Assigned to Me

Start the GitLab MR auto-review watcher (dev-automation Workflow 5).

Arguments (optional): `$ARGUMENTS` may contain `who` (reviewer|assignee|both) and/or an interval in seconds.

1. Read @.claude/skills/dev-automation/SKILL.md (Workflow 5).
2. Confirm GitLab config: `cd .claude/skills/dev-automation/tools && python gitlab_api.py whoami`.
3. Show what's currently pending (no side effects):
   `python mr_watch.py --once --no-spawn` — lists MRs tagged to me.
4. Start the watcher (long-running):
   `python mr_watch.py [--who reviewer|assignee|both] [--interval 300]`
   - Default: reviewer, poll every 5 minutes.
   - For each new/updated MR it opens a Claude terminal that reviews via
     `/review-mr` and **asks before posting any comment**.
5. Tell the user it runs until Ctrl+C, re-reviews when commits change the SHA,
   and tracks state in `temp/mr_reviewed.json`.

Notes:
- Use `python3` instead of `python` on macOS/Linux.
- Posting review comments is a `[WRITE]` action — the spawned Claude must confirm
  with the user before `gitlab_api.py mr-comment`.
- `--include-drafts` to also review Draft/WIP MRs (skipped by default).
