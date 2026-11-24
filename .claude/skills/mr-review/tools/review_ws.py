#!/usr/bin/env python3
"""Review workspace — an exact, isolated checkout for ONE MR, safe to run in parallel.

Accuracy rules:
  * pinned to the MR's diff_refs (base_sha / start_sha / head_sha), so the diff is
    exactly GitLab's, even if the target branch moved on; inline comments use them too
  * head is fetched from refs/merge-requests/<iid>/head (works for forks too)
  * optional merge-result checkout (GitLab merge ref, else a local trial merge) to
    catch "A is fine alone but breaks B"
  * commits already reviewed in another MR (same sha or same patch-id — e.g. a
    promotion MR dev -> uat -> prod) are flagged, so the review focuses on new work

Repos: a registered clone (projects.json clone_dir) is reused; any other project gets
a bare partial clone in work/cache/. The token is passed per command via GIT_CONFIG_*
env, never written to disk. Fetch/worktree ops are serialised per repo (file lock);
reviews themselves run in parallel.

    python review_ws.py prepare <job_id> [--with-merge]
    python review_ws.py check-stale <job_id>            # exit 0 = still current, 4 = MR has new commits
    python review_ws.py diff <job_id> [--stat] [--path P] [--max-lines 800]
    python review_ws.py cleanup <job_id>
    python review_ws.py gc [--days 3]
"""

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import time

import review_common as rc
import review_coverage

GIT_TIMEOUT = 900


# --------------------------------------------------------------------------- git

def _auth_env(use_token: bool) -> dict:
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    pairs = []
    if use_token:
        tok = rc.config.get("GITLAB_PRIVATE_TOKEN")
        if tok:
            basic = base64.b64encode(f"oauth2:{tok}".encode()).decode()
            pairs.append(("http.extraHeader", f"Authorization: Basic {basic}"))
    if rc.config.get("SSL_VERIFY", "true").lower() in ("false", "0", "no"):
        pairs.append(("http.sslVerify", "false"))
    base = int(env.get("GIT_CONFIG_COUNT", "0") or 0)
    for i, (k, v) in enumerate(pairs):
        env[f"GIT_CONFIG_KEY_{base + i}"] = k
        env[f"GIT_CONFIG_VALUE_{base + i}"] = v
    if pairs:
        env["GIT_CONFIG_COUNT"] = str(base + len(pairs))
    return env


class Repo:
    def __init__(self, path: str, bare: bool, use_token: bool, kind: str):
        self.path, self.bare, self.kind = path, bare, kind
        self.env = _auth_env(use_token)

    def git(self, *args, check=True, cwd=None, timeout=GIT_TIMEOUT) -> str:
        base = ["git"] + (["--git-dir", self.path] if self.bare and not cwd else ["-C", cwd or self.path])
        p = subprocess.run(base + list(args), capture_output=True, text=True, encoding="utf-8",
                           errors="replace", env=self.env, timeout=timeout, stdin=subprocess.DEVNULL)
        if check and p.returncode != 0:
            raise RuntimeError(f"git {args[0]} failed: {(p.stderr or p.stdout).strip()[-400:]}")
        return p.stdout

    def git_exact(self, *args, timeout=GIT_TIMEOUT) -> str:
        """Like git() but bytes in, no newline translation: a "\r" inside a NUL-separated file name survives."""
        base = ["git"] + (["--git-dir", self.path] if self.bare else ["-C", self.path])
        p = subprocess.run(base + list(args), capture_output=True, env=self.env, timeout=timeout,
                           stdin=subprocess.DEVNULL)
        if p.returncode != 0:
            raise RuntimeError(f"git {args[0]} failed: {p.stderr.decode('utf-8', 'replace').strip()[-400:]}")
        return p.stdout.decode("utf-8", "surrogateescape")

    def has(self, sha: str) -> bool:
        return bool(sha) and subprocess.run(
            ["git"] + (["--git-dir", self.path] if self.bare else ["-C", self.path]) +
            ["cat-file", "-e", f"{sha}^{{commit}}"], env=self.env, capture_output=True).returncode == 0


