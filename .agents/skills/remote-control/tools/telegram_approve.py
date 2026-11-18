#!/usr/bin/env python3
"""PreToolUse hook: gate risky tool calls through a Telegram approve/deny button.

ONLY active when the spawning process sets CLAUDE_TG_BRIDGE=1 (the bridge does
this for agents it launches). In any other context the hook prints nothing and
exits 0, so your interactive Claude Code sessions are unaffected.

Claude Code hook protocol:
  stdin  : JSON {tool_name, tool_input, session_id, ...}
  stdout : JSON {"hookSpecificOutput": {"hookEventName":"PreToolUse",
                 "permissionDecision":"allow|deny", "permissionDecisionReason":...}}

Decision policy in bridge mode:
  - read-only tools / read-only Bash    -> allow (no ping)
  - Edit/Write/MultiEdit/NotebookEdit   -> ask via Telegram
  - Bash 'write' or 'danger'            -> ask (danger is flagged ⚠️)
  - anything unrecognized               -> ask (fail safe)
  - Telegram unreachable / timeout      -> deny (never run unapproved)
"""

import html
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import rc_config as cfg  # noqa: E402

READ_ONLY_TOOLS = {
    "Read", "Grep", "Glob", "LS", "NotebookRead", "WebFetch", "WebSearch",
    "TodoWrite", "Task",  # Task sub-agents inherit this same hook
    # Control/UI tools — never touch the filesystem; let them through so a
    # headless agent isn't blocked waiting on an approval that makes no sense.
    "AskUserQuestion", "ExitPlanMode",
}
FILE_WRITE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}

# Categories the hook can place a tool call into. 'danger' can NEVER be
# auto-approved. The rest are auto-approved iff listed in TELEGRAM_AUTO_APPROVE.
#   read  : read-only tools / read-only Bash
#   file  : local file edits (Edit/Write/MultiEdit/NotebookEdit)
#   bash  : Bash writes (git push, service restart, SSH to LAN, deletes, ...)
#   danger: destructive Bash (rm -rf, mkfs, shutdown, drop table, ...) -> always ask
DEFAULT_AUTO_APPROVE = "read,file"


def _auto_set() -> set:
    raw = cfg.get("TELEGRAM_AUTO_APPROVE")
    if raw == "":
        raw = DEFAULT_AUTO_APPROVE
    cats = {p.strip().lower() for p in raw.split(",") if p.strip()}
    # Per-run override đặt bởi tiến trình spawn (vd group_watch cho review/build):
    # NỚI thêm nhóm được tự duyệt CHỈ cho lượt agent đó, không đổi chính sách chung
    # của bot CODE. 'danger' vẫn bị chặn ở nơi gọi (guard category != "danger").
    per_run = os.environ.get("CLAUDE_TG_AUTO_APPROVE", "")
    cats |= {p.strip().lower() for p in per_run.split(",") if p.strip()}
    return cats


def _out(decision: str, reason: str):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": decision,
        "permissionDecisionReason": reason,
    }}))
    sys.exit(0)


def _classify_tool(tool: str, ti: dict):
    """Return (category, short_summary, detail) — category in read|file|bash|danger."""
    if tool == "Bash":
        cmd = ti.get("command", "")
        try:
            import ssh_exec
            risk = ssh_exec.classify(cmd)  # read | write | danger
        except Exception:  # noqa: BLE001
            risk = "write"
        cat = "read" if risk == "read" else ("danger" if risk == "danger" else "bash")
        return cat, f"Bash: {cmd[:120]}", cmd
    if tool in FILE_WRITE_TOOLS:
        path = ti.get("file_path") or ti.get("notebook_path") or "?"
        return "file", f"{tool} → {path}", path
    if tool == "Task":
        desc = ti.get("description", "")
        # sub-agent tool calls are independently gated by this same hook
        return "read", f"Spawn agent: {desc[:100]}", ti.get("prompt", "")[:500]
    if tool in READ_ONLY_TOOLS:
        return "read", tool, json.dumps(ti, ensure_ascii=False)[:300]
    return "bash", tool, json.dumps(ti, ensure_ascii=False)[:500]


def main():
    if cfg.get("CLAUDE_TG_BRIDGE") != "1" and os.environ.get("CLAUDE_TG_BRIDGE") != "1":
        sys.exit(0)  # not a bridge-spawned agent: stay out of the way

    try:
        payload = json.load(sys.stdin)
    except (ValueError, OSError):
        sys.exit(0)

    tool = payload.get("tool_name", "")
    ti = payload.get("tool_input", {}) or {}

    category, summary, detail = _classify_tool(tool, ti)
    risk = "danger" if category == "danger" else ("read" if category == "read" else "write")

    # Auto-approve per policy (TELEGRAM_AUTO_APPROVE). 'danger' can never be in it.
    if category != "danger" and category in _auto_set():
        _out("allow", f"auto-approve ({category})")

    try:
        import approvals
        import tg_api
    except Exception as e:  # noqa: BLE001
        _out("deny", f"không nạp được module duyệt: {e}")

    # Route the approval card to the ops/approval bot when one is configured
    # (TELEGRAM_OPS_BOT / TELEGRAM_APPROVAL_BOT). A private chat's id == the user's
    # id, stable across bots, so CLAUDE_TG_CHAT_ID still reaches the right person
    # via the ops bot; an explicit ops channel/allowlist wins when present.
    appr_bot = tg_api.approval_bot()
    chat = cfg.get("TELEGRAM_APPROVAL_CHAT")
    if not chat and appr_bot:
        chat = (tg_api.allowed_chats(appr_bot)[:1] or [""])[0]
    if not chat:
        chat = os.environ.get("CLAUDE_TG_CHAT_ID") or ""
    if not chat:
        chat = (cfg.get_list("TELEGRAM_ALLOWED_CHATS")[:1] or [""])[0]
    if not chat:
        _out("deny", "không có chat để hỏi duyệt (đặt CLAUDE_TG_CHAT_ID hoặc "
                     "TELEGRAM_ALLOWED_CHATS[_OPS])")

    req_id = approvals.create(tool, summary, detail, risk)
    flag = "⚠️ <b>NGUY HIỂM</b>\n" if risk == "danger" else ""
    text = (
        f"{flag}🤖 <b>Agent xin duyệt thao tác</b>\n"
        f"<b>Tool:</b> {html.escape(tool)}\n"
        f"<b>Việc:</b> <code>{html.escape(summary)}</code>\n"
        + (f"\n<pre>{html.escape(detail[:1200])}</pre>" if detail and detail != summary else "")
    )
    resp = tg_api.send_message(chat, text, reply_markup=tg_api.approve_keyboard(req_id),
                               bot=appr_bot)
    if not resp.get("ok"):
        _out("deny", f"không gửi được yêu cầu duyệt: {resp.get('description')}")

    timeout = int(cfg.get("TELEGRAM_APPROVAL_TIMEOUT", "300") or "300")
    verdict = approvals.wait(req_id, timeout=timeout)
    if verdict == "approved":
        _out("allow", "đã duyệt qua Telegram")
    elif verdict == "timeout":
        _out("deny", f"hết {timeout}s không có phản hồi → từ chối")
    else:
        _out("deny", "bị từ chối qua Telegram")


if __name__ == "__main__":
    main()
