#!/usr/bin/env python3
"""Coverage manifest (temp/codex/ocr-eval/FINAL.md step 1): the MACHINE lists every file of the pinned diff; the
reviewer must account for each one in review.json "coverage"; a gap (skipped / truncated) needs a human.

Flag REVIEW_COVERAGE = off (default) | shadow | on — READ AT PREPARE and pinned on the job (coverage_mode), like
the manifest itself: a review keeps the mode it was prepared with, so flipping the flag never changes the verdict
of validate / the rules / an approval half-way (Codex ocr-eval step1 R3–R9). The new mode applies from the next
prepare (new head, or a new request).
  off    : nothing changes (no manifest, same ws.json / job / approval hash / diff output as before)
  shadow : prepare writes the manifest, the prompt asks for "coverage", validate/rules REPORT problems only
  on     : a missing / unknown / duplicate entry is a validation error; a gap blocks auto-post (a human approves)

Files
  <wt>.coverage.json  written by prepare (base/start/head + every file); its sha256 is on the job (coverage_sha) —
                      edited after prepare = error, like prev.json (review_rounds.prev_state)
  <wt>.served.jsonl   what `review_ws.py diff` actually handed out. The agent runs that command, so this is a
                      SIGNAL shown to the human, never a gate. No record = unknown.

review.json "coverage": [{"path": "...", "status": "reviewed|reused|skipped|truncated", "reason": "..."}]
  reused    = re-review only, and only for a file whose diff is byte-identical to the previous round's (same
              status, rename source, modes and blobs: prev.json reuse.prev_files, from that round's archived,
              hash-verified manifest) and that the previous round REVIEWED (or reused) with a coverage that passed
              the check (reuse.prev_coverage) — a gap is never carried over as "reused"
  skipped   = not reviewed (reason required)
  truncated = only part of the file's diff was reviewed (reason required)
Files the manifest marks "excluded" (binary, lock, minified, REVIEW_COVERAGE_EXCLUDE globs) need no entry.

The fingerprint (pinned mode + manifest + the reviewer's statuses) goes into the approval hash: an approval is
void when the reviewed scope / statuses change or the job is prepared again under another mode, even if the
comment text does not. Flipping the flag alone never voids one.

The served log ("unverified") is a signal shown to the human, never a gate — also under "on" (design choice:
the agent may read the diff with plain git in the worktree, so "not served" is not "not read").
"""

import fnmatch
import hashlib
import json
import os
import time

import review_common as rc

MODES = ("off", "shadow", "on")
STATUSES = ("reviewed", "reused", "skipped", "truncated")
GAPS = ("skipped", "truncated")
REUSABLE_FROM = ("reviewed", "reused")
VERSION = 1
# machine exclusions: never worth an LLM read; anything else must be accounted for
DEFAULT_EXCLUDE = ("**/package-lock.json", "**/yarn.lock", "**/pnpm-lock.yaml", "**/npm-shrinkwrap.json",
                   "**/Cargo.lock", "**/poetry.lock", "**/Gemfile.lock", "**/composer.lock", "**/go.sum",
                   "**/*.min.js", "**/*.min.css", "**/*.map")
HDR = "diff --git "


def mode() -> str:
    """The flag NOW — only prepare reads it; everything after prepare uses job_mode()."""
    m = (rc.config.get("REVIEW_COVERAGE", "off") or "off").strip().lower()
    return m if m in MODES else "off"


def job_mode(job: dict) -> str:
    """The mode this review was prepared with ("off" = no manifest)."""
    if not job.get("coverage_sha"):
        return "off"
    m = job.get("coverage_mode")
    return m if m in ("shadow", "on") else "shadow"


def path_of(worktree: str) -> str:
    return (worktree or "") + ".coverage.json"


def served_path(worktree: str) -> str:
    return (worktree or "") + ".served.jsonl"


