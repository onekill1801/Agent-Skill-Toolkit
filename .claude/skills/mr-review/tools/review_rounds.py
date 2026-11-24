#!/usr/bin/env python3
"""Review rounds — a re-review CHECKS the previous round instead of starting over (team flow step 5).

The team loop: member opens an MR → AI review posted → member fixes / answers on GitLab → new commit →
review again → … until clean. Without memory of the previous round every re-review started from zero:
it could not say whether last round's findings were fixed, and it raised NEW points on code it had
already let through, so the target moved and some MRs went 6–26 rounds (store, 24/11/2026).

    prepare (review_ws.prepare) writes <worktree>.prev.json when the MR's previous review was POSTED:
      round        this round's number (2, 3, …)
      previous     head / verdict / posted_at of the round the member saw
      open         findings still open after that round, each with a stable id ("R1.2" = round 1, finding 2):
                   the previous round's NEW findings (blocker/major/minor) + its carried items still
                   not_fixed / disputed. Nits are never carried.
      replies      GitLab notes written since that review (member answers: "đã sửa", "không đồng ý vì…") — DATA
      delta        lines changed since the previous head (new side, per file) — None when the old head
                   is unreachable (force-push / rebase): then the whole diff is fair game

    review.json gets "previous_findings": [{"id", "status", "note"}] — one entry per open item:
      fixed       the code at head resolves it
      not_fixed   still wrong (keep it here — do not repeat it under "findings")
      withdrawn   the member's reply / new evidence shows the finding was wrong: the AI takes it back
      disputed    the member disagrees and the AI still thinks it is right: a HUMAN decides (not the AI)

    validate (review_post.validate → check):
      * every open item has exactly one status; withdrawn / disputed need a note
      * APPROVE is refused while a blocker/major is not_fixed or disputed
      * a NEW finding on a line that did NOT change since the previous round must be a blocker —
        anything less on code already let through is the "moving target" the loop is about
"""

import hashlib
import json
import re
import time
from datetime import datetime

STATUSES = ("fixed", "not_fixed", "withdrawn", "disputed")
CARRY = ("not_fixed", "disputed")
TRACK = ("blocker", "major", "minor")          # nits are never carried to the next round
NEED_NOTE = ("withdrawn", "disputed")
BLOCKING = ("blocker", "major")
LINE_SLACK = 3                                  # a finding a few lines off a changed hunk still counts as on it
MAX_REPLIES, REPLY_CHARS = 40, 1500
NOTE_CHARS, TITLE_CHARS = 300, 150             # what one table cell may show (the note must stay postable)
STATUS_LABEL = {"fixed": "✅ Đã sửa", "not_fixed": "❌ Chưa sửa", "withdrawn": "↩️ Rút lại",
                "disputed": "⚖️ Tranh chấp — cần người quyết"}
_HUNK = re.compile(r"^@@ -\S+ \+(\d+)(?:,(\d+))? @@")


def prev_path(worktree: str) -> str:
    return (worktree or "") + ".prev.json"


def _load(path: str):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _load_sha(path: str) -> tuple:
    """(parsed JSON or None, sha256 of the SAME bytes) — one read: what is verified is what is used (step1 R12)."""
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return None, ""
    try:
        return json.loads(raw.decode("utf-8")), hashlib.sha256(raw).hexdigest()
    except ValueError:
        return None, hashlib.sha256(raw).hexdigest()


def file_sha(path: str) -> str:
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return ""


def prev_state(job: dict) -> tuple:
    """(prev.json, error). prepare records the file's hash on the job: a prev.json that was deleted or
    edited afterwards is an ERROR, never a silent downgrade to "first round" (Codex rounds R1 #4)."""
    want = job.get("prev_json_sha")
    if not want:
        # this prepare built no round data: a prev.json lying at the path belongs to another round of the same
        # head (force-push back to a posted commit) and must not be applied (Codex rounds R3 #5)
        return None, None
    path = prev_path(job.get("worktree") or "") if job.get("worktree") else ""
    if not path or file_sha(path) != want:
        return None, (f"{path or 'prev.json'} is missing or was changed after prepare — prepare the job again "
                      "(review_ws.py prepare) instead of editing it")
    p = _load(path)
    if not (isinstance(p, dict) and isinstance(p.get("open"), list)):
        return None, f"{path} is not a valid round file — prepare the job again"
    return p, None


