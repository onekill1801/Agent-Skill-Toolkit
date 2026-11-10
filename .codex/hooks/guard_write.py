#!/usr/bin/env python3
"""PreToolUse hook — hold every outward-facing / destructive toolkit command for a human.

Claude-only layer, and a SECONDARY one: an agent with a shell can always find another
spelling. The real boundary is credentials the agent cannot read (design §10.2).
  * interactive session          -> "ask"  (Claude shows its permission prompt)
  * TOOLKIT_HEADLESS=1 (launched by toolkit daemons / agent_runner) -> "deny"
  * forging approvals / review state, approving a review          -> always "deny"
  * reading secrets (.env, work/secrets, …) through any tool       -> "ask" / "deny"

Shell tools covered: Bash and PowerShell. Tools may be called directly (`gitlab_api.py`)
or through the dispatcher (`agentkit.py gitlab_api` / `agentkit.py dev-automation:gitlab_api`).

Stdin: the hook event JSON. Stdout: a permission decision, or nothing (= no opinion).
"""

import json
import os
import re
import sys

SHELL_TOOLS = ("Bash", "PowerShell")


def tool(name: str) -> str:
    """Regex for a toolkit tool invoked directly or via agentkit."""
    return rf"(?:\b{name}\.py\b|\bagentkit\.py\s+(?:[\w-]+:)?{name}\b)"


WRITE_RULES = [
    (tool("gitlab_api") + r"\s+(create-branch|delete-branch|create-mr|mr-comment|merge-mr)\b", "GitLab write"),
    (tool("azure_devops") + r"\s+(state|comment)\b", "Azure DevOps write"),
    (tool("notifier"), "notify real people"),
    (tool("send") + "|" + tool("notify_group") + "|" + tool("reply_worker"), "TChat message to real people"),
    (tool("tasks") + r"\s+(create|update|complete|move|assign-sprint|delete)\b", "aTask write"),
    (tool("checklists") + r"\s+(create|delete|add-comment|del-comment)\b", "aTask write"),
    (tool("jenkins") + r"\s+(build|trigger)\b", "Jenkins build"),
    (tool("ssh_exec"), "SSH to other machines"),
    (tool("review_post") + r"\s+(post|sweep)\b", "post review to GitLab"),
    (r"\bgit\s+push\b", "git push"),
    (tool("podfwd") + r"\s+(start|start-saved|fetch)\b.*--allow-prod", "tunnel/copy from a PROD cluster"),
    (r"\b(kubectl|oc)\b(\s+--?\S+(\s+[^-\s]\S*)?)*\s+(apply|delete|patch|edit|scale|rollout|set|replace|exec|"
     r"cordon|drain|rsh|debug|start-build|new-app|process|adm|policy|tag|import-image)\b", "k8s/OpenShift write"),
    (r"\b(curl|Invoke-WebRequest|Invoke-RestMethod|iwr|irm)\b[^|]*(PRIVATE-TOKEN|Authorization)", "raw authenticated API call"),
    # decisions that belong to a human (an agent must never start or accept its own proposal)
    (tool("proposals") + r"\s+(go|decide)\b", "proposal decision"),
    (tool("refactor") + r"\s+program\s+approve\b", "refactor plan approval"),
    (tool("coordinator") + r"\s+(accept|capacity|usage|budget)\b", "coordinator decision / policy"),
]
ALWAYS_DENY = [
    (tool("review_post") + r"\s+(approve|reject)\b", "only a human may approve/reject a review"),
    (r"\borca\b.*\bgate-resolve\b(?!.*--help)", "only a human resolves gates"),
    (r"\bsave_job\s*\(|\bapprovals\.(decide|create)\s*\(", "review/approval state is written by the tools only"),
    (r"review_queue[/\\]+(jobs|locks)|tg_approvals[/\\]", "review/approval state is written by the tools only"),
]
# `.env` but not `.env.sample` / `.environment`; plus other credential locations
SECRET_RE = re.compile(r"(?<![\w.-])\.env(?![\w.-])|work[/\\]+secrets[/\\]|fchat_private\.pem|"
                       r"credentials-config\.json|temp[/\\]+vpn[/\\]|orca-runtime\.json", re.I)
STATE_PATH_RE = re.compile(r"/temp/(review_queue|tg_approvals)/", re.I)


def _secret_path(path: str) -> bool:
    p = (path or "").replace("\\", "/")
    return bool(SECRET_RE.search(p) or p.endswith(".pem"))


def decide(event: dict):
    name = event.get("tool_name", "")
    inp = event.get("tool_input") or {}
    headless = os.environ.get("TOOLKIT_HEADLESS") == "1"
    soft = "deny" if headless else "ask"
    if name in SHELL_TOOLS:
        cmd = inp.get("command", "")
        for rx, why in ALWAYS_DENY:
            if re.search(rx, cmd, re.I):
                return "deny", why
        if SECRET_RE.search(cmd):
            return soft, "touches a secrets file"
        for rx, why in WRITE_RULES:
            if re.search(rx, cmd, re.I):
                return soft, f"{why} — needs a human"
    elif name == "Grep":
        if _secret_path(inp.get("path", "")) or _secret_path(inp.get("glob", "")):
            return soft, "searches a secrets file"
    elif name in ("Read", "Edit", "Write", "NotebookEdit"):
        path = (inp.get("file_path") or inp.get("notebook_path") or "").replace("\\", "/")
        if _secret_path(path):
            return soft, "secrets file"
        if name != "Read" and STATE_PATH_RE.search(path):
            return "deny", "review/approval state is written by the tools only"
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