def open_repo(project_id: str) -> Repo:
    name, reg = rc.registry_by_gitlab_id(project_id)
    clone = reg.get("clone_dir")
    if clone and os.path.isdir(os.path.join(clone, ".git")):
        return Repo(clone, bare=False, use_token=False, kind=f"registry:{name}")
    info = rc.gitlab_project_info(project_id)
    path = rc.cache_dir(info["path_with_namespace"] + ".git")
    repo = Repo(path, bare=True, use_token=True, kind="cache")
    if not os.path.isdir(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        filt = rc.config.get("REVIEW_CLONE_FILTER", "blob:none")
        args = ["git", "clone", "--bare", "--quiet"] + ([f"--filter={filt}"] if filt else []) + \
               [info["http_url_to_repo"], path]
        p = subprocess.run(args, capture_output=True, text=True, env=repo.env, timeout=GIT_TIMEOUT)
        if p.returncode != 0:
            shutil.rmtree(path, ignore_errors=True)
            raise RuntimeError(f"clone failed: {p.stderr.strip()[-400:]}")
    _register_in_orca(path)
    return repo


def _register_in_orca(path: str) -> None:
    """Add a cache repo to Orca once, so its review worktrees get board cards (best effort)."""
    marker = os.path.join(path, "toolkit-orca-repo.json")
    if os.path.isfile(marker) or rc.config.get("REVIEW_ORCA_REGISTER_CACHE", "1").lower() in ("0", "false", "no"):
        return
    o = rc.orca()
    if not o:
        return
    try:
        repo = o.call("repo", "add", "--path", path).get("repo", {})
        with open(marker, "w", encoding="utf-8") as f:
            json.dump({"orca_repo_id": repo.get("id"), "at": rc.now()}, f)
    except Exception:  # noqa: BLE001 — retried on the next prepare
        pass


# --------------------------------------------------------------------------- ledger

def _ledger_path(project_id) -> str:
    return rc.reviews_dir("ledger", f"{project_id}.jsonl")


def ledger_load(project_id) -> tuple:
    shas, pids = {}, {}
    try:
        with open(_ledger_path(project_id), encoding="utf-8") as f:
            for ln in f:
                try:
                    r = json.loads(ln)
                except ValueError:
                    continue
                shas[r["sha"]] = r.get("iid")
                if r.get("patch_id"):
                    pids[r["patch_id"]] = r.get("iid")
    except OSError:
        pass
    return shas, pids


def ledger_add(project_id, iid, commits: list) -> int:
    os.makedirs(os.path.dirname(_ledger_path(project_id)), exist_ok=True)
    with rc.file_lock(f"ledger-{project_id}"):
        shas, _ = ledger_load(project_id)
        new = [c for c in commits if c["sha"] not in shas]
        with open(_ledger_path(project_id), "a", encoding="utf-8") as f:
            for c in new:
                f.write(json.dumps({"sha": c["sha"], "patch_id": c.get("patch_id"), "iid": iid,
                                    "at": rc.now()}) + "\n")
    return len(new)


# --------------------------------------------------------------------------- facts

def _commits(repo: Repo, start: str, head: str, project_id) -> dict:
    log = repo.git("log", "--format=%H%x1f%P%x1f%an%x1f%s", f"{start}..{head}")
    patch_ids = {}
    raw = repo.git("log", "--no-merges", "--format=%H", "-p", f"{start}..{head}")
    if raw.strip():
        p = subprocess.run(["git", "patch-id", "--stable"], input=raw, capture_output=True,
                           text=True, encoding="utf-8", errors="replace")
        for ln in p.stdout.splitlines():
            pid, sha = (ln.split() + ["", ""])[:2]
            patch_ids[sha] = pid
    seen_sha, seen_pid = ledger_load(project_id)
    out = []
    for ln in log.splitlines():
        sha, parents, author, subject = (ln.split("\x1f") + [""] * 4)[:4]
        pid = patch_ids.get(sha)
        before = seen_sha.get(sha) or (seen_pid.get(pid) if pid else None)
        out.append({"sha": sha, "subject": subject[:120], "author": author,
                    "merge": len(parents.split()) > 1, "patch_id": pid, "reviewed_in": before})
    new = [c for c in out if not c["reviewed_in"]]
    return {"total": len(out), "new": len(new), "already_reviewed": len(out) - len(new),
            "merges": sum(1 for c in out if c["merge"]), "list": out}


def _numstat(repo: Repo, base: str, head: str) -> list:
    files = []
    for ln in repo.git("diff", "--numstat", "-M", base, head).splitlines():
        parts = ln.split("\t")
        if len(parts) == 3:
            add, rem, path = parts
            files.append({"path": path, "added": None if add == "-" else int(add),
                          "removed": None if rem == "-" else int(rem)})
    return files


def _ws_paths(job: dict) -> tuple:
    head8 = (job.get("head_sha") or "")[:8]
    wt = rc.reviews_dir(str(job["project_id"]), f"{job['iid']}@{head8}")
    return wt, wt + ".ws.json", wt + ".merge"


class AlreadyReviewed(RuntimeError):
    """prepare refused: the head being prepared already has a POSTED review (review_rounds)."""


def _seen(job: dict) -> str:
    return json.dumps([job.get(k) for k in ("posted_rounds", "previous", "diff_refs", "status", "coverage_sha",
                                            "coverage_mode")],
                      sort_keys=True,
                      default=str)


def _save_ledger(jid: str, gen, seen: str, previous: dict, ledger: list) -> None:
    """Record rounds found posted (lost responses) on the job — only if it is still the job prepare read."""
    with rc.file_lock("queue"):
        job = rc.load_job(jid)
        if job.get("gen") != gen or _seen(job) != seen:
            raise RuntimeError(f"job {jid} changed during prepare — retry")
        job.update(posted_rounds=ledger, previous=previous or job.get("previous"))
        rc.log_event(job, "ledger_recorded", rounds=len(ledger))
        rc.save_job(job)


def _known_previous(job: dict, pid, iid, head: str) -> tuple:
    """(previous, ledger): the round this prepare compares against and every posted round of the MR — both
    normalised and SAVED on the job (the tamper snapshot and the next round read the same paths — R2 #3):
      * the job's own last round when its head moved after a POST whose response was lost (marker on the MR):
        the member saw it, whatever the job status says (R2 #2 — the coordinator re-prepares before post runs);
        it is archived + recorded like any posted round
      * a job queued before rounds existed: its posted review sits at the per-head path (R1 #3)"""
    import review_rounds
    import review_rules as rr
    previous = dict(job.get("previous") or {})
    ledger = list(job.get("posted_rounds") or [])
    if previous.get("status") == "posted" and not review_rounds.posted_at_head(ledger, previous.get("head_sha")):
        ledger.insert(0, previous)                         # a job from before the ledger
    old_head = (job.get("diff_refs") or {}).get("head_sha")
    old_wt = job.get("worktree") or ""
    # only a draft that was APPROVED can have reached GitLab: no approval = no lookup (no GitLab call per re-prepare)
    may_be_posted = bool(job.get("approval")) or job.get("status") in ("approved", "posted")
    if (may_be_posted and old_head and old_head != head and old_wt and previous.get("head_sha") != old_head
            and os.path.exists(old_wt + ".review.json")):
        try:
            with rc.gitlab_project(pid) as gl:
                note = rr.posted_note(gl, {**job, "diff_refs": {"head_sha": old_head}})
        except Exception as e:  # noqa: BLE001
            # unknown is NOT "not posted": moving the job on would lose the round for good — retry later (R3 #2)
            raise RuntimeError(f"could not check whether the review of {old_head[:8]} was posted "
                               f"({type(e).__name__}: {str(e)[:150]}) — retry later") from e
        if note:
            try:
                with open(old_wt + ".review.json", encoding="utf-8") as f:
                    verdict = (json.load(f) or {}).get("verdict") or ""
            except (OSError, ValueError, AttributeError):
                verdict = ""
            sent = None                             # what that try sent (review_post.posting_path)
            try:
                with open(old_wt + ".posting.json", encoding="utf-8") as f:
                    sent = json.load(f)
            except (OSError, ValueError):
                pass
            if not (isinstance(sent, dict) and sent.get("head_sha") == old_head):
                sent = None
            previous, _ = review_rounds.adopt_posted({**job, "head_sha": old_head, "worktree": old_wt,
                                                      "diff_refs": {"head_sha": old_head},
                                                      "review_file": old_wt + ".review.json"}, note,
                                                     (sent or {}).get("verdict") or verdict, review=sent)
            ledger = review_rounds.with_entry(ledger, previous)
            rc.log_event(job, "posted_reconciled", before_prepare=True, head=old_head[:8])
    for e in [previous, *ledger]:
        h = e.get("head_sha")
        if e.get("status") == "posted" and h and not e.get("review_file"):
            legacy = rc.reviews_dir(str(pid), f"{iid}@{h[:8]}")
            e.update(worktree=legacy, review_file=legacy + ".review.json")
    return previous, ledger


def _write_prev(job: dict, repo, wt: str, pid, iid, head: str) -> dict:
    """<wt>.prev.json for a re-review (review_rounds): what the previous POSTED round asked, the member's
    replies since, and the lines changed since. None on a first round. Never fails the prepare."""
    import review_rounds
    import review_rules as rr
    path = review_rounds.prev_path(wt)

    def git(*args):
        try:
            return repo.git(*args)
        except (RuntimeError, OSError, subprocess.SubprocessError):
            return None

    def notes():
        with rc.gitlab_project(pid) as gl:
            return list(rr.all_pages(gl, f"/merge_requests/{iid}/notes?sort=asc&order_by=created_at", "notes"))

    previous = dict(job.get("previous") or {})              # normalised by _known_previous
    prev_head = previous.get("head_sha")
    if prev_head and not repo.has(prev_head):
        with rc.file_lock(f"repo-{pid}", timeout=900):
            git("fetch", "--quiet", "origin", prev_head)
    try:
        data = review_rounds.build({**job, "previous": previous, "worktree": wt}, git, notes, rr.MARKER_RE,
                                   head=head)
    except Exception as e:  # noqa: BLE001 — a broken previous round must not block this review
        data = None
        rc.log_event(job, "prev_round_error", error=f"{type(e).__name__}: {e}"[:200])
    if data is None:
        # nothing to remove: a prev.json left at this path is ignored unless THIS prepare records its hash
        return None
    # written aside, published by prepare under the queue lock only once the job is verified unchanged (R5 #1)
    tmp = f"{path}.tmp-{job.get('gen') or 'x'}-{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return {"round": data["round"], "prev_json": path, "tmp": tmp, "sha": review_rounds.file_sha(tmp),
            "open": len(data["open"]), "replies": len(data["replies"]), "delta": data["delta"] is not None}