def load_prev(job: dict):
    """This round's prev.json, or None (first round / previous review never posted / not trustworthy)."""
    p, err = prev_state(job)
    return None if err else p


# --------------------------------------------------------------------------- the posted-rounds ledger
#
# Per-head artifact paths (<pid>/<iid>@<head8>.review.json, …) are shared by every round that ever sits at that
# head — a force-push back to a posted commit used to draft over the round the member saw (Codex rounds R1–R5).
# So: every round that reaches GitLab is ARCHIVED (immutable copies under <pid>/rounds/) and recorded in the
# job's `posted_rounds` ledger, which each new round inherits. History is read ONLY from that ledger.

def _head(job: dict) -> str:
    return (job.get("diff_refs") or {}).get("head_sha") or job.get("head_sha") or ""


def record_posted(job: dict, posted_at: float = 0, verdict: str = "", note_id=None, review=None,
                  coverage_ok: bool = False) -> dict:
    """Archive the round the job just put on GitLab (its review.json + this round's prev.json) and return its
    ledger entry. A failed copy keeps the live paths (better than no history)."""
    import os
    import shutil
    wt = job.get("worktree") or ""
    head = _head(job)
    rnd = int(job.get("round") or 1)
    live_review = job.get("review_file") or (wt + ".review.json" if wt else "")
    live_prev = prev_path(wt) if wt and job.get("prev_json_sha") and file_sha(prev_path(wt)) == job["prev_json_sha"] else ""
    review_file, prev_file, failed = live_review, live_prev or None, False
    coverage_file = None                    # the round's coverage manifest (next round's "reused" proof)

    def freeze(src: str, dst: str) -> str:
        """Immutable: the FIRST archive of a round wins — a re-reconcile never rewrites it (R6 #3)."""
        if not os.path.exists(dst):
            tmp = f"{dst}.tmp-{os.getpid()}"
            shutil.copyfile(src, tmp)
            os.replace(tmp, dst)
        return dst
    if wt and head:
        base = os.path.join(os.path.dirname(wt), "rounds", f"{job.get('iid')}-r{rnd}-{head[:12]}")
        try:
            os.makedirs(os.path.dirname(base), exist_ok=True)
            if isinstance(review, dict):
                # the exact review that went out: the file may have been edited during the GitLab calls, and
                # the next round reads its coverage from here (Codex ocr-eval step1 R2)
                dst = base + ".review.json"
                if not os.path.exists(dst):
                    tmp = f"{dst}.tmp-{os.getpid()}"
                    with open(tmp, "w", encoding="utf-8") as f:     # ascii: a lone surrogate never fails
                        json.dump(review, f, ensure_ascii=True, indent=2)
                    os.replace(tmp, dst)
                review_file = dst
            elif live_review and os.path.exists(live_review):
                review_file = freeze(live_review, base + ".review.json")
            if live_prev:
                prev_file = freeze(live_prev, base + ".prev.json")
            live_cov = wt + ".coverage.json"
            if job.get("coverage_sha") and file_sha(live_cov) == job["coverage_sha"]:
                coverage_file = freeze(live_cov, base + ".coverage.json")
        except OSError:
            failed = True              # history still points at the live files: flagged, never silent
    return {"status": "posted", "head_sha": head, "round": rnd, "verdict": verdict or job.get("verdict"),
            "posted_at": int(posted_at or time.time()), "note_id": note_id, "review_file": review_file,
            "prev_file": prev_file, "coverage_file": coverage_file,
            "coverage_file_sha": job.get("coverage_sha") if coverage_file else None,
            # the archived review as written: a "reused" inherits its statuses only while it still matches
            "review_file_sha": file_sha(review_file) or None if review_file else None, "worktree": wt,
            # the posted coverage passed review_coverage.check: only then may the next round inherit it
            "coverage_ok": bool(coverage_ok and coverage_file) or None,
            "archive_failed": failed or None}


