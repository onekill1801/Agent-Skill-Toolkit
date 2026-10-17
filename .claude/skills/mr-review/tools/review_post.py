#!/usr/bin/env python3
"""Approve-then-post for MR reviews — the ONLY path that writes a review to GitLab.

Hard gate (enforced here, whichever agent/provider drafted the review):
  1. review.json must validate and be for the head the workspace was prepared at
  2. EITHER the exact payload passes the auto-post rules (review_rules.py, REVIEW_AUTO_POST=auto)
     OR a human approved THIS payload (Telegram button · terminal TTY). Every approval is bound
     to a hash of the text to post — edit the review after approving and the approval is void.
     Orca board status / gates are display only: agents can set them, so they never approve.
  3. the MR must not have moved since (stale -> requeued, nothing posted)
  4. our marker for this head is not already on the MR (reconcile before any retry)

Interim limit (until the isolated executor, design §10.2): the job and approval files are
still writable by a process running as the same OS user, so a determined local agent could
forge a human approval. The auto-post rules do not depend on those files.

    python review_post.py validate <job_id> [--file F]
    python review_post.py render <job_id>                  # preview the GitLab note
    python review_post.py request-approval <job_id> [--via auto|orca|telegram|terminal]
    python review_post.py approval-status <job_id>
    python review_post.py approve|reject <job_id>          # human, interactive terminal only
    python review_post.py post <job_id> [--dry-run] [--no-notify]
    python review_post.py sweep [--dry-run]                # post approved / close rejected / retry notices (no LLM)
    python review_post.py notify <job_id>                  # re-send the requester notice of a posted review

review.json (default: <worktree>.review.json):
{
  "head_sha": "<diff_refs.head_sha from ws.json>",
  "verdict": "APPROVE | REQUEST_CHANGES | COMMENT",
  "summary": "markdown, 1-6 câu",
  "findings": [{"severity": "blocker|major|minor|nit", "title": "...", "body": "...",
                "file": "path/in/repo", "line": 42, "side": "new|old", "suggestion": "..."}],
  "tests": [{"cmd": "...", "result": "pass|fail|skipped", "note": "..."}],
  "compliance": {"Correctness": {"status": "ok|warn|fail", "note": "..."}, ...},   # optional:
                 # Correctness · Code Quality · Architecture · Security · Performance · Testing
  "positives": ["điểm làm tốt", ...],                                              # optional
  "date": "YYYY-MM-DD",                       # optional; the note's Date line (never file metadata)
  "verdict_note": "kèm 1 góp ý Major về test",  # optional, after the verdict
  "scope": "chỉ đọc code (diff + source nhánh). Chưa chạy mvn test.",   # optional "> Phạm vi" line
  "reviewed_commits": "all | new_only",
  "model": "provider:model"
}
REVIEW_NOTE_STYLE=full (default): the "Code Review Summary" layout agreed with the team — MR/Date/Commit/
Verdict header, "> Phạm vi" line, Overview, Critical/Major (Why:/Suggestion:) and Minor/Nitpicks (one
paragraph), Standards Compliance, Positive Observations, Files Reviewed (from ws.json).
REVIEW_NOTE_STYLE=compact: verdict + summary + one or two lines per finding (nits: title only).
REVIEW_INLINE_COMMENTS=1 also posts findings with file:line on the diff line (default 0: one note only).
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time

import review_common as rc
import review_rules as rr

VERDICTS = ("APPROVE", "REQUEST_CHANGES", "COMMENT")
SEVERITIES = ("blocker", "major", "minor", "nit")
SEV_ICON = {"blocker": "🔴", "major": "🟡", "minor": "🟢", "nit": "⚪"}   # same as code_review_prompt.md
VERDICT_ICON = {"APPROVE": "🟢 APPROVE", "REQUEST_CHANGES": "🔴 REQUEST_CHANGES", "COMMENT": "🟡 COMMENT"}
MAX_INLINE = 25
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


# --------------------------------------------------------------------------- review.json

def review_path(job: dict, override: str = "") -> str:
    return override or (job.get("worktree", "") + ".review.json")


def load_review(job: dict, override: str = "") -> dict:
    with open(review_path(job, override), encoding="utf-8") as f:
        return json.load(f)


def validate(job: dict, review: dict) -> list:
    errs = []
    head = (job.get("diff_refs") or {}).get("head_sha") or job.get("head_sha")
    if review.get("head_sha") != head:
        errs.append(f"head_sha {str(review.get('head_sha'))[:10]} != prepared head {str(head)[:10]}")
    if review.get("verdict") not in VERDICTS:
        errs.append(f"verdict must be one of {VERDICTS}")
    if not isinstance(review.get("summary"), str) or not review["summary"].strip():
        errs.append("summary is required")
    findings = review.get("findings")
    if not isinstance(findings, list):
        errs.append("findings must be a list")
        findings = []
    for i, f in enumerate(findings):
        if not isinstance(f, dict):
            errs.append(f"findings[{i}] must be an object")
            continue
        for k in ("title", "body", "suggestion", "file"):
            if f.get(k) is not None and not isinstance(f.get(k), str):
                errs.append(f"findings[{i}].{k} must be a string")
        if f.get("severity") not in SEVERITIES:
            errs.append(f"findings[{i}].severity must be one of {SEVERITIES}")
        if not f.get("title"):
            errs.append(f"findings[{i}].title is required")
        if f.get("line") is not None and not isinstance(f.get("line"), int):
            errs.append(f"findings[{i}].line must be an integer")
        if f.get("side", "new") not in ("new", "old"):
            errs.append(f"findings[{i}].side must be new|old")
    comp = review.get("compliance")
    if comp is not None:
        if not isinstance(comp, dict):
            errs.append("compliance must be an object {Category: {status, note}}")
        else:
            for cat, c in comp.items():
                if cat not in COMPLIANCE:
                    errs.append(f"compliance.{cat}: category must be one of {COMPLIANCE}")
                elif not isinstance(c, dict) or not isinstance(c.get("status"), str) \
                        or c["status"] not in STATUS_ICON:
                    errs.append(f"compliance.{cat}.status must be one of {tuple(STATUS_ICON)}")
                elif c.get("note") is not None and not isinstance(c.get("note"), str):
                    errs.append(f"compliance.{cat}.note must be a string")
    pos = review.get("positives")
    if pos is not None and not (isinstance(pos, list) and all(isinstance(p, str) for p in pos)):
        errs.append("positives must be a list of strings")
    for k in ("verdict_note", "scope"):
        if review.get(k) is not None and not (isinstance(review[k], str) and len(review[k]) <= 300
                                              and not re.search(r"[\r\n]", review[k])):
            errs.append(f"{k} must be one line of text (<= 300 chars)")
    if review.get("date") is not None and not (isinstance(review["date"], str) and _DATE.fullmatch(review["date"])):
        errs.append("date must be a string YYYY-MM-DD")
    if review.get("verdict") == "APPROVE" and any(isinstance(f, dict) and f.get("severity") == "blocker"
                                                 for f in findings):
        errs.append("verdict APPROVE with a blocker finding is contradictory")
    return errs


def _counts(findings: list) -> str:
    parts = [f"{sum(1 for f in findings if f.get('severity') == s)} {s}" for s in SEVERITIES]
    return " · ".join(p for p in parts if not p.startswith("0 ")) or "không có finding"


# Same layout as dev-automation/prompts/code_review_prompt.md (the format reviewers already know).
SECTIONS = (("🔴 Critical Issues (must fix before merge)", ("blocker",), "No critical issues found."),
            ("🟡 Major Suggestions (strongly recommended)", ("major",), "No major suggestions."),
            ("🟢 Minor / Nitpicks", ("minor", "nit"), "Code looks clean."))
COMPACT_SECTIONS = (("🔴 Phải sửa trước khi merge", ("blocker",), ""), ("🟡 Nên sửa", ("major",), ""),
                    ("🟢 Nhỏ", ("minor", "nit"), ""))
COMPLIANCE = ("Correctness", "Code Quality", "Architecture", "Security", "Performance", "Testing")
STATUS_ICON = {"ok": "✅", "warn": "🟡", "fail": "❌"}

WORD_JOINER = rr.WORD_JOINER    # invisible; GitLab needs a name char right after "@" to make a mention


def defang(text: str) -> str:
    """`@Test`, `@Override`, `@all`… would ping a GitLab user/group of that name. EVERY "@" gets an
    invisible WORD JOINER after it — independent of markdown parsing (escapes, emphasis, backticks
    can not undo it: Codex fmt R1/R5/R6). Only fenced code blocks, where GitLab never pings, are left
    byte-exact so suggested code still compiles when copied."""
    text = text or ""

    def fix(seg):
        # entities first: "&#64;all" would decode to a live @all in GitLab's DOM (Codex fmt R4 #1);
        # "&amp;#64;all" shows the characters literally and decodes to nothing
        seg = rr.ENTITY.sub("&amp;", seg)
        seg = rr.PCT_AT.sub("%2540", seg)                  # [x](%40victim) is a mention (R7 #1)
        return rr.MENTION.sub("@" + WORD_JOINER, seg)
    out, pos = [], 0
    for start, end in rr.fence_spans(text):
        out += [fix(text[pos:start]), text[start:end]]
        pos = end
    return "".join(out) + fix(text[pos:])


def _labelled(label: str, text: str) -> str:
    """One-line text follows the label; multi-line text (code fences, lists) starts on its own line at
    column 0, so a fence opens a real code block (Codex fmt R13 #1)."""
    text = text.strip()
    return f"{label}\n\n{text}" if "\n" in text else f"{label} {text}"


def _loc(f: dict) -> str:
    if not f.get("file"):
        return ""
    return f"[`{f['file']}`" + (f":{f['line']}" if f.get("line") else "") + "] "


def _review_date(review: dict) -> str:
    """From review.json only — never file metadata: the approval hash covers the rendered text, and
    touching an unchanged review must not void an approval (Codex fmt R14 #1). No date -> no line."""
    d = review.get("date")
    return d if isinstance(d, str) and _DATE.fullmatch(d) else ""


def _cell(text) -> str:
    """One markdown table cell: no line break of any kind (CR is a line ending too — R14 #2), no pipe."""
    return re.sub(r"\r\n|\r|\n", " ", str(text or "")).replace("|", "\\|")


def note_style() -> str:
    """REVIEW_NOTE_STYLE: full (default — the team's "Code Review Summary" layout) | compact (a short
    note, one or two lines per finding, no tables)."""
    s = (rc.config.get("REVIEW_NOTE_STYLE", "full") or "full").strip().lower()
    return s if s in ("compact", "full") else "full"


def inline_enabled() -> bool:
    """REVIEW_INLINE_COMMENTS=1: findings with file:line are ALSO posted as comments on the diff line.
    Default 0: everything stays in the one review note (less noise in the MR)."""
    return (rc.config.get("REVIEW_INLINE_COMMENTS", "0") or "0").strip().lower() in ("1", "true", "yes", "on")


COMPACT_MAX = 250             # characters of one-line prose per body / suggestion in the compact note


_LINKISH = re.compile(r"!?\[[^\]\n]*\]\([^)\n]*\)|<[^>\n]*>|https?://\S+")


def _protected(text: str) -> list:
    """[(start, end)] a cut must not fall inside: code spans (a run of N backticks closes only with a run
    of exactly N — CommonMark; an unmatched run protects the rest of the text), links, autolinks, URLs."""
    spans, i = [], 0
    while i < len(text):
        if text[i] == "\\":
            i += 2
            continue
        if text[i] != "`":
            i += 1
            continue
        j = i
        while j < len(text) and text[j] == "`":
            j += 1
        run = j - i
        m = re.compile(r"(?<!`)`{%d}(?!`)" % run).search(text, j)
        end = m.end() if m else len(text)
        spans.append((i, end))
        i = end
    spans += [m.span() for m in _LINKISH.finditer(text)]
    return spans


def _clip(text: str, limit: int = COMPACT_MAX) -> str:
    """One-line prose cut at the last sentence end (else word boundary) before `limit`, with " …". The
    cut is never inside a code span, link or URL (Codex compact R1 #2) and an unpaired `**` left at the
    end is dropped. Multi-line text (code blocks) is never cut."""
    text = text.strip()
    if "\n" in text or len(text) <= limit:
        return text
    prot = _protected(text)
    free = lambda k: all(not (s < k < e) for s, e in prot)          # noqa: E731
    sentence = [k + 1 for k in range(limit // 3, limit) if text[k] in ".;!?" and text[k + 1:k + 2] == " " and free(k + 1)]
    words = [k for k in range(1, limit) if text[k] == " " and free(k)]
    at = sentence[-1] if sentence else words[-1] if words else 0
    if not at:
        return text                                                  # one unbreakable token: keep it whole
    cut = text[:at].rstrip(" ,;:")
    # emphasis delimiters OUTSIDE code spans / links only: a literal `**` in code is not bold, and cutting
    # there would break the span (Codex compact R2 #1). The cut point stays outside every protected span.
    # an unpaired one is CLOSED, never cut away — cutting at it could leave nothing (R3 #1); `cut` ends in
    # a non-space, so the appended `**` is a valid closing delimiter
    stars = [m.start() for m in re.finditer(r"\*\*", cut) if all(not (s <= m.start() < e) for s, e in prot)]
    if len(stars) % 2:
        cut += "**"
    return cut + " …"


def _short_names(paths: list) -> dict:
    """{path: shortest trailing part that is still unique among `paths`} — `C.java`, or `api/C.java`
    when two findings sit in files of the same name (Codex compact R1 #3)."""
    parts = {p: re.split(r"[\\/]", p) for p in set(paths)}
    out = {}
    for p, segs in parts.items():
        for n in range(1, len(segs) + 1):
            tail = segs[-n:]
            if all(q == p or parts[q][-n:] != tail for q in parts):
                out[p] = "/".join(tail)
                break
        else:
            out[p] = p
    return out


def _short(f: dict) -> str:
    """'— body' when the body is one line (clipped); a multi-line body (code) goes on its own lines."""
    body = (f.get("body") or "").strip()
    return "" if not body else (f" — {_clip(body)}" if "\n" not in body else "\n\n" + body + "\n")


def _loc_short(f: dict, names: dict) -> str:
    """[`File.java`:12] — the shortest unique tail of the path (the full path doubled the line length)."""
    if not f.get("file"):
        return ""
    return f"[`{names.get(f['file'], f['file'])}`" + (f":{f['line']}" if f.get("line") else "") + "] "


def _render_compact(job: dict, review: dict, inline_ok: set, approver: str) -> str:
    findings = review.get("findings", [])
    names = _short_names([f["file"] for f in findings if f.get("file")])
    head = ((job.get("diff_refs") or {}).get("head_sha") or "")[:8]
    where = f" · `{job['source_branch']}` → `{job['target_branch']}`" if job.get("source_branch") else ""
    lines = [rr.marker(job), f"### 🤖 Code Review — {VERDICT_ICON[review['verdict']]}",
             f"!{job['iid']}{where} · `{head}`", "", _clip(review["summary"], 400)]
    n = 0
    for title, sevs, _ in COMPACT_SECTIONS:
        items = [(i, f) for i, f in enumerate(findings) if f.get("severity") in sevs]
        if not items:
            continue
        lines += ["", f"**{title}**", ""]
        for i, f in items:
            n += 1
            if i in inline_ok:
                lines.append(f"{n}. {_loc_short(f, names)}**{f['title']}** → _xem comment inline_")
                continue
            if f.get("severity") == "nit":                    # nitpicks: the title says it all
                lines.append(f"{n}. {_loc_short(f, names)}{f['title']}")
                continue
            lines.append(f"{n}. {_loc_short(f, names)}**{f['title']}**{_short(f)}")
            if f.get("suggestion"):
                lines.append(_labelled("   ↳ _Sửa:_", _clip(f["suggestion"])))
    if not findings:
        lines += ["", "Không có vấn đề cần sửa. 👍"]
    failed = [t for t in (review.get("tests") or []) if t.get("result") == "fail"]
    if failed:
        lines += ["", "**Test lỗi:** " + " · ".join(f"`{t.get('cmd', '?')}`" for t in failed)]
    c = (_ws(job).get("commits") or {})
    meta = [x for x in ("🤖 AI review", review.get("model") and f"model {review['model']}",
                        approver and f"duyệt bởi {approver}",
                        c and f"{c.get('new', '?')}/{c.get('total', '?')} commit mới") if x]
    lines += ["", "_" + " · ".join(meta) + "_"]
    return defang("\n".join(lines))


def _title(t: str) -> str:
    t = t.strip()
    return t if t[-1:] in ".!?:…" else t + "."


_SRC_ROOT = re.compile(r"^(?:.*?/)?src/(?:main|test|it|integrationTest)/(?:java|kotlin|groovy|scala|resources)/")


def _files_rel(paths: list) -> dict:
    """{path: path without its source root (src/main/java/, src/test/java/...) and without the package
    prefix ALL changed files share} - src/main/java/com/fis/etask/module/x/A.java -> module/x/A.java."""
    split = [_SRC_ROOT.sub("", p.replace("\\", "/")).split("/") for p in paths]
    common = 0
    if len(split) > 1:
        for parts in zip(*[s[:-1] for s in split]):
            if len(set(parts)) != 1:
                break
            common += 1
    common = max(0, common - 1)          # keep one shared folder for context (module/task/..., as the team writes it)
    out = {p: "/".join(s[common:]) for p, s in zip(paths, split)}
    seen = {}
    for label in out.values():
        seen[label] = seen.get(label, 0) + 1
    # a label shared by two files (same package in two modules / source roots): show their full paths
    return {p: (p.replace(chr(92), "/") if seen[label] > 1 else label) for p, label in out.items()}   # Codex layout R1 #2


def _scope_line(job: dict, review: dict, head: str) -> str:
    """'> Phạm vi: …' — the reviewer's own note, else what the tests list says was (not) run."""
    if isinstance(review.get("scope"), str) and review["scope"].strip():
        return "> Phạm vi: " + review["scope"].strip()
    ran = [t for t in (review.get("tests") or []) if t.get("result") in ("pass", "fail")]
    if ran:
        return "> Phạm vi: đã chạy " + " · ".join(f"`{t.get('cmd', '?')}` → {t['result']}" for t in ran) + "."
    return f"> Phạm vi: chỉ đọc code (diff + source nhánh tại `{head}`). Chưa chạy build/test."


def _render_full(job: dict, review: dict, inline_ok: set, approver: str) -> str:
    """The team's "Code Review Summary" layout (the sample agreed with the user, 2026-10-16)."""
    findings = review.get("findings", [])
    head = ((job.get("diff_refs") or {}).get("head_sha") or "")[:8]
    names = _short_names([f["file"] for f in findings if f.get("file")])
    ws = _ws(job)
    lines = [rr.marker(job), "## Code Review Summary", "",
             f"**MR:** !{job['iid']} - {job.get('title') or ''}".rstrip(" -"), "**Reviewer:** AI Agent"]
    date = _review_date(review)
    if not date and isinstance(ws.get("prepared_at"), (int, float)):
        date = time.strftime("%Y-%m-%d", time.localtime(ws["prepared_at"]))   # stable for this workspace
    if date:
        lines.append(f"**Date:** {date}")
    src = f" (source `{job['source_branch']}` -> target `{job['target_branch']}`)" if job.get("source_branch") else ""
    lines.append(f"**Commit:** `{head}`{src}")
    note = review.get("verdict_note")
    lines += [f"**Overall Verdict:** {VERDICT_ICON[review['verdict']]}"
              + (f" ({note.strip()})" if isinstance(note, str) and note.strip() else ""),
              "", _scope_line(job, review, head), "", "---", "", "### Overview", "", review["summary"].strip()]
    for title, sevs, empty in SECTIONS:
        lines += _section_break(lines) + [f"### {title}", ""]
        items = [(i, f) for i, f in enumerate(findings) if f.get("severity") in sevs]
        if not items:
            lines.append(empty)
        for n, (i, f) in enumerate(items, 1):
            loc = (f"[`{names[f['file']]}`" + (f":{f['line']}" if f.get("line") else "") + "] ") if f.get("file") else ""
            head_line = f"**{n}. {loc}{_title(f['title'])}**"
            if i in inline_ok:
                lines += [head_line + " _(chi tiết ở comment inline)_", ""]
                continue
            body, sug = (f.get("body") or "").strip(), (f.get("suggestion") or "").strip()
            if f.get("severity") in ("blocker", "major"):
                # one line each, as in the sample: a trailing double space is a Markdown hard line break,
                # so title / Why / Suggestion never run together into one paragraph (Codex layout R1 #1)
                parts = [head_line] + ([_labelled("Why:", body)] if body else []) \
                    + ([_labelled("Suggestion:", sug)] if sug else [])
                for k, part in enumerate(parts):
                    last = k == len(parts) - 1
                    if "\n" in part:                            # a block (code): a blank line closes it
                        lines += [part] + ([] if last else [""])
                    else:
                        lines.append(part if last else part + "  ")
            else:                                               # minor / nit: one paragraph, as in the sample
                text = " ".join(x for x in (body, sug) if x and "\n" not in x)
                lines.append(head_line + (" " + text if text else ""))
                if body and "\n" in body:
                    lines += ["", body]
                if sug and "\n" in sug:                         # code: labelled, the fence on its own lines
                    lines += ["", _labelled("Suggestion:", sug)]
            lines.append("")
    comp = review.get("compliance") or {}
    if comp:
        lines += _section_break(lines) + ["### Standards Compliance", "", "| Category | Status | Notes |",
                                          "|----------|--------|-------|"]
        for cat in COMPLIANCE:
            c = comp.get(cat) or {}
            lines.append(f"| {cat} | {STATUS_ICON.get(c.get('status'), '—')} | {_cell(c.get('note'))} |")
    pos = [p for p in (review.get("positives") or []) if isinstance(p, str) and p.strip()]
    if pos:
        lines += _section_break(lines) + ["### Positive Observations", ""] + [f"- {p.strip()}" for p in pos]
    files = ws.get("files") or []
    if files:
        rel = _files_rel([f.get("path", "?") for f in files])
        per = {}
        for f in findings:
            if f.get("file"):
                per[f["file"]] = per.get(f["file"], 0) + 1
        lines += _section_break(lines) + ["### Files Reviewed", "", "| File | Lines Changed | Issues Found |",
                                          "|------|--------------|--------------|"]
        for f in files:
            add, rem = f.get("added"), f.get("removed")
            size = "binary" if add is None else f"+{add} / -{rem}"
            path = _cell(rel.get(f.get("path", "?"), "?")).replace("`", "'")
            lines.append(f"| `{path}` | {size} | {per.get(f.get('path'), 0)} |")
    # ONE pass over the exact text the rules check: fences are judged on the whole note, never per
    # field (a field's fence could pair with another field's in the posted note — Codex fmt R2)
    return defang("\n".join(lines).rstrip() + "\n")


def _section_break(lines: list) -> list:
    while lines and lines[-1] == "":
        lines.pop()
    return ["", "---", ""]


def render_note(job: dict, review: dict, inline_ok: set, approver: str = "") -> str:
    if note_style() == "compact":
        return _render_compact(job, review, inline_ok, approver)
    return _render_full(job, review, inline_ok, approver)


def render_inline(f: dict) -> str:
    body = f"{SEV_ICON[f['severity']]} **{f['title']}**"
    if f.get("body"):
        body += "\n\n" + _labelled("**Why:**", f["body"])
    if f.get("suggestion"):
        body += "\n\n" + _labelled("**Suggestion:**", f["suggestion"])
    return defang(body)


def inline_candidates(review: dict) -> list:
    if not inline_enabled():
        return []
    findings = review.get("findings", [])
    return [i for i, f in enumerate(findings) if f.get("file") and f.get("line")][:MAX_INLINE]


def payload(job: dict, review: dict) -> tuple:
    """(note, inline bodies) — the superset of text that can reach GitLab for this review."""
    findings = review.get("findings", [])
    return render_note(job, review, set()), [render_inline(findings[i]) for i in inline_candidates(review)]


def payload_hash(job: dict, review: dict) -> str:
    note, inline = payload(job, review)
    head = (job.get("diff_refs") or {}).get("head_sha") or job.get("head_sha") or ""
    blob = json.dumps({"head": head, "note": note, "inline": inline}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _ws(job: dict) -> dict:
    try:
        with open(job.get("ws_path", ""), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


# --------------------------------------------------------------------------- approval

def _preview(job: dict, review: dict) -> str:
    f = review.get("findings", [])
    top = "\n".join(f"{SEV_ICON[x['severity']]} {x['title']}" for x in f[:8])
    return (f"Đăng review {job.get('project_path')}!{job['iid']} ({job.get('source_branch')} → "
            f"{job.get('target_branch')})?\n{VERDICT_ICON[review['verdict']]} — {_counts(f)}\n{top}\n"
            f"{job.get('web_url', '')}\nXem đầy đủ: python review_post.py render {job['id']}")


class ApprovalUncertain(RuntimeError):
    """A request for this exact payload was started but never recorded (crash while sending):
    it may already be on someone's phone. Sending again could produce two cards — a human checks."""


def request_approval(job: dict, via: str = "auto") -> dict:
    """Ask for approval of the CURRENT review payload — once per payload, whoever calls it
    (review_flow, the CLI, an Orca worker), even across crashes and concurrent callers."""
    jid = job["id"]
    with rc.file_lock(f"approval-{jid}", timeout=60):
        job = rc.load_job(jid) or job
        if job.get("quarantine"):
            raise PermissionError(f"job {jid} is quarantined ({job['quarantine'].get('reason', '')[:120]}) — "
                                  "a human must release it")
        review = load_review(job)
        errs = validate(job, review)
        if errs:
            raise ValueError("review.json invalid: " + "; ".join(errs))
        phash = payload_hash(job, review)
        a = job.get("approval") or {}
        if a.get("payload_hash") == phash and a.get("via"):
            return {"job_id": jid, "approval": a, "reused": True}
        intent = job.get("approval_intent") or {}
        if intent.get("payload_hash") == phash:
            raise ApprovalUncertain("an approval request for this exact review may already have been sent "
                                    "(interrupted while sending) — check Telegram, or approve in a terminal")
        job["approval_intent"] = {"payload_hash": phash, "at": rc.now()}   # durable BEFORE anything is sent
        rc.save_job(job, sync=False)
        out = _request_approval_locked(job, review, phash, via)
        return out


def _request_approval_locked(job: dict, review: dict, phash: str, via: str) -> dict:
    note, inline = payload(job, review)
    rules = rr.evaluate(job, note, inline)
    question = _preview(job, review)
    if rules["mode"] == "shadow":
        question += ("\n🤖 Auto-post (shadow): " + ("SẼ tự đăng" if rules["rules_passed"]
                     else "KHÔNG tự đăng — " + "; ".join(rules["reasons"][:3])))
    approval = {"via": None, "requested_at": rc.now(), "payload_hash": phash, "rules": rules}
    if via == "auto" and rules["auto_post"]:
        approval["via"] = "rules"                  # no human: the rules are re-checked at post time
    if not approval["via"] and via in ("auto", "telegram", "orca"):
        approval.update(_telegram_request(job, question) or {})
    if not approval["via"]:
        approval["via"] = "terminal"   # human runs: review_post.py approve <job>
    if rc.orca():                       # Orca board = display only, never an approval source
        st = rc.board_statuses()
        state = "tự đăng theo luật" if approval["via"] == "rules" else f"CHỜ DUYỆT qua {approval['via']}"
        rc.board_card(job, status=st["pending"], note=(f"{VERDICT_ICON[review['verdict']]} · "
                      f"{_counts(review.get('findings', []))} — {state}. Xem: python review_post.py render {job['id']}"))
    job.update(status="awaiting_approval", approval=approval, verdict=review["verdict"],
               review_file=review_path(job))
    job.pop("approval_intent", None)                  # recorded: the send is no longer uncertain
    rc.log_event(job, "approval_requested", via=approval["via"], rules_passed=rules["rules_passed"])
    rc.save_job(job)
    return {"job_id": job["id"], "approval": approval, "preview": question}


def _telegram_request(job: dict, question: str) -> dict:
    rc_tools = os.path.join(rc.tools_dir(), "..", "..", "remote-control", "tools")
    sys.path.insert(0, os.path.normpath(rc_tools))
    try:
        import approvals
        import tg_api
        bot = tg_api.approval_bot()
        chats = tg_api.allowed_chats(bot)
        if not chats:
            return {}
        req = approvals.create("review_post", f"post review {job['id']}", question, risk="write")
        tg_api.send_message(chats[0], question, reply_markup=tg_api.approve_keyboard(req), bot=bot,
                            parse_mode="")
        return {"via": "telegram", "id": req}
    except Exception as e:  # noqa: BLE001
        job["approval_error"] = str(e)[:200]
        return {}


def _current_payload(job: dict):
    try:
        review = load_review(job, job.get("review_file", ""))
    except (OSError, ValueError):
        return None, None
    if validate(job, review):
        return None, None
    return review, payload_hash(job, review)


def approval_status(job: dict) -> str:
    """'pending' | 'approved' | 'rejected' — for the payload as it is NOW."""
    a = job.get("approval") or {}
    if job.get("quarantine"):            # an agent tampered with this job: nothing counts until a human releases it
        return "pending"
    review, phash = _current_payload(job)
    if review is None:
        return "pending"
    if a.get("via") == "rules":
        note, inline = payload(job, review)
        return "approved" if rr.evaluate(job, note, inline)["auto_post"] else "pending"
    if a.get("via") == "terminal":
        decision = a.get("decision")
    elif a.get("via") == "telegram":
        sys.path.insert(0, os.path.normpath(os.path.join(rc.tools_dir(), "..", "..", "remote-control", "tools")))
        import approvals
        decision = {"approved": "approved", "denied": "rejected"}.get(approvals.get(a.get("id")).get("status"))
    else:
        return "pending"   # orca-board / orca gate / unknown: agents can set these, never trusted
    if decision == "rejected":
        return "rejected"
    # an approval counts only for the exact payload the human saw
    return "approved" if decision == "approved" and a.get("payload_hash") == phash else "pending"


def human_decide(job: dict, approved: bool) -> dict:
    if not sys.stdin.isatty():
        raise PermissionError("approve/reject needs an interactive terminal (a human) — agents cannot approve")
    review, phash = _current_payload(job)
    if review is None:
        raise ValueError("review.json missing or invalid — nothing to approve")
    print(render_note(job, review, set()))
    ans = input(f"\n{'APPROVE' if approved else 'REJECT'} THIS text for {job['id']} ({job.get('title', '')[:60]})? [y/N] ")
    if ans.strip().lower() not in ("y", "yes"):
        return {"job_id": job["id"], "changed": False}
    job.setdefault("approval", {}).update(via="terminal", decision="approved" if approved else "rejected",
                                          payload_hash=phash,
                                          by=os.environ.get("USER") or os.environ.get("USERNAME") or "terminal")
    rc.log_event(job, "decided", decision=job["approval"]["decision"])
    rc.save_job(job)
    return {"job_id": job["id"], "decision": job["approval"]["decision"]}


# --------------------------------------------------------------------------- post

def inline_marker(job: dict, i: int) -> str:
    """Invisible per-finding marker: an inline discussion can be found again after an uncertain POST."""
    return f"<!-- ai-review-inline {(job.get('diff_refs') or {}).get('head_sha', '')[:12]} #{i} -->"


def inline_already_posted(gl, job: dict) -> set:
    """Finding indices whose marker is already in a discussion of this MR — EVERY page, any index
    (reconcile, Codex R.M4 R1/R2)."""
    head = (job.get("diff_refs") or {}).get("head_sha", "")[:12]
    rx = re.compile(r"<!-- ai-review-inline " + re.escape(head) + r" #(\d+) -->")
    found = set()
    for d in rr.all_pages(gl, f"/merge_requests/{job['iid']}/discussions", "discussions"):
        for n in d.get("notes") or []:
            found |= {int(m) for m in rx.findall(n.get("body") or "")}
    return found


def _inline(gl, job: dict, f: dict, i: int = 0) -> bool:
    refs = job["diff_refs"]
    side_new = f.get("side", "new") == "new"
    pos = {"position_type": "text", "base_sha": refs["base_sha"], "start_sha": refs["start_sha"],
           "head_sha": refs["head_sha"], "new_path": f["file"], "old_path": f.get("old_file") or f["file"]}
    pos["new_line" if side_new else "old_line"] = f["line"]
    url = gl._api(f"/merge_requests/{job['iid']}/discussions")
    res = gl._request(url, method="POST", data={"body": render_inline(f) + "\n\n" + inline_marker(job, i),
                                                  "position": pos})
    if isinstance(res, dict) and res.get("uncertain"):
        # it may exist: stop, the next attempt reconciles by marker before posting anything (Codex R.M4 #2)
        job.setdefault("inline_uncertain", {})[refs["head_sha"]] = True
        _save_merged(job, sync=False)
        raise RuntimeError(f"inline comment #{i}: outcome unknown ({res.get('message', '')[:120]}) — "
                           "will reconcile before retrying")
    if isinstance(res, dict) and res.get("error"):
        return False                               # a definite rejection (e.g. line outside the diff)
    return isinstance(res, dict) and bool(res.get("id"))


def post(jid: str, dry_run: bool = False, notify: bool = True) -> dict:
    # one poster per job across processes (several group_watch sweepers may run)
    with rc.file_lock(f"post-{jid}", timeout=5 if not dry_run else 0.5, stale_after=1800):
        return _post_locked(jid, dry_run, notify)


def _safe_name(s: str) -> str:
    return re.sub(r"[^\w.-]", "", s or "")[:40]


def _save_merged(job: dict, seed: bool = False, sync: bool = True) -> None:
    """EVERY save of the posting path: under the "queue" lock, reload and merge what review_queue.add_job
    appended while GitLab was being called (another requester, a re-request for a new head) so nobody is
    dropped; `seed` = also create the pending notices (the "posted" save) — Codex notify R3 #2, R4 #1."""
    with rc.file_lock("queue"):
        cur = rc.load_job(job["id"]) or {}
        seen = {json.dumps(r, sort_keys=True) for r in job.get("requests", [])}
        for r in cur.get("requests", []):
            if json.dumps(r, sort_keys=True) not in seen:
                job.setdefault("requests", []).append(r)
        if cur.get("rerequested_head"):
            job["rerequested_head"] = cur["rerequested_head"]
        if "priority" in cur:
            job["priority"] = min(job.get("priority", cur["priority"]), cur["priority"])
        if seed:
            seed_notices(job)
        rc.save_job(job, sync=sync)


def _save_posted(job: dict, notify: bool) -> None:
    _save_merged(job, seed=notify)


def _post_locked(jid: str, dry_run: bool, notify: bool) -> dict:
    job = rc.load_job(jid)
    if not job:
        raise ValueError(f"no job {jid}")
    if job.get("status") in ("posted", "cancelled", "rejected"):
        return {"job_id": jid, "skipped": f"status {job.get('status')}"}
    decision = approval_status(job)
    if decision != "approved":
        raise PermissionError(f"not approved (approval: {decision}) — nothing posted")
    review = load_review(job, job.get("review_file", ""))
    errs = validate(job, review)
    if errs:
        raise ValueError("review.json invalid: " + "; ".join(errs))
    import review_ws
    st = review_ws.check_stale(jid)
    if st["stale"]:
        job.update(status="queued" if st["state"] == "opened" else "cancelled", head_sha=st["current_head"],
                   approval=None, previous_review=job.get("review_file"))
        rc.log_event(job, "stale", reviewed=str(st["reviewed_head"])[:8], current=str(st["current_head"])[:8])
        _save_merged(job)
        return {"job_id": jid, "stale": True, "requeued": job["status"] == "queued", "posted": False}

    findings = review.get("findings", [])
    inline_cands = inline_candidates(review)
    a = job.get("approval") or {}
    auto = a.get("via") == "rules"
    approver = "luật tự đăng" if auto else _safe_name(a.get("by") or a.get("via", ""))
    if dry_run:
        return {"job_id": jid, "dry_run": True, "auto": auto, "inline_candidates": len(inline_cands),
                "note": render_note(job, review, set(), approver)}

    head = (job.get("diff_refs") or {}).get("head_sha") or ""
    # survives a crash between inline and note — but only for findings that are inline candidates NOW:
    # with inline comments turned off since, the note must carry every finding in full, exactly as the
    # approved / dry-run payload did (Codex compact R1 #1)
    done = set(job.get("inline_done", {}).get(head, [])) & set(inline_cands)
    inline_ok = set(done)
    with rc.gitlab_project(job["project_id"]) as gl:
        if rr.already_posted(gl, job):                      # reconcile: lost response on a previous try
            job.update(status="posted", posted_at=rc.now(), auto_posted=auto, reconciled=True)
            rc.log_event(job, "posted_reconciled")
            _save_posted(job, notify)              # the lost try never told anyone: sweep delivers these
            return {"job_id": jid, "posted": True, "reconciled": True}
        if (job.get("inline_uncertain") or {}).get(head):    # a previous inline POST had no clear outcome
            found = inline_already_posted(gl, job) & set(inline_cands)
            done |= found
            inline_ok |= found
            job.setdefault("inline_done", {})[head] = sorted(inline_ok)
            job["inline_uncertain"].pop(head, None)
            _save_merged(job, sync=False)
        for i in inline_cands:
            if i in done:
                continue
            if _inline(gl, job, findings[i], i):
                inline_ok.add(i)
                job.setdefault("inline_done", {})[head] = sorted(inline_ok)
                _save_merged(job, sync=False)
        note = rc.gl_check(gl.add_mr_comment(job["iid"], render_note(job, review, inline_ok, approver)),
                           "post note")
        if not (isinstance(note, dict) and note.get("id")):          # Codex R.M4 #1: no id = not posted
            raise RuntimeError(f"GitLab post note returned no note id: {str(note)[:200]}")
    ws = _ws(job)
    import review_ws as rw
    reviewed = [c for c in (ws.get("commits") or {}).get("list", []) if not c.get("merge")]
    rw.ledger_add(job["project_id"], job["iid"], reviewed)
    job.update(status="posted", posted_at=rc.now(), note_id=note.get("id"), inline_posted=len(inline_ok),
               auto_posted=auto)
    rc.log_event(job, "posted", verdict=review["verdict"], inline=len(inline_ok), auto=auto)
    _save_posted(job, notify)                      # notices seeded in the SAME save as "posted"
    out = {"job_id": jid, "posted": True, "verdict": review["verdict"], "inline": len(inline_ok),
           "note_id": note.get("id")}
    rc.board_card(job, note=f"✅ Đã đăng review ({review['verdict']}, {len(inline_ok)} inline) — {job.get('web_url', '')}")
    if notify:
        out["notified"] = deliver_notices(jid, review)       # under post()'s lock
    if not rw.keep_worktrees():
        # the review is on GitLab: its worktree (one Orca card per review) is no longer needed —
        # review.json / ws.json stay for notices and history (REVIEW_KEEP_WORKTREES=1 keeps it)
        try:
            out["worktree_removed"] = bool(rw.cleanup(jid).get("removed"))
        except Exception as e:  # noqa: BLE001 — cleanup is best effort; sweep's prune retries
            out["worktree_removed"] = False
            out["worktree_error"] = str(e)[:200]
    return out


NOTIFY_MAX_ATTEMPTS = 5
NOTIFY_BACKOFF_S = 60          # retry n waits n * this (sweep runs every ~30 s)
NOTIFY_SENDING_STALE_S = 600   # a "sending" mark older than this = the sender died mid-send
# credentials the child must read FRESH from .env: a long-running parent (Coordinator) holds the values it
# loaded at start, and TChat rotates its tokens (the child would send a stale one)
_STALE_ENV = ("FCHAT_BEARER_TOKEN", "FCHAT_REFRESH_TOKEN", "FCHAT_WS_TOKEN")
_TOKENISH = re.compile(r"eyJ[\w-]+\.[\w-]+\.[\w-]+|[A-Za-z0-9_\-+/=]{32,}")
# notice states: pending -> sending -> sent | failed (retried with backoff) | unconfirmed | uncertain.
# unconfirmed (TChat did not echo the message) and uncertain (sender died mid-send) are NEVER re-sent
# automatically — the message may have arrived; a human decides (`review_post.py notify <job>`).
NOTICE_OPEN = ("failed", "unconfirmed", "uncertain")


def _notify_key(r: dict) -> str:
    return f"{r.get('group')}|{r.get('by_id') or ''}"


def _clean_error(text: str) -> str:
    """Redact BEFORE truncating (a token cut in half would no longer match — Codex notify R1 #6)."""
    return _TOKENISH.sub("<redacted>", str(text or "")).strip()[:200]


def _json_obj(text: str):
    try:
        t = (text or "").strip()
        return json.loads(t) if t.startswith("{") else None
    except ValueError:
        return None


def _notify_result(p) -> tuple:
    """(state, error) from notify_group's exit code + JSON output. `failed` (auto-retried) ONLY for a
    structured error — notify_group / send.py report those BEFORE anything is sent (bad config, group type
    not resolvable, HTTP error). A child that dies without a verdict may have sent: `uncertain` (R3 #1)."""
    res = _json_obj(p.stdout)
    if p.returncode == 0 and isinstance(res, dict) and not res.get("error"):
        # send.py: "sent" with "confirmed": false = no echo from TChat — not proof of delivery (R1 #1)
        return ("sent", "") if res.get("confirmed") else ("unconfirmed", "TChat không xác nhận đã nhận tin")
    err = res if isinstance(res, dict) and res.get("error") else _json_obj(p.stderr)
    if isinstance(err, dict) and err.get("error"):
        return "failed", _clean_error(err.get("message") or f"exit {p.returncode}")
    tail = [ln for ln in (p.stderr or "").splitlines() if ln.strip() and not ln.startswith("[WARN]")]
    return "uncertain", _clean_error("tiến trình gửi dừng bất thường — không rõ tin đã tới chưa: "
                                     + (tail[-1] if tail else f"exit {p.returncode}"))


def _requesters(job: dict) -> dict:
    """{key: request} — one notice per (group, person), FChat requests only."""
    out = {}
    for r in job.get("requests", []):
        if r.get("source") == "fchat" and r.get("group"):
            out.setdefault(_notify_key(r), r)
    return out


def notice_text(job: dict, review: dict) -> str:
    return (f"🔎 Review {job.get('project_path')}!{job['iid']} — {VERDICT_ICON[review['verdict']]}: "
            f"{_counts(review.get('findings', []))}. {job.get('web_url', '')}")


def _send_one(r: dict, text: str) -> tuple:
    script = os.path.normpath(os.path.join(rc.tools_dir(), "..", "..", "tchat-automation", "tools", "notify_group.py"))
    args = [sys.executable, script, "--group", r["group"], "--text", text]
    if r.get("group_type"):
        args += ["--group-type", r["group_type"]]
    if r.get("by_id") and r.get("by"):
        args += ["--user-id", r["by_id"], "--user-name", r["by"]]
    env = {k: v for k, v in os.environ.items() if k not in _STALE_ENV}
    try:
        p = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=60, env=env)
    except subprocess.TimeoutExpired:
        # send.py transmits FIRST and then waits for the echo: the message may have arrived (R2 #1)
        return "uncertain", "hết thời gian chờ khi gửi — không rõ tin đã tới chưa"
    except OSError as e:                                       # the child never started: nothing was sent
        return "failed", _clean_error(f"{type(e).__name__}: {e}")
    return _notify_result(p)


def seed_notices(job: dict) -> None:
    """Record who must be told, in the SAME save that marks the job posted: a crash before any notice is
    sent leaves `pending` entries that sweep() delivers (Codex notify R1 #2)."""
    notices = job.setdefault("notices", {})
    for key in _requesters(job):
        notices.setdefault(key, {"state": "pending", "attempts": 0, "at": rc.now()})


def _due(n: dict, now: int) -> bool:
    if n.get("state") == "pending":
        return True
    return (n.get("state") == "failed" and n.get("attempts", 0) < NOTIFY_MAX_ATTEMPTS
            and now - (n.get("at") or 0) >= NOTIFY_BACKOFF_S * max(1, n.get("attempts", 1)))


def _update_posted(jid: str, note_id, change) -> dict:
    """Apply `change(job)` and save — under the "queue" lock every job writer uses (review_queue.add_job
    re-queues a posted job under it), and only while the job is still THIS posted review (same note id):
    a job replaced by a new request / head is never overwritten with stale state (Codex notify R2 #2).
    Returns the saved job, or None when the review was replaced."""
    with rc.file_lock("queue"):
        job = rc.load_job(jid)
        if not job or job.get("status") != "posted" or job.get("note_id") != note_id:
            return None
        change(job)
        rc.save_job(job, sync=False)
        return job


def deliver_notices(jid: str, review: dict = None, force: bool = False) -> list:
    """Send every notice that is due — per recipient (R1 #4) — recording `sending` before and the outcome
    after EACH send (R1 #3), each write through _update_posted. Caller holds the post-<jid> lock (one
    sender per job). `force` (a human): also failed-out / unconfirmed / uncertain ones."""
    job = rc.load_job(jid)
    if not job or job.get("status") != "posted":
        return []
    note_id = job.get("note_id")

    def settle_dead(j):                                        # a sender that died mid-send
        now = rc.now()
        for n in (j.get("notices") or {}).values():
            if n.get("state") == "sending" and now - (n.get("at") or 0) > NOTIFY_SENDING_STALE_S:
                n.update(state="uncertain", error="tiến trình gửi bị dừng giữa chừng — không rõ tin đã tới chưa")
    job = _update_posted(jid, note_id, settle_dead)
    if job is None:
        return []
    reqs, now = _requesters(job), rc.now()
    keys = [k for k, n in (job.get("notices") or {}).items() if k in reqs
            and (_due(n, now) or (force and n.get("state") in NOTICE_OPEN + ("pending",)))]
    if not keys:
        return []
    review = review or load_review(job, job.get("review_file", ""))
    text, out = notice_text(job, review), []
    for k in keys:
        def mark(j, k=k):
            n = j["notices"][k]
            n.update(state="sending", at=rc.now(), attempts=(0 if force else n.get("attempts", 0)) + 1)
        if _update_posted(jid, note_id, mark) is None:
            break                                              # replaced meanwhile: this delivery is obsolete
        state, err = _send_one(reqs[k], text)

        def record(j, k=k, state=state, err=err):
            j["notices"][k].update(state=state, at=rc.now(), error=err)
            rc.log_event(j, "notified" if state == "sent" else f"notify_{state}", **({"error": err[:120]} if err else {}))
        _update_posted(jid, note_id, record)
        out.append({"key": k, "state": state, "error": err})
    return out


def _notices_due(job: dict) -> bool:
    now = rc.now()
    return any(_due(n, now) or (n.get("state") == "sending" and now - (n.get("at") or 0) > NOTIFY_SENDING_STALE_S)
               for n in (job.get("notices") or {}).values())


def close_idle_workers(dry_run: bool = False) -> list:
    """Close the Orca tab of a worker whose job left `running` (the agent session stays
    open after its turn). Skips a terminal still running another job."""
    if rc.config.get("REVIEW_CLOSE_WORKER_TERMINAL", "1").lower() in ("0", "false", "no"):
        return []
    o = rc.orca()
    if not o:
        return []
    grace = rc.cfg_int("REVIEW_CLOSE_WORKER_AFTER_SEC", 60)
    jobs = rc.all_jobs()
    busy = {j.get("worker_terminal") for j in jobs if j.get("status") == "running"}
    closed = []
    for j in jobs:
        h = j.get("worker_terminal")
        if (not h or j.get("worker_terminal_closed") or j.get("status") == "running" or h in busy
                or rc.now() - j.get("updated", 0) < grace):
            continue
        if h == os.environ.get("ORCA_TERMINAL_HANDLE"):
            continue                                   # never close the caller's own tab
        if not dry_run:
            err = ""
            try:
                o.terminal_close(h)
            except Exception as e:  # noqa: BLE001 — already closed by hand, etc.
                err = str(e)[:120]
            # reload + save under the job's lock: a snapshot taken above must not overwrite what another
            # process recorded meanwhile, e.g. a delivered notice (Codex notify R1 #5)
            try:
                with rc.file_lock("queue", timeout=5):         # the lock every job writer uses (R2 #2)
                    cur = rc.load_job(j["id"]) or j
                    cur["worker_terminal_closed"] = True
                    if err:
                        cur["worker_terminal_error"] = err
                    rc.save_job(cur, sync=False)
            except TimeoutError:
                continue                               # busy: the next sweep marks it
        closed.append({"job_id": j["id"], "closed_terminal": h})
    return closed


def notify_again(jid: str) -> dict:
    """Human re-send: every requester not yet told (failed, unconfirmed, uncertain, pending) gets the notice."""
    with rc.file_lock(f"post-{jid}", timeout=5, stale_after=1800):
        job = rc.load_job(jid)
        if not job or job.get("status") != "posted":
            raise ValueError(f"job {jid} is not posted (status {job and job.get('status')})")
        _update_posted(jid, job.get("note_id"), seed_notices)   # a job posted before notices existed
        res = deliver_notices(jid, force=True)
    return {"job_id": jid, "notified": res}


def sweep(dry_run: bool = False) -> dict:
    actions = close_idle_workers(dry_run)
    import review_ws
    if not dry_run and not review_ws.keep_worktrees():
        try:                                       # worktrees of finished / superseded reviews
            pruned = review_ws.prune().get("removed") or []
            if pruned:
                actions.append({"pruned_worktrees": len(pruned)})
        except Exception as e:  # noqa: BLE001 — never let housekeeping stop posting
            actions.append({"prune_error": str(e)[:200]})
    for job in rc.all_jobs():
        if job.get("status") == "posted" and not dry_run and _notices_due(job):
            try:
                with rc.file_lock(f"post-{job['id']}", timeout=5, stale_after=1800):   # one sender per job
                    res = deliver_notices(job["id"])
                if res:
                    actions.append({"job_id": job["id"], "notices": [{"state": x["state"], "error": x["error"]} for x in res]})
            except (OSError, ValueError, TimeoutError) as e:       # review file gone, lock busy…
                actions.append({"job_id": job["id"], "notify_error": _clean_error(e)})
            continue
        if job.get("status") != "awaiting_approval":
            continue
        decision = approval_status(job)
        if decision == "approved":
            try:
                actions.append(post(job["id"], dry_run=dry_run))
            except (PermissionError, ValueError, RuntimeError, TimeoutError) as e:
                actions.append({"job_id": job["id"], "error": str(e)})
        elif decision == "rejected" and not dry_run:
            rc.board_card(job, note="❌ Review bị từ chối — không đăng.")
            job["status"] = "rejected"
            rc.log_event(job, "rejected")
            rc.save_job(job)
            actions.append({"job_id": job["id"], "rejected": True})
    return {"actions": actions}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="review_post.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("validate", "render", "approval-status", "approve", "reject"):
        p = sub.add_parser(name)
        p.add_argument("job_id")
        if name == "validate":
            p.add_argument("--file", default="")
    p = sub.add_parser("request-approval")
    p.add_argument("job_id")
    p.add_argument("--via", default="auto", choices=["auto", "orca", "telegram", "terminal"])
    p = sub.add_parser("post")
    p.add_argument("job_id")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-notify", action="store_true")
    p = sub.add_parser("sweep")
    p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("notify")                  # [WRITE] re-send the requester notice of a POSTED review
    p.add_argument("job_id")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "sweep":
            return rc.pc.emit(sweep(a.dry_run))
        job = rc.load_job(a.job_id)
        if not job:
            return rc.pc.emit({"error": True, "message": f"no job {a.job_id}"})
        if a.cmd == "validate":
            errs = validate(job, load_review(job, a.file))
            out = {"job_id": a.job_id, "valid": not errs, "errors": errs}
            rc.pc.emit(out)
            return 0 if not errs else 1
        if a.cmd == "render":
            review = load_review(job, job.get("review_file", ""))
            print(render_note(job, review, set()))
            return 0
        if a.cmd == "request-approval":
            out = request_approval(job, a.via)
        elif a.cmd == "approval-status":
            out = {"job_id": a.job_id, "approval": approval_status(job)}
        elif a.cmd in ("approve", "reject"):
            out = human_decide(job, a.cmd == "approve")
        elif a.cmd == "notify":
            out = notify_again(a.job_id)
        else:
            out = post(a.job_id, a.dry_run, notify=not a.no_notify)
    except FileNotFoundError as e:
        out = {"error": True, "message": f"review file missing: {e.filename}"}
    except (PermissionError, ValueError, RuntimeError, TimeoutError) as e:
        out = {"error": True, "message": str(e)}
    return rc.pc.emit(out)


if __name__ == "__main__":
    sys.exit(main())
