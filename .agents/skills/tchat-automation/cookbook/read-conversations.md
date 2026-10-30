# Browse Conversations & History

## Purpose
List the user's TChat conversations, drill into one, see members, and read
message metadata / shared media.

## Prerequisites
- `.env` has `FCHAT_BASE_API_URL`, `FCHAT_BEARER_TOKEN`, `FCHAT_X_APP`.
- Verify: `python config.py` then `python auth.py whoami`.
- `cd .claude/skills/tchat-automation/tools` (Windows `python`, else `python3`).

## Steps

1. **List conversations (newest first):**
   ```
   python groups.py list --limit 30
   ```
   To page older, take the oldest `latestMessageAt` you got and pass it back:
   ```
   python groups.py list --limit 30 --before 2026-06-11T07:45:50.421Z
   ```

2. **Inspect one conversation** (use its id from step 1):
   ```
   python groups.py get <group_id>
   python groups.py participants <group_id> --limit 50
   python groups.py setting <group_id>
   ```

3. **Read message history** (⚠ text bodies are E2E-encrypted ciphertext):
   ```
   python messages.py list <group_id> --limit 50
   ```
   Use this for metadata (sender, timestamps, ids, attachments), not plaintext.

4. **Shared files / links / media / voice:**
   ```
   python messages.py media <group_id> --type FILE
   python messages.py media <group_id> --type LINK
   python messages.py media <group_id> --type MEDIA
   python messages.py media <group_id> --type VOICE
   ```

## Error recovery

| Symptom | Cause | Fix |
|---|---|---|
| `status: 401` | Token expired/invalid | Re-copy `FCHAT_BEARER_TOKEN` from a live session |
| `status: 403` | Header/origin rejected | Check `FCHAT_X_APP` matches a real request |
| `status: 429` | Rate limited | Back off; honor `Retry-After` from the error message |
| Empty / odd fields | Response shape differs from assumption | Inspect raw JSON; adjust caller |
