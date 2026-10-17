"""Classify an agent CLI failure: provider limit (pause + resume later), auth, or a real error.

Only the LAST lines of a failed run are inspected, and only phrasings a provider/CLI uses for its
own refusal — not words that appear in ordinary failures ("expected HTTP 429 but got 500",
"rate limit config is invalid", docs about rate limiting). [Unverified against real CLI output on
this machine — no sample was found in the logs.] Extend with the exact text you see:
    TOOLKIT_QUOTA_PATTERNS="regex1||regex2"      extra quota/limit patterns
    TOOLKIT_AUTH_PATTERNS="regex1||regex2"       extra auth patterns
    TOOLKIT_QUOTA_DEFAULT_WAIT_MIN=30            used only when no reset time can be parsed
"""

import os
import re
import time
from datetime import datetime, timedelta, timezone


class QuotaExceeded(Exception):
    """A provider refused for a limit. Workflows PAUSE (no attempt used) until `resume_at`.
    Shared by agent runners and the step engine (agent_runner.AgentQuotaError is a subclass)."""

    def __init__(self, resume_at: float, backend: str = "", reason: str = ""):
        super().__init__(reason or "provider limit")
        self.resume_at, self.backend, self.reason = float(resume_at or 0), backend, reason


# A provider/CLI refusal is a line of its own: optional "Error:"-style prefix, then the phrase.
# Text quoted inside an assertion, a log of someone else's API, or docs does not start a line
# that way ("expected 429 Too Many Requests", "assert 'rate_limit_error' in msg").
_PREFIX = (r"^\s*(?:[•●✗✖×>]\s*)?"                                  # a CLI status glyph, not a quote
           r"(?:\[?[\w .-]{0,24}(?:error|warn(?:ing)?|fatal)\]?\s*[:\-]\s*)?")
_PHRASES = [
    r"(?:claude ai )?usage limit (?:reached|exceeded|hit)",
    r"you(?:'ve| have) (?:hit|reached) your (?:usage |rate )?limit",
    r"(?:5|five)[- ]hour (?:usage )?limit (?:reached|hit)", r"weekly (?:usage )?limit (?:reached|hit)",
    r"rate[ _-]?limit(?:ed| exceeded| reached)\b", r"(?:429:?\s*)?too many requests\b",
    r"you exceeded your (?:current )?quota", r"credit balance is too low",
    r"(?:529:?\s*)?overloaded\b", r"(?:rate_limit|overloaded)_error\b",
]
# A provider error ENVELOPE that starts the line (after an optional error prefix / HTTP status):
#   {"type":"error","error":{"type":"rate_limit_error",...}}     (Anthropic)
#   {"error":{"message":"...","type":"insufficient_quota",...}}   (OpenAI)
# Not a fixture or a quoted value in the middle of a sentence.
_ENVELOPE = _PREFIX + r'(?:\d{3}\s*)?\{\s*(?:"type"\s*:\s*"error"\s*,\s*)?"error"\s*:\s*\{[^{}]*'
_JSON_ERR = (_ENVELOPE + r'"(?:type|code)"\s*:\s*"(?:rate_limit_error|overloaded_error|insufficient_quota|'
             r'rate_limit_exceeded)"')
_QUOTA = [_PREFIX + "(?:" + "|".join(_PHRASES) + r")(?=$|[\s.,;:!)|·-])", _JSON_ERR]
_AUTH = [
    _PREFIX + r"(?:not logged in|please (?:log ?in|run .{0,20}login)|invalid (?:x-)?api[ _-]?key|"
              r"oauth token (?:has )?expired|401\b[^\n]{0,40}(?:unauthori[sz]ed|authentication))",
    _ENVELOPE + r'"type"\s*:\s*"authentication_error"',
]
_LIMIT_KIND = [("overloaded", r"overloaded|\b529\b"), ("rate_limit", r"rate[ _-]?limit|too many requests|\b429\b"),
               ("usage_limit", r".")]

TAIL_LINES = 12
MAX_PLAUSIBLE_S = 31 * 86400          # a parsed reset further than this is treated as unparsed
MAX_FALLBACK_S = 6 * 3600


def _compile(defaults, env_key):
    extra = [p for p in (os.environ.get(env_key) or "").split("||") if p.strip()]
    return re.compile("|".join(f"(?:{p})" for p in defaults + extra), re.I | re.M)


def _tail(text: str) -> str:
    """A limit is reported as the final error of a run; ignore the agent's own earlier output."""
    lines = [ln for ln in (text or "").strip().splitlines() if ln.strip()]
    return "\n".join(lines[-TAIL_LINES:])[-3000:]


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
    m = re.search(r"(?:try again|retry|resets?|available again)\s+in\s+(\d+)\s*"
                  r"(seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h|days?|d)\b", t, re.I)
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        mult = 86400 if unit.startswith("d") else 3600 if unit.startswith("h") else 60 if unit.startswith("m") else 1
        return now + n * mult
    m = re.search(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)", t)
    if m and re.search(r"reset|retry|again|until", t, re.I):
        try:
            dt = datetime.fromisoformat(m.group(1).replace("Z", "+00:00"))
            return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()
        except ValueError:
            pass
    m = re.search(r"resets?\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", t, re.I)   # "resets 3pm"
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
        "resume_at": epoch|None, "reset_parsed": bool, "retry_other_backend": bool}"""
    now = now or time.time()
    tail = _tail(text)
    if _compile(_AUTH, "TOOLKIT_AUTH_PATTERNS").search(tail):
        return {"kind": "auth", "limit": None, "resume_at": None, "reset_parsed": False,
                "retry_other_backend": True}
    if _compile(_QUOTA, "TOOLKIT_QUOTA_PATTERNS").search(tail):
        limit = next(k for k, rx in _LIMIT_KIND if re.search(rx, tail, re.I))
        parsed = parse_reset(tail, now)
        if parsed and now < parsed <= now + MAX_PLAUSIBLE_S:
            at = max(parsed, now + 30)                       # explicit reset: keep it (weekly limits!)
        else:
            wait = 60 if limit in ("rate_limit", "overloaded") else \
                int(os.environ.get("TOOLKIT_QUOTA_DEFAULT_WAIT_MIN", "30")) * 60
            at, parsed = now + min(wait, MAX_FALLBACK_S), 0
        return {"kind": "quota", "limit": limit, "resume_at": at, "reset_parsed": bool(parsed),
                "retry_other_backend": True}
    return {"kind": "error", "limit": None, "resume_at": None, "reset_parsed": False,
            "retry_other_backend": False}
