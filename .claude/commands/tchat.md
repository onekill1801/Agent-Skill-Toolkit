# TChat: Conversations Overview

Summarize the user's TChat conversations and outstanding todos (read-only).

1. Read @.claude/skills/tchat-automation/SKILL.md (scope + routing).
2. From `.claude/skills/tchat-automation/tools/`, run:
   - `python config.py` (abort with setup hint if config invalid)
   - `python auth.py whoami` (confirm token works)
   - `python groups.py list --limit 30`
   - `python todos.py list --type TO_ME --limit 30`
   - `python todos.py count-expired`
3. Present a table of conversations (name, id, latest activity) and a short
   todo summary (assigned-to-me count, expired count).
4. Offer next actions: read a conversation (`messages.py list <id>`),
   list members (`groups.py participants <id>`), or shared files
   (`messages.py media <id> --type FILE`).

Notes:
- Use `python3` instead of `python` on macOS/Linux.
- Message bodies are E2E-encrypted — summarize metadata, not plaintext.
- On `401`, tell the user to refresh `FCHAT_BEARER_TOKEN` from a live session.
