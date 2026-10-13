"""Classify an agent CLI failure: provider limit (pause + resume later), auth, or a real error.

Replaces substring matching on words like "login" anywhere in output (agent_runner used to
retry a whole turn because the output mentioned a login function).

The default patterns are best-effort [Unverified against real CLI output on this machine — no
sample was found in the logs]. Add the exact text you see with:
    TOOLKIT_QUOTA_PATTERNS="regex1||regex2"      (extra quota/limit patterns)
    TOOLKIT_AUTH_PATTERNS="regex1||regex2"       (extra auth patterns)
    TOOLKIT_QUOTA_DEFAULT_WAIT_MIN=30            (when no reset time can be parsed)
"""

import os
import re
import time
from datetime import datetime, timedelta, timezone

_QUOTA = [
    r"usage limit (?:reached|exceeded)", r"\bhit (?:your|the) (?:usage )?limit", r"(?:5|five)[- ]hour limit",
    r"weekly limit", r"\brate[ _-]?limit(?:ed| exceeded| reached)?\b", r"too many requests", r"\b429\b",
    r"quota (?:exceeded|reached)|insufficient_quota|exceeded your (?:current )?quota",
    r"\boverloaded(?:_error)?\b", r"\b529\b", r"credit balance is too low",
]
_AUTH = [
    r"not logged in", r"please (?:log ?in|run .{0,20}login)", r"invalid api key", r"authentication_error",
    r"\b401\b.{0,40}(?:unauthori[sz]ed|auth)", r"oauth token (?:has )?expired", r"session expired",
]
_LIMIT_KIND = [("overloaded", r"overloaded|\b529\b"), ("rate_limit", r"rate[ _-]?limit|too many requests|\b429\b"),
               ("usage_limit", r".")]

MAX_WAIT_S = 6 * 3600


def _compile(defaults, env_key):
    extra = [p for p in (os.environ.get(env_key) or "").split("||") if p.strip()]
    return re.compile("|".join(f"(?:{p})" for p in defaults + extra), re.I)


def _tail(text: str, n: int = 4000) -> str:
    """Limits are reported at the END of a run; don't match the agent's own earlier output."""
    return (text or "")[-n:]


def parse_reset(text: str, now: float = None) -> float:
    """Best-effort resume time (epoch seconds) from a limit message, else 0."""
    now = now or time.time()
    t = text or ""
    m = re.search(r"\|(\d{10})\b", t)                                   # "...limit reached|1767225600"
    if m:
        return float(m.group(1))
    m = re.search(r"retry[- ]after[\"':\s]+(\d+)", t, re.I)             # header / JSON field, seconds
    if m:
        return now + int(m.group(1))
    m = re.search(r"(?:try again|retry|resets?|available again)\s+in\s+(\d+)\s*(second|sec|s|minute|min|m|hour|hr|h)",
                  t, re.I)
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        mult = 3600 if unit.startswith("h") else 60 if unit.startswith("m") else 1
        return now + n * mult
    m = re.search(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)", t)
    if m and re.search(r"reset|retry|again|until", t, re.I):
        try:
            dt = datetime.fromisoformat(m.group(1).replace("Z", "+00:00"))
            return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()
        except ValueError:
            pass
    m = re.search(r"resets?\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", t, re.I)   # "resets 3pm", "resets at 15:00"
    if m:
        h, mi, ap = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").lower()
        if ap == "pm" and h < 12:
            h += 12
        if ap == "am" and h == 12:
            h = 0
        if h < 24 and mi < 60:
            base = datetime.fromtimestamp(now)
            cand = base.replace(hour=h, minute=mi, second=0, microsecond=0)
            if cand.timestamp() <= now:
                cand += timedelta(days=1)
            return cand.timestamp()
    return 0.0


def classify(text: str, now: float = None) -> dict:
    """{"kind": "quota"|"auth"|"error", "limit": usage_limit|rate_limit|overloaded|None,
        "resume_at": epoch|None, "retry_other_backend": bool}"""
    now = now or time.time()
    tail = _tail(text)
    if _compile(_AUTH, "TOOLKIT_AUTH_PATTERNS").search(tail):
        return {"kind": "auth", "limit": None, "resume_at": None, "retry_other_backend": True}
    if _compile(_QUOTA, "TOOLKIT_QUOTA_PATTERNS").search(tail):
        limit = next(k for k, rx in _LIMIT_KIND if re.search(rx, tail, re.I))
        wait_default = 60 if limit in ("rate_limit", "overloaded") else \
            int(os.environ.get("TOOLKIT_QUOTA_DEFAULT_WAIT_MIN", "30")) * 60
        at = parse_reset(tail, now) or now + wait_default
        at = min(max(at, now + 30), now + MAX_WAIT_S)
        return {"kind": "quota", "limit": limit, "resume_at": at, "retry_other_backend": True}
    return {"kind": "error", "limit": None, "resume_at": None, "retry_other_backend": False}