def with_entry(ledger, entry: dict) -> list:
    """The ledger with `entry` (one entry per head — the latest wins)."""
    out = [e for e in ledger or [] if isinstance(e, dict) and e.get("head_sha") != entry.get("head_sha")]
    return out + [entry]


def ledger_of(old: dict) -> list:
    """Every posted round of the MR as `old` (the job being replaced) knows it — the `posted_rounds` it carries,
    plus, for jobs from before the ledger, its posted `previous` and itself when posted (live paths)."""
    if not old:
        return []
    led = [e for e in old.get("posted_rounds") or [] if isinstance(e, dict) and e.get("head_sha")]
    heads = {e["head_sha"] for e in led}
    prev = old.get("previous")
    if isinstance(prev, dict) and prev.get("status") == "posted" and prev.get("head_sha") not in heads:
        led.insert(0, prev)
        heads.add(prev.get("head_sha"))
    if old.get("status") == "posted" and old.get("head_sha") and old.get("head_sha") not in heads:
        wt = old.get("worktree") or ""
        led.append({"status": "posted", "head_sha": old.get("head_sha"), "verdict": old.get("verdict"),
                    "review_file": old.get("review_file") or (wt + ".review.json" if wt else ""),
                    "prev_file": None, "worktree": wt, "posted_at": old.get("posted_at"),
                    "round": int(old.get("round") or 1)})
    return led


def posted_at_head(ledger, head: str):
    """The posted round at this head, or None."""
    return next((e for e in reversed(ledger or []) if head and e.get("head_sha") == head), None)


def previous_of(old: dict) -> dict:
    """job["previous"] for a NEW round of the same MR (review_queue.add_job): the last posted round. Only a
    POSTED review is a round the member saw; unposted ones (cancelled / failed / rejected) pass it on."""
    if not old:
        return None
    led = ledger_of(old)
    if led:
        return led[-1]
    return {k: old.get(k) for k in ("status", "head_sha", "verdict")}


def adopt_posted(job: dict, note: dict, verdict: str = "", review=None) -> tuple:
    """(entry, round) when the job's OWN round turns out to be on GitLab (a POST whose response was lost,
    found by its marker) although its status never said so: the member saw it, so it is archived + recorded and
    the next round compares against it. posted_at = the note's created_at — replies after it are the answers."""
    at = _epoch(note.get("created_at")) if isinstance(note, dict) else 0.0
    # review = what the lost try SENT (posting record) when known; its coverage is never inherited (unchecked)
    entry = record_posted(job, at, verdict, note.get("id") if isinstance(note, dict) else None, review=review)
    return entry, round_of(entry)


def round_of(previous) -> int:
    return int(previous.get("round") or 1) + 1 if isinstance(previous, dict) and previous.get("status") == "posted" else 1


# --------------------------------------------------------------------------- prepare side