# --------------------------------------------------------------------------- commands

def prepare(jid: str, with_merge: bool = False) -> dict:
    job = rc.load_job(jid)
    if not job:
        raise ValueError(f"no job {jid}")
    pid, iid = job["project_id"], job["iid"]
    mr = rc.gitlab_mr(pid, iid)
    if mr.get("state") != "opened":
        raise ValueError(f"MR !{iid} is {mr.get('state')}")
    refs = mr.get("diff_refs") or {}
    base, start, head = refs.get("base_sha"), refs.get("start_sha"), refs.get("head_sha") or mr.get("sha")
    if not (base and start and head):
        raise RuntimeError("MR has no diff_refs yet (GitLab still computing the diff?) — retry later")
    import review_rounds
    gen = job.get("gen")
    # what this prepare read: if any of it changes before the locked save (a sweeper reconciled / requeued the job),
    # the prepare is stale and retries — never saves an older ledger over a newer one (R6 #2)
    seen = _seen(job)
    known, ledger = _known_previous(job, pid, iid, head)    # before the job's head / worktree move on
    if review_rounds.posted_at_head(ledger, head):
        # the MR is back at a commit whose review was already posted — by ANY earlier round (force-push): that
        # round must stay as the member saw it — never draft over its files / marker (Codex rounds R4 #2, R5 #3).
        # A round found posted just now (lost response) is recorded first, or it would be lost (R6 #1).
        if ledger != (job.get("posted_rounds") or []):
            _save_ledger(jid, gen, seen, known, ledger)
        raise AlreadyReviewed(f"MR !{iid} is at {head[:8]}, already reviewed (posted) — nothing to do")
    round_now = review_rounds.round_of(known) if (known or {}).get("status") == "posted" else int(job.get("round") or 1)
    job["previous"], job["round"] = known or None, round_now
    job["head_sha"] = head
    wt, ws_json, wt_merge = _ws_paths(job)

    repo = open_repo(pid)
    with rc.file_lock(f"repo-{pid}", timeout=900):
        target = mr["target_branch"]
        repo.git("fetch", "--quiet", "origin",
                 f"+refs/merge-requests/{iid}/head:refs/toolkit/mr/{iid}/head",
                 f"+refs/heads/{target}:refs/toolkit/target/{target}")
        merge_ref = None
        if repo.git("fetch", "--quiet", "origin", f"+refs/merge-requests/{iid}/merge:refs/toolkit/mr/{iid}/merge",
                    check=False) is not None:
            merge_ref = repo.git("rev-parse", "--verify", "-q", f"refs/toolkit/mr/{iid}/merge", check=False).strip()
        for sha in (base, start, head):
            if not repo.has(sha):
                repo.git("fetch", "--quiet", "origin", sha, check=False)
            if not repo.has(sha):
                raise RuntimeError(f"commit {sha[:10]} not reachable after fetch (force-pushed?)")
        actual = repo.git("rev-parse", f"refs/toolkit/mr/{iid}/head").strip()
        if actual != head:
            raise RuntimeError(f"MR head moved during prepare ({head[:8]} -> {actual[:8]}); requeue")

        if os.path.isdir(wt) and repo.git("rev-parse", "HEAD", cwd=wt, check=False).strip() != head:
            repo.git("worktree", "remove", "--force", wt, check=False)
            shutil.rmtree(wt, ignore_errors=True)
        if not os.path.isdir(wt):
            os.makedirs(os.path.dirname(wt), exist_ok=True)
            repo.git("worktree", "prune", check=False)
            repo.git("worktree", "add", "--detach", "--force", wt, head)
        files = _numstat(repo, base, head)          # also pre-fetches both sides' blobs (partial clone)
        repo.git("diff", "--quiet", base, head, check=False)
        commits = _commits(repo, start, head, pid)
        coverage = None                             # review_coverage: the files the review must account for
        if review_coverage.mode() != "off":
            manifest = review_coverage.build(repo.git, base, head, start, exact=repo.git_exact)
            cov_tmp, cov_sha = review_coverage.write_tmp(wt, manifest, str(gen or "x"))
            coverage = {"tmp": cov_tmp, "sha": cov_sha, "path": review_coverage.path_of(wt),
                        "mode": review_coverage.mode(),
                        "files": len(manifest["files"]),
                        "excluded": sum(1 for f in manifest["files"] if f.get("excluded"))}

        merge = {"gitlab_merge_status": mr.get("detailed_merge_status") or mr.get("merge_status"),
                 "has_conflicts": mr.get("has_conflicts"), "merge_ref": merge_ref or None,
                 "merge_ref_current": False, "worktree": None, "conflicts": []}
        if merge_ref:
            parents = repo.git("rev-list", "--parents", "-n", "1", merge_ref).split()[1:]
            merge["merge_ref_current"] = head in parents
        if with_merge:
            if os.path.isdir(wt_merge):
                repo.git("worktree", "remove", "--force", wt_merge, check=False)
            if merge["merge_ref_current"]:
                repo.git("worktree", "add", "--detach", "--force", wt_merge, merge_ref)
            else:   # local trial merge onto the CURRENT target tip
                tip = repo.git("rev-parse", f"refs/toolkit/target/{target}").strip()
                repo.git("worktree", "add", "--detach", "--force", wt_merge, tip)
                repo.git("-c", "user.name=review-bot", "-c", "user.email=review-bot@localhost",
                         "merge", "--no-ff", "--no-commit", head, cwd=wt_merge, check=False)
                merge["conflicts"] = repo.git("diff", "--name-only", "--diff-filter=U",
                                              cwd=wt_merge, check=False).split()
            merge["worktree"] = wt_merge

    codegraph = {"status": "disabled"}
    if rc.config.get("REVIEW_CODEGRAPH", "1").lower() not in ("0", "false", "no"):
        import cg                                            # dev-automation/tools (on sys.path)
        tb = mr.get("target_branch") or ""
        # one seed per (project, target branch); one copy/sync at a time per seed
        with rc.file_lock("codegraph-seed-" + re.sub(r"[^A-Za-z0-9_.-]", "_", f"{pid}-{tb}"), timeout=900):
            codegraph = cg.prepare(wt, pid, clone_dir=repo.path if not repo.bare else "", branch=tb)
        if codegraph.get("status") == "ready":
            codegraph.update(cg.analyze(wt, base, head, title=mr.get("title") or "",
                                        out=wt + ".codegraph.md"))

    previous_round = _write_prev(job, repo, wt, pid, iid, head)
    prev_tmp = (previous_round or {}).pop("tmp", None)

    ws = {
        "previous_round": previous_round,
        "job_id": jid, "project_id": pid, "project_path": job.get("project_path"), "iid": iid,
        "title": mr.get("title"), "web_url": mr.get("web_url"),
        "author": (mr.get("author") or {}).get("username"),
        "source_branch": mr.get("source_branch"), "target_branch": mr.get("target_branch"),
        "diff_refs": {"base_sha": base, "start_sha": start, "head_sha": head},
        "worktree": wt, "repo": {"kind": repo.kind, "path": repo.path},
        "files": files, "files_changed": len(files),
        "lines_added": sum(f["added"] or 0 for f in files),
        "lines_removed": sum(f["removed"] or 0 for f in files),
        "commits": commits, "merge": merge,
        "description": (mr.get("description") or "")[:4000],
        "prepared_at": rc.now(),
        "how_to_diff": f"python {os.path.join(rc.tools_dir(), 'review_ws.py')} diff {jid} [--path P]",
        "codegraph": {k: v for k, v in codegraph.items() if k != "external_callers"},
    }
    if coverage:                                    # flag off: ws.json exactly as before
        ws["coverage"] = {k: v for k, v in coverage.items() if k != "tmp"}
    # written aside and published under the queue lock with the rest: a prepare that loses the race must not
    # leave its ws.json next to the winner's job (Codex ocr-eval step1 R2 #5)
    ws_tmp = f"{ws_json}.tmp-{gen or 'x'}-{os.getpid()}"
    with open(ws_tmp, "w", encoding="utf-8") as f:
        json.dump(ws, f, ensure_ascii=False, indent=2)
    # post → approval → queue (post holds post then queue; request_approval holds approval then saves): a post or
    # an approval request in flight finishes — and saves its job — before this prepare publishes a new scope /
    # mode, which then sees the change and refuses; never the other way round (step1 R5 #1, R11 #1)
    with rc.file_lock(f"post-{jid}", timeout=900, stale_after=1800), \
            rc.file_lock(f"approval-{jid}", timeout=120), rc.file_lock("queue"):
        job = rc.load_job(jid)
        why = (f"job {jid} was replaced by a newer request during prepare" if job.get("gen") != gen else
               f"job {jid} changed during prepare (reconciled / requeued)" if _seen(job) != seen else
               # ended while we prepared (e.g. a sweeper posted the OLD head meanwhile): never stamp the new head on
               # a posted / cancelled job — that would turn the next real request into a "same head" no-op (R3 #1)
               f"job {jid} became {job.get('status')} during prepare" if job.get("status") in rc.TERMINAL else "")
        cov_tmp = (coverage or {}).get("tmp")
        draft = wt + ".review.json"
        # new scope = the manifest on disk OR the one pinned on the job differs: an off-prepare in between drops
        # the job's pin but leaves the old file, so the file alone can look "unchanged" (step1 R6 #2)
        new_scope = bool(coverage) and (review_coverage.file_sha(review_coverage.path_of(wt)) != coverage["sha"]
                                        or job.get("coverage_sha") != coverage["sha"])
        if not why and new_scope and (os.path.exists(wt + ".posting.json")
                                      or (job.get("inline_done") or {}).get(head)
                                      or (job.get("inline_uncertain") or {}).get(head)):
            # a post attempt of the OLD scope at this head may be on GitLab (its markers are per head): switching
            # scope now would let a retry take those markers for the new review. A human resolves it first
            # (Codex ocr-eval step1 R4 #1 #3) — rare: same head + new base/start + a post attempt that never ended.
            why = (f"MR !{iid} has an unfinished post attempt at {head[:8]} and its diff scope changed — check "
                   f"the MR, then remove {os.path.basename(wt)}.posting.json / the job's inline state to re-review")
        if why:
            for tmp in (prev_tmp, cov_tmp, ws_tmp):
                if tmp:
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
            raise RuntimeError(why + " — not overwriting it")
        os.replace(ws_tmp, ws_json)
        if new_scope and os.path.exists(draft):
            # the draft at this per-head path was written for ANOTHER manifest (other base/start, an earlier
            # visit of this head, or before the flag): never reuse it (step_review would) (R2 #3, R3 #1).
            # A POSTED round never reaches here (AlreadyReviewed above), and an unfinished post is refused above.
            os.replace(draft, f"{draft}.old-scope-{int(time.time())}")
            rc.log_event(job, "draft_set_aside", reason="coverage manifest changed at this head")
        if prev_tmp:
            os.replace(prev_tmp, review_rounds.prev_path(wt))      # published only for the verified job (R5 #1)
        if cov_tmp:
            os.replace(cov_tmp, review_coverage.path_of(wt))
            try:                                    # a new manifest = a new served log (old one = other round)
                os.remove(review_coverage.served_path(wt))
            except OSError:
                pass
        job.update(head_sha=head, worktree=wt, ws_path=ws_json, diff_refs=ws["diff_refs"],
                   title=mr.get("title", job.get("title")),
                   previous=known or None, round=round_now, posted_rounds=ledger,
                   # the validator refuses a prev.json that no longer matches this (review_rounds.prev_state)
                   prev_json_sha=(previous_round or {}).get("sha") or None)
        if coverage:          # same for the coverage manifest (review_coverage.state) + the mode it runs under
            job.update(coverage_sha=coverage["sha"], coverage_mode=coverage["mode"])
        else:                 # flag off: no key at all (job exactly as before)
            job.pop("coverage_sha", None)
            job.pop("coverage_mode", None)
        rc.log_event(job, "prepared", head=head[:8], files=len(files), new_commits=commits["new"])
        rc.save_job(job)
    rc.board_card(job, status="in-progress",
                  note=f"Đang review · {len(files)} file · {commits['new']}/{commits['total']} commit mới · "
                       f"{mr.get('source_branch')} → {mr.get('target_branch')}")
    return {k: ws[k] for k in ("job_id", "worktree", "diff_refs", "files_changed", "lines_added",
                               "lines_removed", "merge", "codegraph", "previous_round")} | {"ws_path": ws_json,
                                                            "coverage": ws.get("coverage"),
                                                            "commits": {k: v for k, v in commits.items() if k != "list"}}


