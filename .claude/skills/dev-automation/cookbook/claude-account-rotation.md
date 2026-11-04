# Orca Claude account rotation

Tool: `tools/claude_account_rotation.py`. Python stdlib only. Windows named pipe
and macOS/Linux Unix socket use Orca's local authenticated runtime metadata.
The internal runtime RPC is version dependent (verified against Orca 1.4.219).

## Policy

- Explicitly configure managed account UUIDs; the pool never silently includes a
  newly added account. Current implementation handles host accounts only.
- A candidate must have both five-hour and weekly usage below 80%.
- Keep the current account until either window reaches the threshold, then select
  the next eligible account in the configured order, wrapping at the end.
- Only when every account has known, fresh usage and reaches 80% in either window
  does the threshold become 95%.
- At 95% exhaustion, return `wait`. Return to the 80% tier as soon as an account's
  two windows are both below 80% after a refresh/reset.
- Unknown, stale, failed or incomplete quota is never interpreted as zero.
  Unknown quota prevents escalation to 95%. A window whose reset time has passed
  needs a new measurement.

## Preview

From the repository root:

```text
python .claude/skills/dev-automation/tools/claude_account_rotation.py --accounts <uuid1> <uuid2> <uuid3> <uuid4> <uuid5>
```

Append `--watch --interval 60` to monitor. Preview is read only and does not select
accounts. It prints a decision and each account's `[five_hour, weekly]` percentages.

`--apply` performs a WRITE to Orca's current host account selection, using the same
runtime method as Orca's account button, and verifies the selected account. It also
renews expired inactive host tokens when necessary. Use only when authorized.
`--apply-new-sessions` remains an alias. Append `--watch --interval 60` for automatic
rotation. `--refresh-delay 180` delays token renewal after recent rate limiting.
`--status-file <path>` saves the latest decision for inspecting a background watcher.

No terminal is restarted or interrupted. Although Orca displays a restart advisory,
the installed Claude Code 2.1.296 has credential-file change detection and token
cache invalidation. Existing sessions can adopt changed credentials on subsequent
requests. Older versions or sessions using an injected environment token may behave
differently. Successful Orca selection does not prove an old process has adopted it.
The tool's `wait` decision does not stop existing sessions from making requests.
Polling provides an approximate trigger, not a hard spending/quota ceiling.

## Credentials and limitations

Orca's `accounts.list` RPC refreshes usage. If inactive usage is missing, the tool
can read valid existing host `.credentials.json` files and fetch usage using the
same OAuth usage endpoint as Orca. It validates the managed account directory and
marker, refuses symlink credential files and redirects, and keeps TLS verification.
Preview skips expired tokens. Apply mode can renew inactive tokens using the public
Claude Code OAuth client used by Orca. Rotated refresh tokens are persisted atomically
and unrelated credential fields are retained. The active account's refresh remains
owned by Orca/Claude. Failed renewals keep existing credentials and wait at least five
minutes before retrying (15 minutes for authentication rejection). A per-account OS
lock prevents concurrent renewals by this tool; the watcher also holds a singleton
OS lock. A detected concurrent credential update is retained instead of overwritten.
Reauthentication requiring browser interaction must be completed through Orca.
macOS credentials held only in Keychain require Orca to provide inactive usage.

Config is loaded through `config.py`:

- `ORCA_USER_DATA_DIR`: optional Orca profile directory override.
- `CLAUDE_USAGE_URL`: optional HTTPS usage endpoint override. Only configure a
  trusted endpoint; it receives the account's OAuth access token.
- `CLAUDE_OAUTH_TOKEN_URL`, `CLAUDE_OAUTH_CLIENT_ID`: public OAuth configuration
  overrides. The token endpoint receives refresh tokens; only use a trusted service.

On Windows, prefer `py -3` if `python` points to an MSYS build without a CA store.
Do not work around an empty CA store by disabling TLS verification.

The watcher is not automatically installed as a boot-time service. An agent may
launch it hidden when explicitly requested to enable account rotation. For that
launch, store its process ID and start time alongside the log/status file so it can
be identified and stopped without affecting user terminals. Runtime outages return
`wait` and the watcher retries when Orca is reachable again.

## Validation

```text
python -m unittest discover -s tests -p test_claude_account_rotation.py
```
