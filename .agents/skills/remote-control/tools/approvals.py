#!/usr/bin/env python3
"""File-backed approval store shared by the hook and the bridge.

The PreToolUse hook (telegram_approve.py) CREATEs a pending request and then
WAITs on it. The bridge daemon (telegram_bridge.py), on receiving the inline
button press, DECIDEs it. Both processes see the same temp/tg_approvals/ dir
(located via rc_config.repo_root), so the file is the IPC channel.
"""

import json
import os
import time
import uuid

import rc_config as cfg


def _path(req_id: str) -> str:
    return os.path.join(cfg.temp_dir(), f"{req_id}.json")


def create(tool_name: str, summary: str, detail: str = "",
           risk: str = "write") -> str:
    """Create a pending request, return its short id."""
    req_id = uuid.uuid4().hex[:10]
    rec = {
        "id": req_id, "tool": tool_name, "summary": summary,
        "detail": detail, "risk": risk, "status": "pending",
        "created": int(time.time()),
    }
    with open(_path(req_id), "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False)
    return req_id


def get(req_id: str) -> dict:
    try:
        with open(_path(req_id), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def decide(req_id: str, approved: bool, by: str = "") -> dict:
    rec = get(req_id)
    if not rec:
        return {}
    rec["status"] = "approved" if approved else "denied"
    rec["decided"] = int(time.time())
    rec["by"] = by
    with open(_path(req_id), "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False)
    return rec


def wait(req_id: str, timeout: int = 300, poll: float = 1.0) -> str:
    """Block until decided or timeout. Returns 'approved' | 'denied' | 'timeout'."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        rec = get(req_id)
        status = rec.get("status")
        if status in ("approved", "denied"):
            return status
        time.sleep(poll)
    # mark it so a late button press doesn't look actionable
    decide(req_id, False, by="timeout")
    return "timeout"
