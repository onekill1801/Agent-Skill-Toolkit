"""What an identity (core.identity.resolve) may do — design §3.4. Checked when a command is received.

Phase 1b: `review`; 2a: `env` (read-only, any member not blocked by the registry); 3: `task` (draft / confirm /
cancel — the card also checks "same requester"), `task_done` (closing — the card checks requester or assignee).
"""

# action -> required perm; None = any member the registry does not block (read-only questions)
ACTIONS = {"review": "review", "env": None, "task": "task", "task_done": None}


def check(action: str, ident: dict) -> tuple:
    """(allowed, reason). Unknown action = refused."""
    if action.startswith(("deploy:", "approve:")):      # theo môi trường: deploy:dev, approve:dev …
        need = action
    elif action not in ACTIONS:
        return False, f"lệnh '{action}' chưa được hỗ trợ"
    else:
        need = ACTIONS[action]
    perms = set(ident.get("perms") or ())
    if need is None:
        return (True, "") if perms else (False, "danh bạ chặn tài khoản này")
    if need in perms or "admin" in perms:
        return True, ""
    return False, f"chưa có quyền '{need}'"