def check_stale(jid: str, job=None) -> dict:
    """job = the snapshot the caller acts on (post): compare the MR with THAT, not with a re-read that a
    concurrent prepare may already have moved (Codex ocr-eval step1 R2 #1)."""
    job = job or rc.load_job(jid)
    mr = rc.gitlab_mr(job["project_id"], job["iid"])
    now_head = (mr.get("diff_refs") or {}).get("head_sha") or mr.get("sha")
    reviewed = (job.get("diff_refs") or {}).get("head_sha") or job.get("head_sha")
    stale = now_head != reviewed or mr.get("state") != "opened"
    if job.get("coverage_sha") and not stale:
        # the coverage manifest is pinned to base/start too: same head, other scope = a review of old refs
        now, was = mr.get("diff_refs") or {}, job.get("diff_refs") or {}
        stale = any(now.get(k) != was.get(k) for k in ("base_sha", "start_sha"))
    return {"job_id": jid, "reviewed_head": reviewed, "current_head": now_head,
            "state": mr.get("state"), "stale": stale}


def diff(jid: str, stat: bool = False, path: str = "", max_lines: int = 800) -> dict:
    job = rc.load_job(jid)
    refs = job.get("diff_refs") or {}
    repo = open_repo(job["project_id"])
    # unquoted paths = the manifest's spelling; flag off (now) = output as before
    cov = bool(job.get("coverage_sha"))
    args = (["-c", "core.quotePath=false"] if cov else []) + ["diff", "-M"] + \
        (["--stat"] if stat else ["-U5"]) + [refs["base_sha"], refs["head_sha"]]
    if path:
        old = None
        if cov:                                   # a renamed file: both sides, or git shows it as an add
            manifest, _ = review_coverage.state(job)
            old = next((f.get("old_path") for f in (manifest or {}).get("files", [])
                        if f.get("path") == path and f.get("old_path")), None)
        args += ["--", old, path] if old else ["--", path]
    lines = repo.git(*args).splitlines()
    if not stat and cov:
        review_coverage.record_served(job.get("worktree") or "", path, lines, max_lines)
    return {"job_id": jid, "path": path or None, "lines": len(lines),
            "truncated": len(lines) > max_lines, "diff": "\n".join(lines[:max_lines])}


