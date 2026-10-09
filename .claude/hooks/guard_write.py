#!/usr/bin/env python3
"""PreToolUse hook — hold every outward-facing / destructive toolkit command for a human.

Claude-only layer (other providers rely on the tools' own gates, e.g. review_post.py).
  * interactive session          -> "ask"  (Claude shows its permission prompt)
  * TOOLKIT_HEADLESS=1 (launched by toolkit daemons / agent_runner) -> "deny"
  * agent trying to approve a review (review_post.py approve|reject) -> always "deny"
  * reading secrets (.env, work/secrets) through Bash/Read          -> "ask" / "deny"

Stdin: the hook event JSON. Stdout: a permission decision, or nothing (= no opinion).
"""

import json
import os
import re
import sys

WRITE_RULES = [
    (r"gitlab_api\.py\s+(create-branch|delete-branch|create-mr|mr-comment|merge-mr)\b", "GitLab write"),
    (r"azure_devops\.py\s+(state|comment)\b", "Azure DevOps write"),
    (r"\bnotifier\.py\b", "notify real people"),
    (r"\b(send|notify_group)\.py\b", "TChat message to real people"),
    (r"\btasks\.py\s+(create|update|complete|move|assign-sprint|delete)\b", "aTask write"),
    (r"\bchecklists\.py\s+(create|delete|add-comment|del-comment)\b", "aTask write"),
    (r"\bjenkins\.py\s+(build|trigger)\b", "Jenkins build"),
    (r"\bssh_exec\.py\b", "SSH to other machines"),
    (r"\breview_post\.py\s+(post|sweep)\b", "post review to GitLab"),
    (r"\bgit\s+push\b", "git push"),
    (r"\bpodfwd\.py\s+(start|start-saved|fetch)\b.*--allow-prod", "tunnel/copy from a PROD cluster"),
    (r"\b(kubectl|oc)\b(\s+--?\S+(\s+[^-\s]\S*)?)*\s+(apply|delete|patch|edit|scale|rollout|set|replace|exec|"
     r"cordon|drain|rsh|debug|start-build|new-app|process|adm|policy|tag|import-image)\b", "k8s/OpenShift write"),
    (r"\bcurl\b[^|]*(PRIVATE-TOKEN|Authorization:)", "raw authenticated API call"),
]
ALWAYS_DENY = [
    (r"\breview_post\.py\s+(approve|reject)\b", "only a human may approve/reject a review"),
    (r"\borca\b.*\bworktree\s+set\b.*work/reviews/", "only a human moves review cards on the Orca board"),
    (r"\borca\b.*\bgate-resolve\b", "only a human resolves gates"),
]
SECRET_RE = re.compile(r"(^|[\s/\"'=])(\.env)(\s|$|[\"'])|work/secrets/|fchat_private\.pem|credentials-config\.json|temp/vpn/")


def decide(event: dict):
    tool = event.get("tool_name", "")
    inp = event.get("tool_input") or {}
    headless = os.environ.get("TOOLKIT_HEADLESS") == "1"
    if tool == "Bash":
        cmd = inp.get("command", "")
        for rx, why in ALWAYS_DENY:
            if re.search(rx, cmd):
                return "deny", why
        if SECRET_RE.search(cmd) and re.search(r"\b(cat|less|more|head|tail|grep|sed|awk|type|Get-Content|base64|xxd|openssl|dd|python3?)\b", cmd):
            return ("deny" if headless else "ask"), "reads a secrets file"
        for rx, why in WRITE_RULES:
            if re.search(rx, cmd):
                return ("deny" if headless else "ask"), f"{why} — needs a human"
    elif tool in ("Read", "Edit", "Write"):
        path = (inp.get("file_path") or "").replace("\\", "/")
        if (path.endswith("/.env") or "/work/secrets/" in path or path.endswith(".pem")
                or path.endswith("credentials-config.json") or "/temp/vpn/" in path):
            return ("deny" if headless else "ask"), "secrets file"
        if tool in ("Edit", "Write") and "/temp/review_queue/" in path:
            return "deny", "review queue state is written by the tools only"
    return None, ""


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except ValueError:
        return 0
    decision, reason = decide(event)
    # Under the Telegram bridge, telegram_approve.py (same PreToolUse event) shows the
    # approve/deny buttons; keep only the hard denies here so the two hooks don't fight.
    if os.environ.get("CLAUDE_TG_BRIDGE") == "1" and decision == "ask":
        decision = None
    if decision:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                                 "permissionDecision": decision,
                                                 "permissionDecisionReason": f"[toolkit guard] {reason}"}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
