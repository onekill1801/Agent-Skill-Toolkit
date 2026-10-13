"""Intake gate (design §3, checklist F.5): every watcher ONLY enqueues into the durable store,
through this one door — which source may create which kind of task, with what trust, and whether
the source is switched on.

    from agent_skill_toolkit.workflows.intake import gate
    gate.admit("fchat", "review_mr")                        # PermissionError if not allowed / off
    gate.sender_allowed(sender_id, "U1,U2", me=my_id)        # allowlist by USER ID only
    gate.submit(st, "mr_watch", "review_mr", ref, title=..., data=...)

Switches: work/policy.json  {"intake": {"fchat": {"on": false}, ...}}  (missing = on).
Message / MR / task text is DATA — the gate never interprets it.
"""

import json
import os
import time

from agent_skill_toolkit.core import paths

# source -> trust + kinds it may create (design §3 table). "*" = any kind.
SOURCES = {
    "self":        {"trust": "high",   "kinds": {"*"}},          # you: CLI / Orca / Telegram
    "manual":      {"trust": "high",   "kinds": {"*"}},          # alias of self (CLI default)
    "fchat":       {"trust": "low",    "kinds": {"review_mr", "investigate"}},
    "mr_watch":    {"trust": "medium", "kinds": {"review_mr"}},
    "atask_watch": {"trust": "medium", "kinds": {"atask_intake"}},
    "schedule":    {"trust": "high",   "kinds": {"proposal"}},    # weekly scan: proposals only
}


def policy_file() -> str:
    return os.path.join(paths.work_dir(), "policy.json")


def _policy() -> dict:
    """The `intake` section, validated: {source: {"on": bool}}. Anything malformed is an error, never
    "on" — an unreadable or mistyped switch must not silently mean "everything on" (Codex F.5 #5)."""
    try:
        with open(policy_file(), encoding="utf-8") as f:
            doc = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        raise PermissionError(f"cannot read {policy_file()}: {e}")
    intake = (doc or {}).get("intake", {}) if isinstance(doc, dict) else None
    if not isinstance(intake, dict):
        raise PermissionError(f"{policy_file()}: `intake` must be an object")
    for name, entry in intake.items():
        if not isinstance(entry, dict) or not isinstance(entry.get("on", True), bool):
            raise PermissionError(f"{policy_file()}: intake.{name} must be {{\"on\": true|false}}")
    return intake


def source_on(source: str) -> bool:
    return (_policy().get(source) or {}).get("on", True)


def host_ok(authority: str, gitlab_url: str) -> bool:
    """A URL authority (host[:port], as found in a message) is OUR GitLab: same hostname and port,
    no userinfo (`good@evil` is evil). Empty configured URL = nothing matches."""
    import urllib.parse
    if not authority or "@" in authority or any(c in authority for c in "\\/?#"):
        return False
    try:
        got = urllib.parse.urlsplit("//" + authority)
        ours = urllib.parse.urlsplit(gitlab_url or "")
        return bool(ours.hostname) and got.hostname == ours.hostname.lower() and got.port == ours.port
    except ValueError:
        return False


def admit(source: str, kind: str) -> dict:
    """{source, trust} if `source` may create `kind` now, else PermissionError."""
    spec = SOURCES.get(source)
    if not spec:
        raise PermissionError(f"unknown intake source {source!r} (known: {sorted(SOURCES)})")
    if "*" not in spec["kinds"] and kind not in spec["kinds"]:
        raise PermissionError(f"source {source!r} (trust {spec['trust']}) may not create {kind!r} tasks")
    if not source_on(source):
        raise PermissionError(f"intake source {source!r} is switched off in {policy_file()}")
    return {"source": source, "trust": spec["trust"]}


def parse_allowlist(csv: str) -> set:
    return {x.strip().lower() for x in (csv or "").split(",") if x.strip()}


def sender_allowed(sender_id, allowlist_csv: str, me: str = "") -> bool:
    """By USER ID only — display names are not identities (anyone can rename themselves).
    '*' = everyone; empty list = nobody (except `me`, the account owner)."""
    sid = str(sender_id or "").strip().lower()
    if not sid:
        return False
    if me and sid == str(me).strip().lower():
        return True
    allowed = parse_allowlist(allowlist_csv)
    return "*" in allowed or sid in allowed


def submit(st, source: str, kind: str, ref: str, *, title: str = "", data: dict = None,
           priority: int = 5, requester_id: str = "", max_attempts: int = 3, unless_settings=()) -> dict:
    """admit() + store.add(); the task records where it came from (shown on the progress board)."""
    who = admit(source, kind)
    intake = {**who, "requester_id": str(requester_id or ""), "at": time.time()}
    task = st.add(kind, ref, title=title, data={**(data or {}), "intake": intake}, priority=priority,
                  max_attempts=max_attempts, unless_settings=unless_settings)
    return task