def _sha_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def file_sha(path: str) -> str:
    try:
        with open(path, "rb") as f:
            return _sha_bytes(f.read())
    except OSError:
        return ""


def _str(x) -> str:
    return x if isinstance(x, str) else ""


def _clean(x):
    """Agent text that reaches a saved record: a lone surrogate would make the UTF-8 save fail (step1 R11 #3)."""
    if isinstance(x, str):
        return x.encode("utf-8", "replace").decode("utf-8")
    if isinstance(x, list):
        return [_clean(v) for v in x]
    if isinstance(x, dict):
        return {_clean(k): _clean(v) for k, v in x.items()}
    return x


# --------------------------------------------------------------------------- build (prepare side)

def _match(path: str, pat: str) -> bool:
    p = path                    # git paths always use "/" (Windows too); a "\\" is part of a POSIX name (R13 #2)
    if fnmatch.fnmatchcase(p, pat):
        return True
    return pat.startswith("**/") and fnmatch.fnmatchcase(p, pat[3:])     # "**/x" also matches "x" at the root


def excluded_reason(path: str, binary: bool) -> str:
    if binary:
        return "binary"
    extra = [x.strip() for x in (rc.config.get("REVIEW_COVERAGE_EXCLUDE", "") or "").split(",") if x.strip()]
    for pat in (*DEFAULT_EXCLUDE, *extra):
        if _match(path, pat):
            return f"pattern {pat}"
    return ""


def parse_raw(raw_z: str) -> list:
    """`git diff --raw -z -M --no-abbrev` → [{status, path, old_path, id}] in git's order (paths are
    NUL-separated: exact). id = [old mode, new mode, old blob, new blob]: two rounds with the same status,
    old_path and id show the SAME diff for that file — what a "reused" must prove."""
    out, parts, i = [], raw_z.split("\x00"), 0
    while i < len(parts):
        meta = parts[i]
        if not meta.startswith(":"):
            i += 1
            continue
        fields = meta[1:].split()
        letter = fields[-1][:1]
        if letter in ("R", "C"):
            old, new = parts[i + 1], parts[i + 2]
            i += 3
        else:
            old = new = parts[i + 1]
            i += 2
        out.append({"status": letter, "path": new, "old_path": old if letter in ("R", "C") else None,
                    "id": fields[:4]})
    return out


def parse_numstat(num_z: str) -> dict:
    """`git diff --numstat -z -M` → {new_path: (added|None, removed|None)} (None = binary).
    Record = "add<TAB>rem<TAB>path" (the path may itself hold a tab) or "add<TAB>rem<TAB>" + old NUL new."""
    out, parts, i = {}, num_z.split("\x00"), 0
    while i < len(parts):
        rec = parts[i]
        if not rec:
            i += 1
            continue
        cols = rec.split("\t", 2)
        if len(cols) < 3:
            i += 1
            continue
        if cols[2] == "":                                   # rename/copy: old, new follow
            path = parts[i + 2] if i + 2 < len(parts) else ""
            i += 3
        else:
            path = cols[2]
            i += 1
        add, rem = cols[0], cols[1]
        out[path] = (None if add == "-" else int(add), None if rem == "-" else int(rem))
    return out


def section_sizes(diff_text: str) -> list:
    """[(header line, line count)] of each `diff --git` section, in order."""
    out = []
    for ln in diff_text.splitlines():
        if ln.startswith(HDR):
            out.append([ln, 0])
        if out:
            out[-1][1] += 1
    return [tuple(x) for x in out]


def _header_names(entry: dict) -> tuple:
    new, old = entry["path"], entry.get("old_path") or entry["path"]
    return f"a/{old} b/{new}"


def pair_sizes(entries: list, sections: list) -> list:
    """Line count per manifest entry, or all None when the sections cannot be paired 1:1 with the entries
    (count differs, or a header does not spell the entry's paths — git quotes names with a tab / quote)."""
    if len(sections) != len(entries):
        return [None] * len(entries)
    for e, (hdr, _) in zip(entries, sections):
        if hdr != HDR + _header_names(e):
            return [None] * len(entries)
    return [n for _, n in sections]


