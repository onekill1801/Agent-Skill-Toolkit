"""Who a chat sender is, and what they may do (docs/design/group-workboard.md §3.1, §11b).

Source: `work/team.json` (team-registry, gitignored) — members keyed by a short key, matched to a chat sender by
`handles.fchat_id` ONLY (display names are not identities). Fields used here:
    handles{fchat_id, gitlab_username, atask_user_id}, perms[], verified_by, verified_at
Read-only here; team.py writes it.

Permissions (user decision 30/10):
  * the group owner (the account the bot runs on) = admin
  * a member with an explicit `perms` list in team.json gets exactly that list
  * any other sender of a workboard group = DEFAULT_MEMBER_PERMS (review + task)
  * deploy:<env> / approve:<env> are only ever granted explicitly
"""

import json
import os
import re

from agent_skill_toolkit.core import paths

DEFAULT_MEMBER_PERMS = ("review", "task")
OWNER_PERMS = ("admin", "review", "task")
ENVS = ("dev", "sandbox", "demo")
_PERM = re.compile(r"^(review|task|admin|(deploy|approve):(%s))$" % "|".join(ENVS))


def valid_perm(p: str) -> bool:
    return bool(_PERM.match(str(p or "")))


def team_path() -> str:
    return os.path.join(paths.work_dir(), "team.json")


def load_team() -> dict:
    """{} when there is no registry (everyone is a default member). A broken file raises — callers decide."""
    try:
        with open(team_path(), encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    if not isinstance(data, dict):
        raise ValueError("team.json must be an object")
    return data


def find(team: dict, fchat_id: str):
    """(key, record, ambiguous). Not linked = (None, None, False). Two records claiming the same id =
    (None, None, True): an ambiguous link is never trusted — and never falls back to the default perms."""
    sid = str(fchat_id or "").strip()
    hits = [(k, r) for k, r in team.items()
            if isinstance(r, dict) and sid and str((r.get("handles") or {}).get("fchat_id") or "").strip() == sid]
    if len(hits) > 1:
        return None, None, True
    return (*hits[0], False) if hits else (None, None, False)


def resolve(fchat_id: str, name: str = "", owner_id: str = "", team: dict = None) -> dict:
    """Fail closed: a registry entry that is malformed (perms not a list of strings) or an ambiguous link gets
    NO permission; only a sender with no entry at all gets the default member perms."""
    team = load_team() if team is None else team
    key, rec, ambiguous = find(team, fchat_id)
    rec = rec or {}
    h = rec.get("handles") or {}
    owner = bool(owner_id) and str(fchat_id) == str(owner_id)
    explicit = rec.get("perms")
    if owner:
        perms, source = list(OWNER_PERMS), "owner"
    elif ambiguous:
        perms, source = [], "ambiguous"
    elif "perms" in rec and not (isinstance(explicit, list) and all(isinstance(p, str) for p in explicit)):
        perms, source = [], "invalid"
    elif "perms" in rec:
        perms, source = [p for p in explicit if valid_perm(p)], "team.json"
    else:
        perms, source = list(DEFAULT_MEMBER_PERMS), "default"
    return {"fchat_id": str(fchat_id or ""), "name": name or rec.get("name") or "", "key": key, "owner": owner,
            "gitlab": h.get("gitlab_username") or "", "atask": str(h.get("atask_user_id") or ""),
            "verified": bool(rec.get("verified_by")), "verified_by": rec.get("verified_by") or "",
            "perms": perms, "perms_source": source}