def open_items(prev_review: dict, prev_prev: dict, prev_round: int) -> list:
    """Findings still open after the previous round (see module doc)."""
    items = []
    statuses = {}
    for pf in prev_review.get("previous_findings") or []:
        if isinstance(pf, dict) and pf.get("id"):
            statuses[pf["id"]] = pf
    known = set()
    for it in (prev_prev or {}).get("open") or []:
        if not isinstance(it, dict):
            continue
        known.add(it.get("id"))
        st = statuses.get(it.get("id"))
        if st and st.get("status") in CARRY:
            items.append({**it, "last_status": st["status"], "last_note": str(st.get("note") or "")[:500]})
    # the facts of an older round are gone (prev.json deleted): never drop what is still open — keep the
    # id and its last status so the reviewer re-checks it (Codex rounds R1 #3)
    for pid, st in statuses.items():
        if pid not in known and st.get("status") in CARRY and isinstance(pid, str):
            items.append({"id": pid, "severity": "major", "title": "(nội dung vòng trước không còn — xem lại theo ghi chú)",
                          "last_status": st["status"], "last_note": str(st.get("note") or "")[:500], "history_gap": True})
    k = 0
    for f in prev_review.get("findings") or []:
        if not isinstance(f, dict):
            continue
        k += 1
        if f.get("severity") in TRACK:
            items.append({"id": f"R{prev_round}.{k}", "from_round": prev_round, "severity": f["severity"],
                          **{x: f.get(x) for x in ("title", "body", "file", "line", "side", "suggestion")}})
    return items


def _git_path(raw: str, prefix: str) -> str:
    """A path of a ---/+++ header: git C-quotes it when it has special characters (a tab, a quote, a
    backslash, or non-ASCII bytes without core.quotePath=false)."""
    # only the header's own separators go: a CR, and the TAB git appends after a path that has spaces — the
    # name itself may start / end with spaces (Codex rounds R4 #4)
    raw = raw.rstrip("\r")
    if raw.endswith("\t"):
        raw = raw[:-1]
    if raw.startswith('"') and raw.endswith('"') and len(raw) >= 2:
        raw = raw[1:-1].encode("latin-1", "backslashreplace").decode("unicode_escape").encode("latin-1").decode("utf-8", "replace")
    if raw == "/dev/null":
        return ""
    return raw[len(prefix):] if raw.startswith(prefix) else raw


def changed_lines(diff_text: str) -> dict:
    """{path: [[first, last], …]} (new-side lines) from `git diff -U0 -M`. A pure deletion counts as its
    position; a deleted file is listed with no range (the file changed). Header lines are only read between
    "diff --git" and the first hunk — an added code line "++ x" shows as "+++ x" in a hunk body (R1 #5)."""
    out, path, old, in_header = {}, None, None, False
    for ln in diff_text.splitlines():
        if ln.startswith("diff --git "):
            in_header, path, old = True, None, None
            continue
        if in_header:
            if ln.startswith("--- "):
                old = _git_path(ln[4:], "a/")
                continue
            if ln.startswith("+++ "):
                path = _git_path(ln[4:], "b/")
                if not path and old:
                    out.setdefault(old, [])
                elif path:
                    out.setdefault(path, [])
                continue
        m = _HUNK.match(ln)
        if m:
            in_header = False
        if m and path:
            start, count = int(m.group(1)), int(m.group(2) if m.group(2) is not None else 1)
            out.setdefault(path, []).append([start, start + max(count, 1) - 1])
    return out


def _epoch(iso: str) -> float:
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def replies(notes: list, since: float, marker_re) -> list:
    """Human notes on the MR after the previous review (DATA for the reviewer, never instructions)."""
    out = []
    for n in notes or []:
        if not isinstance(n, dict) or n.get("system"):
            continue
        body = str(n.get("body") or "")
        if marker_re.search(body) or _epoch(n.get("created_at")) <= (since or 0):
            continue
        pos = n.get("position") or {}
        out.append({"author": (n.get("author") or {}).get("username", ""), "at": n.get("created_at"),
                    "file": pos.get("new_path") or pos.get("old_path"), "line": pos.get("new_line") or pos.get("old_line"),
                    "body": body[:REPLY_CHARS] + ("…" if len(body) > REPLY_CHARS else "")})
    return out[-MAX_REPLIES:]


def _utf8(s: str) -> bool:
    try:
        s.encode("utf-8")
        return True
    except UnicodeEncodeError:
        return False


