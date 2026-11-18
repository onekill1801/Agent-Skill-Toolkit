"""Env change notices (design §5.3, phase 2b) — pure: what changed between two snapshots of one env, which
MRs it brought or took away, and the E card / daily digest texts. The daemon (env_watch.py) does the I/O.

  * a change = the env now runs another version / commit / digest (a level change alone is not news)
  * an env we could not read this time is NOT a change (no "rollback" because k8s timed out)
  * MRs added = merged MRs of the commits in compare(old, new); lost = compare(new, old) (rollback)
  * E card: one per (group, repo, env), id E-<repo>-<env>, PROTOCOL.md fields the extension parses
"""

import re
import time

from agent_skill_toolkit.workflows.group_board import envmap, render

MAX_COMMITS = 50          # commits mapped to MRs per change; beyond that the compare link is the answer


def changed(prev: dict, cur: dict) -> bool:
    """True when `cur` is a readable snapshot running something else than `prev` (both from envmap.snapshot)."""
    if not prev or not cur or not cur.get("version"):
        return False
    if cur.get("level") == "unknown" and not cur.get("commit"):
        return False                                   # unreadable now: never report a change from that
    return any((prev.get(k) or "") != (cur.get(k) or "") for k in ("version", "commit", "digest"))


def mrs_between(frm: str, to: str, compare, commit_mrs, branch: str = "") -> dict:
    """{"mrs": [{iid, title, web_url}], "truncated": bool, "unknown": bool, "error": bool} for commits in `to`
    not in `frm`. unknown = a commit is missing (nothing to compare); error = GitLab failed (retry later)."""
    if not frm or not to:
        return {"mrs": [], "truncated": False, "unknown": True, "error": False}
    commits = compare(frm, to)
    if commits is None:
        return {"mrs": [], "truncated": False, "unknown": True, "error": True}
    seen, out = set(), []
    for c in commits[-MAX_COMMITS:]:
        found = commit_mrs(c.get("id"))
        if found is None:
            return {"mrs": [], "truncated": False, "unknown": True, "error": True}
        for m in found:
            if m.get("state") != "merged" or m.get("iid") in seen:
                continue
            if branch and m.get("target_branch") not in (branch, None):
                continue
            seen.add(m.get("iid"))
            out.append({"iid": m.get("iid"), "title": m.get("title") or "", "web_url": m.get("web_url") or ""})
    return {"mrs": sorted(out, key=lambda m: m["iid"] or 0), "truncated": len(commits) > MAX_COMMITS,
            "unknown": False, "error": False}


def card_id(repo: str, env: str, taken_by=None) -> str:
    """E-<repo>-<env>. `taken_by(id)` -> the ref already owning that id (or None): a DIFFERENT ref (repo a-b /
    env c vs repo a / env b-c) gets a stable short-hash suffix instead of colliding."""
    import hashlib
    cid = "E-" + re.sub(r"[^A-Za-z0-9._-]", "-", f"{repo}-{env}")
    owner = taken_by(cid) if taken_by else None
    if owner and owner != f"{repo}:{env}":
        cid += "-" + hashlib.sha1(f"{repo}:{env}".encode()).hexdigest()[:4]
    return cid


def _codes(mrs: list, r_code) -> tuple:
    """(['R-7', …], ['!1401', …]) — MRs that have a review card show its code, the others their !iid."""
    with_card, without = [], []
    for m in mrs:
        code = r_code(m["iid"])
        (with_card if code else without).append(code or f"!{m['iid']}")
    return with_card, without


def e_card_text(repo: str, web: str, prev: dict, cur: dict, added: dict, lost: dict, r_code, cid: str = "") -> str:
    """The E card (PROTOCOL.md): header, fields the extension reads, links to check by hand."""
    when = envmap._when(cur.get("rs_created_at") or "")
    lines = [f"🤖 [{cid or card_id(repo, cur['env'])}] {repo} @ {cur['env']} · {cur.get('version') or '?'}",
             f"Môi trường: {cur['env']}",
             " · ".join(x for x in (f"Phiên bản: {cur.get('version') or '?'}",
                                     f"Commit: {cur['commit'][:8]}" if cur.get("commit") else "",
                                     f"Xác minh: {envmap.LEVEL[cur.get('level') or 'unknown']}") if x),
             " · ".join(x for x in (f"Rollout: {when}" if when else "",
                                     f"Trước đó: {prev.get('version') or '?'}") if x)]
    a_codes, a_other = _codes(added.get("mrs") or [], r_code)
    l_codes, l_other = _codes(lost.get("mrs") or [], r_code)
    parts = []
    if a_codes:
        parts.append("MR thêm: " + " ".join(a_codes))
    if l_codes:
        parts.append("MR mất: " + " ".join(l_codes))
    if parts:
        lines.append(" · ".join(parts))
    other = []
    if a_other:
        other.append("thêm " + " ".join(a_other))
    if l_other:
        other.append("mất " + " ".join(l_other))
    if added.get("truncated") or lost.get("truncated"):
        other.append(f"quá {MAX_COMMITS} commit — xem link so sánh")
    if added.get("error") or lost.get("error") or (added.get("unknown") and lost.get("unknown")):
        side = ("thêm" if added.get("error") else "") + ("/" if added.get("error") and lost.get("error") else "") + \
            ("mất" if lost.get("error") else "")
        other.append(f"không đọc được danh sách MR {side}".rstrip() + " — xem link so sánh")
    if other:
        lines.append("Ghi chú: MR chưa có thẻ " + "; ".join(other))
    if cur.get("notes"):
        lines.append("Lưu ý: " + "; ".join(cur["notes"]))
    if web and prev.get("commit") and cur.get("commit"):
        lines.append(f"🔗 {web}/-/compare/{prev['commit']}...{cur['commit']}")
    if (cur.get("build") or {}).get("url"):
        lines.append(f"🔗 {cur['build']['url']}")
    return render.defuse_body("\n".join(lines))


def digest_text(repo_changes: dict, since: float, now: float = None) -> str:
    """Daily summary of batched changes: {repo: [(env, prev, cur, added, lost)]} -> one message."""
    now = now or time.time()
    lines = [f"🌐 Tổng hợp môi trường {time.strftime('%d/%m %H:%M', time.localtime(since))} → "
             f"{time.strftime('%d/%m %H:%M', time.localtime(now))}"]
    for repo, items in sorted(repo_changes.items()):
        for env, prev, cur, added, lost in items:
            def ids(d):
                mrs = d.get("mrs") or []
                s = " ".join(f"!{m['iid']}" for m in mrs[:10]) + (f" +{len(mrs) - 10}" if len(mrs) > 10 else "")
                if d.get("truncated"):
                    s += f" (chỉ tính {MAX_COMMITS} commit mới nhất)"
                if d.get("error"):
                    s = "không đọc được danh sách MR"
                return s
            extra = []
            if (added or {}).get("mrs") or (added or {}).get("error"):
                extra.append(f"MR thêm: {ids(added)}")
            if (lost or {}).get("mrs") or (lost or {}).get("error"):
                extra.append(f"MR mất: {ids(lost)}")
            lines.append(f"{repo} @ {env}: {prev.get('version') or '?'} → {cur.get('version') or '?'} "
                         f"({envmap.LEVEL[cur.get('level') or 'unknown']})" + "".join(" · " + x for x in extra))
    if len(lines) == 1:
        lines.append("Không có thay đổi.")
    return render.defuse_body("\n".join(lines))