def cleanup(jid: str) -> dict:
    job = rc.load_job(jid)
    removed = []
    if not job.get("worktree"):
        return {"job_id": jid, "removed": removed}
    repo = open_repo(job["project_id"])
    wt = job["worktree"]
    with rc.file_lock(f"repo-{job['project_id']}", timeout=300):
        for p in (wt + ".merge", wt):
            if p == wt:
                try:
                    os.remove(wt + ".codegraph.md")
                except OSError:
                    pass
            if os.path.isdir(p):
                repo.git("worktree", "remove", "--force", p, check=False)
                shutil.rmtree(p, ignore_errors=True)
                removed.append(p)
        repo.git("worktree", "prune", check=False)
        for ref in (f"refs/toolkit/mr/{job['iid']}/head", f"refs/toolkit/mr/{job['iid']}/merge"):
            repo.git("update-ref", "-d", ref, check=False)
    return {"job_id": jid, "removed": removed}


_WT_NAME = re.compile(r"^\d+@[0-9a-f]{6,40}(\.merge)?$")
PRUNE_MIN_AGE_S = 600     # never touch a worktree this young: a prepare may not have recorded it on its job yet


def keep_worktrees() -> bool:
    """REVIEW_KEEP_WORKTREES=1: keep each review's worktree (an Orca card per review) until `gc`."""
    return (rc.config.get("REVIEW_KEEP_WORKTREES", "0") or "0").strip().lower() in ("1", "true", "yes", "on")