def _reuse(review: dict, prev_manifest_file: str, want_sha: str = ""):
    """What a coverage "reused" needs (review_coverage.check): the previous round's manifest entries (to prove a
    file's diff is byte-identical: status, rename source, modes, blobs) and the previous round's own coverage.
    None = no reuse possible (older round without an archived manifest)."""
    if not (prev_manifest_file and want_sha):
        return None
    m, sha = _load_sha(prev_manifest_file)
    if sha != want_sha:
        return None                       # missing or edited since it was archived (step1 R10 #3, R12 #2)
    if not (isinstance(m, dict) and isinstance(m.get("files"), list)):
        return None
    files = {f["path"]: [f.get("status"), f.get("old_path"), f.get("id")] for f in m["files"]
             if isinstance(f, dict) and isinstance(f.get("path"), str) and f.get("id")
             # a name that is not valid UTF-8 cannot go into prev.json (UTF-8): never reused, reviewed again (R13 #1)
             and _utf8(f["path"]) and _utf8(f.get("old_path") or "")}
    cov = review.get("coverage") if isinstance(review.get("coverage"), list) else []
    return {"prev_files": files,
            "prev_coverage": {c["path"]: c["status"] for c in cov if isinstance(c, dict)
                              and isinstance(c.get("path"), str) and isinstance(c.get("status"), str)
                              and _utf8(c["path"]) and _utf8(c["status"])}}


def build(job: dict, git, notes_fn=None, marker_re=None, head: str = "") -> dict:
    """prev.json content for a prepared round, or None. git(*args) -> stdout, None on failure;
    notes_fn() -> the MR's notes (may raise: replies are then marked unavailable). head = the head being
    prepared NOW (the job's diff_refs may still be the stale ones of a requeue — Codex rounds R1 #1)."""
    prev = job.get("previous")
    if not (isinstance(prev, dict) and prev.get("status") == "posted"):
        return None
    review, review_sha = _load_sha(prev.get("review_file") or "")
    review = review or {}
    if not isinstance(review, dict) or not review.get("head_sha"):
        return None                                   # the file the member saw is gone: nothing to compare
    prev_head = review.get("head_sha") or prev.get("head_sha")
    pr = int(prev.get("round") or 1)
    # the previous round's own facts: its archived prev.json (ledger); legacy entries only had the live path
    older = prev.get("prev_file") if "prev_file" in prev else prev_path(prev.get("worktree") or "")
    items = open_items(review, _load(older) if older else None, pr)
    head = head or job.get("head_sha") or (job.get("diff_refs") or {}).get("head_sha")
    if prev_head and head and prev_head == head:
        return None              # same commit asked again: nothing changed since — a fresh full review (R1 #2)
    delta = None
    if prev_head and head and git("cat-file", "-e", prev_head + "^{commit}") is not None:
        out = git("-c", "core.quotePath=false", "diff", "-U0", "--no-color", "-M", prev_head, head)
        if out is not None:
            delta = changed_lines(out)
            # a pure rename / mode change has no hunk and maybe no ---/+++ header: still a changed file (R2 #4)
            names = git("-c", "core.quotePath=false", "diff", "--name-only", "-z", "-M", prev_head, head)
            for name in (names or "").split("\x00"):
                if name:                                 # names from -z are exact: never strip them
                    delta.setdefault(name, [])
    reps, rep_err = [], None
    if notes_fn:
        try:
            reps = replies(notes_fn(), float(prev.get("posted_at") or 0), marker_re or re.compile(r"(?!)"))
        except Exception as e:  # noqa: BLE001 — replies are context, never a reason to fail the round
            rep_err = f"{type(e).__name__}: {e}"[:200]
    return {"round": pr + 1, "previous": {"head_sha": prev_head, "verdict": prev.get("verdict") or review.get("verdict"),
                                         "posted_at": prev.get("posted_at"), "round": pr},
            "open": items, "replies": reps, "replies_error": rep_err,
            "reuse": _reuse(review, prev.get("coverage_file") or "", prev.get("coverage_file_sha") or "")
            if prev.get("coverage_ok") and prev.get("review_file_sha")
            and review_sha == prev["review_file_sha"] else None,
            "delta": delta, "delta_note": None if delta is not None else
            "previous head not reachable (force-push / rebase?) — review the whole diff",
            "built_at": int(time.time())}