def build(git, base: str, head: str, start: str = "", exact=None) -> dict:
    """git(*args) -> stdout. The manifest of base..head as GitLab shows it (diff_refs, -M).
    exact(*args) -> stdout with NO newline translation (Repo.git_exact): the NUL-separated listings must keep a
    "\r" inside a file name, or two names collapse into one path (step1 R12 #1)."""
    q = ("-c", "core.quotePath=false")
    exact = exact or git
    entries = parse_raw(exact(*q, "diff", "--raw", "-z", "-M", "--no-abbrev", base, head))
    nums = parse_numstat(exact(*q, "diff", "--numstat", "-z", "-M", base, head))
    sizes = pair_sizes(entries, section_sizes(git(*q, "diff", "-M", "-U5", base, head)))  # = `review_ws.py diff`
    files = []
    for e, size in zip(entries, sizes):
        add, rem = nums.get(e["path"], (None, None))
        binary = e["path"] in nums and add is None and rem is None
        why = excluded_reason(e["path"], binary)
        if why and not binary and e.get("old_path") and not excluded_reason(e["old_path"], False):
            why = ""            # a source file renamed to an excluded-looking name is still reviewed (R14 #1)
        files.append({**e, "added": add, "removed": rem, "binary": binary, "diff_lines": size,
                      "excluded": why or None})
    return {"version": VERSION, "base_sha": base, "start_sha": start or None, "head_sha": head, "files": files}


def write_tmp(worktree: str, manifest: dict, tag: str) -> tuple:
    """(tmp path, sha) — published by prepare (os.replace) only once the job is verified unchanged."""
    tmp = f"{path_of(worktree)}.tmp-{tag}-{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:          # ascii: a non-UTF-8 file name (surrogate) still writes
        json.dump(manifest, f, ensure_ascii=True, indent=2)
    return tmp, file_sha(tmp)


# --------------------------------------------------------------------------- served log (review_ws diff)

def served_record(path_arg: str, text_lines: list, max_lines: int) -> dict:
    """One `review_ws.py diff` call. With --path: that file, full or cut. Without: how many sections were
    shown whole (in diff order = manifest order) and whether the last shown one was cut. A cut that falls
    exactly on a section boundary leaves that section whole."""
    cut = len(text_lines) > max_lines
    if path_arg:
        return {"path": path_arg, "full": bool(text_lines) and not cut}      # empty = wrong path / nothing
    shown = text_lines[:max_lines]
    sections = sum(1 for ln in shown if ln.startswith(HDR))
    last_whole = not cut or text_lines[max_lines].startswith(HDR)
    return {"path": None, "whole": sections if last_whole else max(sections - 1, 0), "cut": cut}


def record_served(worktree: str, path_arg: str, text_lines: list, max_lines: int) -> None:
    if not worktree:
        return
    rec = {"at": int(time.time()), "lines": len(text_lines), "max_lines": max_lines,
           **served_record(path_arg, text_lines, max_lines)}
    try:
        with open(served_path(worktree), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=True) + "\n")   # a non-UTF-8 name (surrogate) still writes
    except OSError:
        pass                                            # a signal only: never fail the diff over it


