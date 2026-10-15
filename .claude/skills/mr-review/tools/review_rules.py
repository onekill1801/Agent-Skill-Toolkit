#!/usr/bin/env python3
"""Auto-post rules for MR review comments (docs/design/agent-ecosystem.md §10.2.1).

A review that passes every rule may be posted with no human approval; one that fails any
rule falls back to the human approval channels. The rules run on the exact text that would
be posted, at post time — editing the job file or review.json after the check changes
nothing, because post() re-renders and re-checks.

Config (.env / environment):
  REVIEW_AUTO_POST           off | shadow | auto   (default shadow: evaluate + report, still ask a human)
  REVIEW_AUTO_POST_PROJECTS  comma list of registry names / project paths / ids allowed to auto-post
                             (empty = none)
  REVIEW_AUTO_POST_SOURCES   comma list of request sources (default manual,mr_watch,fchat)
  REVIEW_AUTO_POST_DAILY_MAX posts per day across all MRs (default 30)
  REVIEW_AUTO_POST_MAX_CHARS rendered note limit (default 6000)
  REVIEW_AUTO_POST_MAX_INLINE inline comments limit (default 15)

    python review_rules.py check <job_id>      # what auto-post would decide, no side effects
"""

import json
import os
import re
import sys
import time
from urllib.parse import urlparse

import review_common as rc

MODES = ("off", "shadow", "auto")
MARKER_FMT = "<!-- ai-review:{iid}:{head} -->"
MARKER_RE = re.compile(r"<!-- ai-review:(\d+):([0-9a-f]{7,40}) -->")

_URL = re.compile(r"https?://[^\s)>\]]+", re.I)
# GitLab pings "@name" after any non-word char — a backtick included (Codex fmt R1 #1)
MENTION = re.compile(r"(?<!\w)@([A-Za-z0-9_][A-Za-z0-9_.-]*)")
_FENCE_OPEN = re.compile(r"(`{3,}|~{3,})(.*)")             # column 0 only: never inside a container
# a line that opens an HTML block: markdown (fences included) inside it is NOT parsed, so text in it
# could ping while looking "fenced" to fence_spans(); our own marker comments are the only exception
# our markers are comments CLOSED on their own line (an unclosed one would hide/shift what follows)
_MARKER_OK = r"(?!!-- ai-review[^\n]*-->[ \t]*$)"
_HTML_LINE = re.compile(r"(?m)^ {0,3}<" + _MARKER_OK + r"[A-Za-z/!?]")
# constructs that can make a column-0 ``` line NOT a fence (or end one early) in GitLab markdown:
# a bare CR (a line ending there, not here), GitLab's >>> multi-line quote, $$ math blocks, HTML
# blocks, and ANY line starting with "[" (link reference definitions / footnotes: escaped "]" and
# multi-line labels/titles swallow lines — Codex fmt R3 #1). Any of them -> no fence is trusted.
_FENCE_UNSAFE = re.compile(r"(?m)\r|^ {0,3}(?:>>>|\$\$|\[|<" + _MARKER_OK + r"[A-Za-z/!?])")


def fence_spans(text: str) -> list:
    """[(start, end)] of fenced code blocks GitLab is certain to see as code. Deliberately STRICTER
    than CommonMark (Codex fmt R2 #1): lines split on "\\n" only; opener at column 0 (a top-level
    block: nothing but its closer or the end of the text can end it); closer = same char, length >=
    opener, <= 3 spaces indent, only spaces/tabs after; unclosed runs to the end. A text containing
    _FENCE_UNSAFE constructs gets no spans at all, so mentions are neutralised inside code too."""
    text = text or ""
    if _FENCE_UNSAFE.search(text):
        return []
    spans, pos, cur = [], 0, None
    for line in text.split("\n"):
        if cur is None:
            m = _FENCE_OPEN.fullmatch(line)
            if m and not (m.group(1)[0] == "`" and "`" in m.group(2)):
                cur = (pos, m.group(1)[0], len(m.group(1)))
        elif re.fullmatch(r" {0,3}" + re.escape(cur[1]) + "{%d,}[ \t]*" % cur[2], line):
            spans.append((cur[0], min(len(text), pos + len(line) + 1)))
            cur = None
        pos += len(line) + 1
    if cur is not None:
        spans.append((cur[0], len(text)))
    return spans


def outside_fences(text: str) -> str:
    out, pos = [], 0
    for start, end in fence_spans(text):
        out += [text[pos:start], "\n"]
        pos = end
    return "".join(out) + text[pos:]
_SECRET = re.compile(
    r"glpat-[A-Za-z0-9_-]{10,}|gh[pousr]_[A-Za-z0-9]{20,}|xox[abprs]-[A-Za-z0-9-]{10,}"
    r"|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|(?i:(?:password|passwd|secret|token|api[_-]?key)\s*[:=]\s*\S{6,})")
_RUN_THIS = re.compile(r"(?i)(?:curl|wget|iwr|invoke-webrequest)\b[^\n|]*\|\s*(?:sh|bash|iex|pwsh|powershell)\b"
                       r"|\brm\s+-rf\s+/|\bformat\s+[a-z]:")


def _cfg(key: str, default: str = "") -> str:
    return (rc.config.get(key, default) or default).strip()