# --------------------------------------------------------------------------- validate side

def _in_ranges(path: str, line, delta: dict, slack: int = LINE_SLACK) -> bool:
    return isinstance(line, int) and not isinstance(line, bool) and any(
        a - slack <= line <= b + slack for a, b in delta.get(path or "") or [])


_CAUSE = re.compile(r"^(.+):(\d+)$")


def _allowed_new(f: dict, delta: dict) -> bool:
    """May this NEW non-blocker finding be raised in a re-review? Only about code that changed since the
    previous round: its own line is in the delta (new side), or — a removed line / a whole-file remark —
    its file changed, or it names the changed line that causes it (`caused_by: "path:line"`, e.g. a
    regression that shows in an untouched caller — Codex rounds R1 #6)."""
    cause = f.get("caused_by")
    if isinstance(cause, str):
        m = _CAUSE.match(cause.strip())
        if m and _in_ranges(m.group(1), int(m.group(2)), delta, slack=0):     # must BE a changed line
            return True
    path = f.get("file")
    if f.get("side", "new") == "old" or f.get("line") is None:
        return path in delta
    return _in_ranges(path, f.get("line"), delta)


def check(review: dict, prev: dict) -> list:
    """Errors of a re-review against its prev.json (empty list = ok). No prev.json = first round: only
    a stray previous_findings is refused (it would be shown as if a previous round existed)."""
    pfs = review.get("previous_findings")
    if not prev:
        return ([] if pfs is None or pfs == [] else
                ["previous_findings given but this is a first round (no previous review was posted) — remove it"])
    errs = []
    open_ = {it["id"]: it for it in prev.get("open") or [] if isinstance(it, dict) and isinstance(it.get("id"), str)}
    if pfs is None:
        pfs = []
    if not isinstance(pfs, list):
        return ["previous_findings must be a list of {id, status, note}"]
    seen = {}
    for i, pf in enumerate(pfs):
        if not isinstance(pf, dict):
            errs.append(f"previous_findings[{i}] must be an object")
            continue
        pid = pf.get("id")
        if not isinstance(pid, str) or pid not in open_:
            errs.append(f"previous_findings[{i}].id {pid!r} is not an open item of the previous round "
                        f"(open: {', '.join(open_) or 'none'})")
            continue
        if pid in seen:
            errs.append(f"previous_findings: {pid} given twice")
        if pf.get("note") is not None and not isinstance(pf.get("note"), str):
            errs.append(f"previous_findings[{i}].note must be a string")
        elif not isinstance(pf.get("status"), str) or pf["status"] not in STATUSES:
            errs.append(f"previous_findings[{i}].status must be one of {STATUSES}")
        elif pf["status"] in NEED_NOTE and not (pf.get("note") or "").strip():
            errs.append(f"previous_findings[{i}] ({pid}) is {pf['status']}: note is required (why)")
        seen[pid] = pf
    missing = [k for k in open_ if k not in seen]
    if missing:
        errs.append("previous round items without a status: " + ", ".join(missing)
                    + f" — give each one of {STATUSES} in previous_findings (read <worktree>.prev.json)")
    if review.get("verdict") == "APPROVE":
        still = [k for k, pf in seen.items() if pf.get("status") in CARRY and open_[k].get("severity") in BLOCKING]
        if still:
            errs.append("verdict APPROVE while previous blocker/major items are not fixed or disputed: " + ", ".join(still))
    delta = prev.get("delta")
    if isinstance(delta, dict):
        for i, f in enumerate(review.get("findings") if isinstance(review.get("findings"), list) else []):
            if not isinstance(f, dict) or f.get("severity") == "blocker" or not isinstance(f.get("file"), str) \
                    or not f["file"]:
                continue                       # a blocker anywhere; a remark with no file (e.g. "MR thiếu test")
            if not _allowed_new(f, delta):
                errs.append(f"findings[{i}] ({f.get('severity')}) is on {f['file']}"
                            + (f":{f['line']}" if f.get("line") is not None else "")
                            + f", unchanged since round {prev['round'] - 1}: only a blocker may be raised there now — "
                            "drop it; if a CHANGED line causes it, set \"caused_by\": \"path:line\" of that line; if it "
                            "is a previous item still open, record it in previous_findings instead")
    return errs