def prune(dry_run: bool = False) -> dict:
    """Remove every review worktree (work/reviews/<project>/<iid>@<sha>[.merge]) that no OPEN job uses:
    reviews already posted / cancelled / rejected and worktrees of superseded heads. review.json / ws.json
    stay (re-sending a notice and the history need them); a worktree younger than PRUNE_MIN_AGE_S stays."""
    active = set()
    for j in rc.all_jobs():
        if j.get("status") not in rc.TERMINAL and j.get("worktree"):
            for p in (j["worktree"], j["worktree"] + ".merge"):
                active.add(os.path.normcase(os.path.abspath(p)))
    root, now, removed = rc.reviews_dir(), time.time(), []
    if not os.path.isdir(root):
        return {"removed": removed}
    for pid in sorted(os.listdir(root)):
        pdir = os.path.join(root, pid)
        if not (pid.isdigit() and os.path.isdir(pdir)):
            continue
        cands = []
        for name in sorted(os.listdir(pdir)):
            p = os.path.join(pdir, name)
            if (_WT_NAME.match(name) and os.path.isdir(p) and os.path.normcase(os.path.abspath(p)) not in active
                    and now - os.path.getmtime(p) > PRUNE_MIN_AGE_S):
                cands.append(p)
        if not cands:
            continue
        if dry_run:
            removed += cands
            continue
        try:
            repo = open_repo(pid)
        except (RuntimeError, ValueError, OSError):
            repo = None                                   # project gone from the registry: plain delete
        with rc.file_lock(f"repo-{pid}", timeout=300):
            for p in cands:
                if repo is not None:
                    repo.git("worktree", "remove", "--force", p, check=False)
                shutil.rmtree(p, ignore_errors=True)
                if not p.endswith(".merge"):
                    try:
                        os.remove(p + ".codegraph.md")
                    except OSError:
                        pass
                removed.append(p)
            if repo is not None:
                repo.git("worktree", "prune", check=False)
    return {"removed": removed}


