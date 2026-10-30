"""What an identity (core.identity.resolve) may do — design §3.4. Checked when a command is received.

Phase 1b covers `review` only; task / deploy / approve rows are added with their phases.
"""

ACTIONS = {"review": "review"}


def check(action: str, ident: dict) -> tuple:
    """(allowed, reason). Unknown action = refused."""
    need = ACTIONS.get(action)
    if not need:
        return False, f"lệnh '{action}' chưa được hỗ trợ"
    perms = set(ident.get("perms") or ())
    if need in perms or "admin" in perms:
        return True, ""
    return False, f"chưa có quyền '{need}'"