# --------------------------------------------------------------------------- render side

def counts(review: dict) -> dict:
    out = {s: 0 for s in STATUSES}
    for pf in review.get("previous_findings") or []:
        if isinstance(pf, dict) and pf.get("status") in out:
            out[pf["status"]] += 1
    return out


def open_by_severity(review: dict, prev: dict) -> dict:
    """{severity: n} of previous items still not_fixed / disputed (the note must not say "no issue")."""
    open_ = {it.get("id"): it for it in (prev or {}).get("open") or [] if isinstance(it, dict)}
    out = {}
    for pf in review.get("previous_findings") or []:
        if isinstance(pf, dict) and pf.get("status") in CARRY and pf.get("id") in open_:
            sev = open_[pf["id"]].get("severity")
            out[sev] = out.get(sev, 0) + 1
    return out


def _one_line(text, limit: int) -> str:
    """Model-written text shown in a cell / list item: one line, bounded (a long table would push the note past
    the auto-post size cap), code fences flattened. Mentions / entities are defanged on the whole note."""
    t = re.sub(r"\s+", " ", str(text or "").replace("```", "`")).strip()
    return t if len(t) <= limit else t[:limit].rstrip() + " …"


def render_section(review: dict, prev: dict, cell, title_fn, compact: bool = False) -> list:
    """Markdown lines of the "previous round" section (full note) or a short block (compact)."""
    if not prev:
        return []
    pfs = {pf.get("id"): pf for pf in review.get("previous_findings") or [] if isinstance(pf, dict)}
    c = counts(review)
    head8 = str((prev.get("previous") or {}).get("head_sha") or "")[:8]
    tally = " · ".join(f"{STATUS_LABEL[s]} {c[s]}" for s in STATUSES if c[s])
    head = (f"Vòng {prev['round']} — so với review vòng {prev['round'] - 1} (`{head8}`)"
            + (f": {tally}" if tally else ": không còn mục nào mở"))
    if compact:
        lines = [f"**{head}**"]
        for it in prev.get("open") or []:
            pf = pfs.get(it.get("id")) or {}
            if pf.get("status") in CARRY:
                note = _one_line(pf.get("note"), NOTE_CHARS)
                lines.append(f"- {STATUS_LABEL[pf['status']]} `{it['id']}` {_one_line(it.get('title'), TITLE_CHARS)}"
                             + (f" — {note}" if note else ""))
        return lines
    lines = ["### Đối chiếu vòng trước", "", head, ""]
    if prev.get("open"):
        lines += ["| ID | Finding vòng trước | Mức | Trạng thái | Ghi chú |", "|----|----|----|----|----|"]
        for it in prev["open"]:
            pf = pfs.get(it.get("id")) or {}
            loc = f"`{it['file']}`" + (f":{it['line']}" if it.get("line") else "") + " — " if it.get("file") else ""
            lines.append(f"| {it['id']} | {cell(loc + title_fn(_one_line(it.get('title'), TITLE_CHARS) or '—'))} | "
                         f"{it.get('severity', '')} | {STATUS_LABEL.get(pf.get('status'), '—')} | "
                         f"{cell(_one_line(pf.get('note'), NOTE_CHARS))} |")
    if any((pfs.get(it.get("id")) or {}).get("status") == "disputed" for it in prev.get("open") or []):
        lines += ["", "_⚖️ Mục tranh chấp: AI và tác giả MR chưa thống nhất — chủ module / reviewer người quyết, "
                      "AI không tự đóng._"]
    return lines