def gc(days: int = 3) -> dict:
    done = []
    for j in rc.all_jobs():
        if j.get("status") in rc.TERMINAL and rc.now() - j.get("updated", 0) > days * 86400 and j.get("worktree"):
            try:
                done.append(cleanup(j["id"]))
            except (RuntimeError, ValueError) as e:
                done.append({"job_id": j["id"], "error": str(e)})
    return {"cleaned": done}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="review_ws.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("job_id")
    p.add_argument("--with-merge", action="store_true")
    p = sub.add_parser("check-stale")
    p.add_argument("job_id")
    p = sub.add_parser("diff")
    p.add_argument("job_id")
    p.add_argument("--stat", action="store_true")
    p.add_argument("--path", default="")
    p.add_argument("--max-lines", type=int, default=800)
    p = sub.add_parser("cleanup")
    p.add_argument("job_id")
    p = sub.add_parser("gc")
    p.add_argument("--days", type=int, default=3)
    p = sub.add_parser("prune")                  # remove every worktree no open job uses
    p.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "prepare":
            out = prepare(a.job_id, a.with_merge)
        elif a.cmd == "check-stale":
            out = check_stale(a.job_id)
            rc.pc.emit(out)
            return 4 if out["stale"] else 0
        elif a.cmd == "diff":
            out = diff(a.job_id, a.stat, a.path, a.max_lines)
        elif a.cmd == "cleanup":
            out = cleanup(a.job_id)
        elif a.cmd == "prune":
            out = prune(a.dry_run)
        else:
            out = gc(a.days)
    except (ValueError, RuntimeError, TimeoutError, subprocess.TimeoutExpired) as e:
        out = {"error": True, "message": str(e)}
    return rc.pc.emit(out)


if __name__ == "__main__":
    sys.exit(main())
