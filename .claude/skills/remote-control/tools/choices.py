#!/usr/bin/env python3
"""File-backed store for interactive multiple-choice prompts over Telegram.

The bridge, after an agent turn, may find a structured choice block in the
agent's reply (see CHOICE_SYS in telegram_bridge.py). It registers the options
here, renders them as inline buttons, and — when the user taps one — resumes the
agent session feeding the chosen option back as the next turn.

Stored next to the approval records (rc_config.temp_dir()), one JSON per token.
"""

import json
import os
import re
import time
import uuid

import rc_config as cfg

# Markers the agent is instructed to wrap a choice prompt in. Kept deliberately
# unlikely to appear in normal prose so detection is unambiguous.
_BLOCK_RE = re.compile(r"\[\[TG_CHOICE\]\](.*?)\[\[/TG_CHOICE\]\]", re.DOTALL)
_OPT_RE = re.compile(r"^\s*\d+[.)]\s*(.+?)\s*$")
_Q_RE = re.compile(r"^\s*question\s*:\s*(.+?)\s*$", re.IGNORECASE)

MAX_OPTIONS = 8


def _path(token: str) -> str:
    return os.path.join(cfg.temp_dir(), f"choice_{token}.json")


def parse(text: str):
    """Find a TG_CHOICE block in `text`.

    Returns (preamble, question, options) or None if no well-formed block.
    `preamble` is whatever the agent wrote before the block (its reasoning).
    """
    m = _BLOCK_RE.search(text or "")
    if not m:
        return None
    preamble = text[:m.start()].strip()
    body = m.group(1)
    question, options = "", []
    for line in body.splitlines():
        if not line.strip():
            continue
        qm = _Q_RE.match(line)
        if qm and not question:
            question = qm.group(1)
            continue
        om = _OPT_RE.match(line)
        if om:
            options.append(om.group(1))
        elif not question:
            question = line.strip()
    options = options[:MAX_OPTIONS]
    if len(options) < 2:
        return None  # not a real choice -> let it through as plain text
    return preamble, (question or "Chọn một phương án:"), options


def create(chat, question: str, options: list) -> str:
    token = uuid.uuid4().hex[:8]
    rec = {
        "token": token, "chat": str(chat), "question": question,
        "options": options, "status": "pending", "created": int(time.time()),
    }
    with open(_path(token), "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False)
    return token


def get(token: str) -> dict:
    try:
        with open(_path(token), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def resolve(token: str, index: int) -> str:
    """Mark a token decided; return the chosen option text ('' if invalid)."""
    rec = get(token)
    if not rec or rec.get("status") != "pending":
        return ""
    opts = rec.get("options", [])
    if not (0 <= index < len(opts)):
        return ""
    rec["status"] = "done"
    rec["chosen"] = opts[index]
    rec["decided"] = int(time.time())
    with open(_path(token), "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False)
    return opts[index]