def served_signal(worktree: str, manifest: dict) -> dict:
    """{path: "full"|"cut"} from the served log (served whole at least once = full). Missing = unknown.
    Whole-diff records map to files by diff order only when prepare could pair sections with files."""
    files = [f.get("path") for f in (manifest or {}).get("files") or [] if isinstance(f, dict)]
    paired = bool(files) and all(isinstance(f.get("diff_lines"), int) for f in manifest["files"]
                                 if isinstance(f, dict))
    out = {}
    try:
        with open(served_path(worktree), encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return out
    for ln in lines:
        try:
            rec = json.loads(ln)
        except ValueError:
            continue
        if not isinstance(rec, dict):
            continue
        p = rec.get("path")
        if isinstance(p, str) and p:
            if rec.get("full") is True:
                out[p] = "full"
            else:
                out.setdefault(p, "cut")
        elif p is None and paired and isinstance(rec.get("whole"), int):
            whole = max(0, min(rec["whole"], len(files)))
            for name in files[:whole]:
                out[name] = "full"
            if rec.get("cut") is True and whole < len(files):
                out.setdefault(files[whole], "cut")
    return out


# --------------------------------------------------------------------------- validate side

def state(job: dict) -> tuple:
    """(manifest, error). No coverage_sha on the job = this prepare built none → (None, None).
    The file is read ONCE: the bytes hashed are the bytes parsed."""
    want = job.get("coverage_sha")
    if not want:
        return None, None
    path = path_of(job.get("worktree") or "") if job.get("worktree") else ""
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        raw = None
    if raw is None or _sha_bytes(raw) != want:
        return None, (f"{path or 'coverage.json'} is missing or was changed after prepare — prepare the job again "
                      "(review_ws.py prepare) instead of editing it")
    try:
        m = json.loads(raw.decode("utf-8"))
    except ValueError:
        m = None
    if not (isinstance(m, dict) and isinstance(m.get("files"), list)
            and all(isinstance(f, dict) and isinstance(f.get("path"), str) for f in m["files"])):
        return None, f"{path} is not a valid coverage manifest — prepare the job again"
    return m, None


def file_key(entry: dict) -> list:
    """What makes one file's diff identical between two rounds (status, rename source, modes + blobs)."""
    return [entry.get("status"), entry.get("old_path"), entry.get("id")]


def _reuse_error(p: str, prev, entry: dict) -> str:
    if not isinstance(prev, dict):
        return f"coverage[{p}]: reused only on a re-review (no previous round)"
    reuse = prev.get("reuse")
    if not (isinstance(reuse, dict) and isinstance(reuse.get("prev_files"), dict)
            and isinstance(reuse.get("prev_coverage"), dict)):
        return (f"coverage[{p}]: reused not allowed — the previous round has no coverage manifest, "
                "review it")
    if not entry.get("id") or reuse["prev_files"].get(p) != file_key(entry):
        return f"coverage[{p}]: reused but its diff is not the one reviewed in the previous round"
    if reuse["prev_coverage"].get(p) not in REUSABLE_FROM:
        return (f"coverage[{p}]: reused but the previous round did not review it "
                f"({reuse['prev_coverage'].get(p) or 'no entry'}) — review it now")
    return ""


def check(review: dict, manifest: dict, prev) -> dict:
    """{"errors": [...], "gaps": [...]} for review["coverage"] against the manifest.
    errors = the coverage list does not account for the diff; gaps = files a human must know were not
    (fully) reviewed. Never raises on malformed input."""
    errs, gaps = [], []
    files = [f for f in manifest.get("files", []) if isinstance(f, dict) and isinstance(f.get("path"), str)]
    need = {f["path"] for f in files if not f.get("excluded")}
    known = {f["path"]: f for f in files}
    if len(known) != len(files):          # two diff entries, one path: one status cannot cover both
        return {"errors": ["coverage manifest lists the same path twice — prepare again / review by hand"],
                "gaps": []}
    cov = review.get("coverage") if isinstance(review, dict) else None
    if not isinstance(cov, list):
        return {"errors": [f'"coverage" must list every file of the diff ({len(need)} files) with a status '
                           f"{STATUSES}"], "gaps": []}
    seen = set()
    for i, c in enumerate(cov):
        if not isinstance(c, dict) or not isinstance(c.get("path"), str) or not c["path"]:
            errs.append(f"coverage[{i}] must be an object with a non-empty string path")
            continue
        p, st = c["path"], c.get("status")
        if p in seen:
            errs.append(f"coverage: {p} given twice")
            continue
        seen.add(p)
        if p not in known:
            errs.append(f"coverage: {p} is not a file of this diff (use the new path of a renamed file)")
            continue
        if st not in STATUSES:
            errs.append(f"coverage[{p}].status must be one of {STATUSES}")
            continue
        reason = c.get("reason")
        if reason is not None and not isinstance(reason, str):
            errs.append(f"coverage[{p}].reason must be a string")
            reason = ""
        if st in GAPS:
            if not (reason or "").strip():
                errs.append(f"coverage[{p}]: status {st} needs a reason")
            gaps.append({"path": p, "status": st, "reason": (reason or "").strip()[:200]})
        if st == "reused":
            e = _reuse_error(p, prev, known[p])
            if e:
                errs.append(e)
    for p in sorted(need - seen):
        errs.append(f"coverage: {p} missing (every non-excluded file needs a status)")
    return {"errors": errs, "gaps": gaps}


def unverified(review: dict, worktree: str, manifest: dict) -> list:
    sig = served_signal(worktree, manifest)
    cov = review.get("coverage") if isinstance(review, dict) else None
    out = []
    for c in cov if isinstance(cov, list) else []:
        if isinstance(c, dict) and c.get("status") == "reviewed" and isinstance(c.get("path"), str):
            s = sig.get(c["path"])
            if s != "full":
                out.append({"path": c["path"], "served": s or "unknown"})
    return out


def fingerprint(job: dict, review: dict) -> str:
    """'' when the job has no manifest; else manifest + the reviewer's statuses, for the approval hash: a human
    approval is void when the reviewed scope or the statuses change. Independent of the FLAG on purpose: turning
    it on/off never voids a human approval (Codex step1 R8 #3); a rules approval is re-judged anyway —
    approval_status re-runs the rules on the job's pinned mode."""
    if not job.get("coverage_sha"):
        return ""
    cov = review.get("coverage") if isinstance(review, dict) else None
    rows = sorted((_str(c.get("path")), _str(c.get("status")), _str(c.get("reason")).strip())
                  for c in (cov if isinstance(cov, list) else []) if isinstance(c, dict))
    # ensure_ascii: a lone surrogate in an agent-written reason must not crash the hash
    # the job's PINNED mode (not the flag): a re-prepare under another mode re-asks (step1 R10 #1)
    return _sha_bytes(json.dumps([job_mode(job), job["coverage_sha"], rows], ensure_ascii=True).encode("ascii"))


def evaluate(job: dict, review: dict) -> dict:
    """{"mode", "active", "errors", "gaps", "unverified"} — for validate, the rules and the approval preview.
    mode = the job's pinned mode; active = the job has a manifest."""
    m = job_mode(job)
    base = {"mode": m, "active": False, "errors": [], "gaps": [], "unverified": []}
    if m == "off":
        return base
    manifest, err = state(job)
    if err:
        return {**base, "active": True, "errors": [err]}
    from review_rounds import load_prev
    res = check(review, manifest, load_prev(job))
    return _clean({**base, "active": True, **res,
                   "unverified": unverified(review, job.get("worktree") or "", manifest)})


def summary_line(cov: dict) -> str:
    if not cov.get("active"):
        return ""
    if cov["errors"]:
        return f"⚠️ Độ phủ: bảng kê lỗi ({len(cov['errors'])})"
    parts = []
    if cov["gaps"]:
        parts.append(f"{len(cov['gaps'])} file chưa review đủ: " + ", ".join(
            f"{g['path']} ({g['status']})" for g in cov["gaps"][:4]))
    if cov["unverified"]:
        parts.append(f"{len(cov['unverified'])} file không thấy diff đầy đủ đã được cấp")
    return "📋 Độ phủ: " + ("; ".join(parts) if parts else "đủ mọi file")