def _csv(key: str, default: str = "") -> list:
    return [x.strip().lower() for x in _cfg(key, default).split(",") if x.strip()]


def mode() -> str:
    m = _cfg("REVIEW_AUTO_POST", "shadow").lower()
    return m if m in MODES else "shadow"


def marker(job: dict) -> str:
    head = ((job.get("diff_refs") or {}).get("head_sha") or job.get("head_sha") or "")[:12]
    return MARKER_FMT.format(iid=job["iid"], head=head)


def _project_keys(job: dict) -> set:
    keys = {str(job.get("project_id", "")).lower(), (job.get("project_path") or "").lower()}
    try:
        name, _ = rc.registry_by_gitlab_id(job.get("project_id"))
        if name:
            keys.add(str(name).lower())
    except Exception:  # noqa: BLE001 — registry is optional
        pass
    return {k for k in keys if k}


def _gitlab_host() -> str:
    return (urlparse(rc.config.get("GITLAB_URL", "") or "").hostname or "").lower()


def content_problems(job: dict, texts: list) -> list:
    """Rules on the exact text to be posted."""
    probs = []
    host = _gitlab_host()
    allowed_mentions = {(job.get("author") or "").lower()} - {""}
    for t in texts:
        for u in _URL.findall(t):
            h = (urlparse(u).hostname or "").lower()
            if not host or h != host:
                probs.append(f"link outside GitLab host: {h or u[:40]}")
        prose = outside_fences(t)                             # GitLab does not ping from fenced code
        for m in MENTION.findall(prose):
            if m.lower() not in allowed_mentions:
                probs.append(f"@mention of someone other than the MR author: @{m}")
        if _HTML_LINE.search(prose):
            probs.append("raw HTML block in the text")
        if _SECRET.search(t):
            probs.append("text looks like it contains a secret/token")
        if _RUN_THIS.search(t):
            probs.append("text tells readers to run a dangerous command")
    return sorted(set(probs))


def posted_today() -> int:
    start = time.time() - 86400
    return sum(1 for j in rc.all_jobs()
               if j.get("status") == "posted" and j.get("auto_posted") and (j.get("posted_at") or 0) >= start)


def evaluate(job: dict, note: str, inline_bodies: list) -> dict:
    """Decide whether this exact payload may be posted without a human. No side effects."""
    m = mode()
    reasons = []
    if m == "off":
        reasons.append("auto-post is off (REVIEW_AUTO_POST=off)")
    allowed = set(_csv("REVIEW_AUTO_POST_PROJECTS"))
    if not allowed & _project_keys(job):
        reasons.append("project not in REVIEW_AUTO_POST_PROJECTS")
    sources = set(_csv("REVIEW_AUTO_POST_SOURCES", "manual,mr_watch,fchat"))
    req_sources = {(r.get("source") or "manual").lower() for r in job.get("requests", [])} or {"manual"}
    if not req_sources <= sources:
        reasons.append(f"request source not allowed: {sorted(req_sources - sources)}")
    if len(note) > rc.cfg_int("REVIEW_AUTO_POST_MAX_CHARS", 6000):
        reasons.append("note too long")
    if len(inline_bodies) > rc.cfg_int("REVIEW_AUTO_POST_MAX_INLINE", 15):
        reasons.append("too many inline comments")
    reasons += content_problems(job, [note, *inline_bodies])
    if posted_today() >= rc.cfg_int("REVIEW_AUTO_POST_DAILY_MAX", 30):
        reasons.append("daily auto-post cap reached")
    passed = not reasons
    return {"mode": m, "rules_passed": passed, "auto_post": passed and m == "auto", "reasons": reasons}


def all_pages(gl, path: str, what: str, per_page: int = 100, max_pages: int = 200):
    """Every item of a paginated GitLab list (page until a short page). Anything but a list = error:
    reconciliation never concludes "absent" from a partial or unreadable answer (Codex R.M4 R2)."""
    sep = "&" if "?" in path else "?"
    for page in range(1, max_pages + 1):
        res = gl._request(gl._api(f"{path}{sep}per_page={per_page}&page={page}"))
        if not isinstance(res, list):
            raise RuntimeError(f"cannot read MR {what} to reconcile: "
                               f"{str(res.get('message', res) if isinstance(res, dict) else res)[:200]}")
        yield from res
        if len(res) < per_page:
            return
    raise RuntimeError(f"MR {what}: more than {max_pages} pages — reconcile by hand")


def already_posted(gl, job: dict) -> bool:
    """Reconcile before (re)posting: is our marker for this head already on the MR (any page)?"""
    want = marker(job)
    return any(want in (n.get("body") or "")
               for n in all_pages(gl, f"/merge_requests/{job['iid']}/notes?sort=desc&order_by=created_at", "notes"))


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 2 or argv[0] != "check":
        print(__doc__)
        return 2
    import review_post as rp
    job = rc.load_job(argv[1])
    if not job:
        return rc.pc.emit({"error": True, "message": f"no job {argv[1]}"})
    review = rp.load_review(job, job.get("review_file", ""))
    note, inline = rp.payload(job, review)
    return rc.pc.emit({"job_id": job["id"], **evaluate(job, note, inline)})


if __name__ == "__main__":
    sys.exit(main())
