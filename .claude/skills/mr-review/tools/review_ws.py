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
import shutil
import subprocess
import sys

import review_common as rc

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
        with rc.file_lock(f"codegraph-seed-{pid}", timeout=900):   # one seed copy/sync at a time per project
            codegraph = cg.prepare(wt, pid, clone_dir=repo.path if not repo.bare else "")
        if codegraph.get("status") == "ready":
            codegraph.update(cg.analyze(wt, base, head, title=mr.get("title") or "",
                                        out=wt + ".codegraph.md"))

    ws = {
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
    with open(ws_json, "w", encoding="utf-8") as f:
        json.dump(ws, f, ensure_ascii=False, indent=2)
    with rc.file_lock("queue"):
        job = rc.load_job(jid)
        job.update(head_sha=head, worktree=wt, ws_path=ws_json, diff_refs=ws["diff_refs"],
                   title=mr.get("title", job.get("title")))
        rc.log_event(job, "prepared", head=head[:8], files=len(files), new_commits=commits["new"])
        rc.save_job(job)
    rc.board_card(job, status="in-progress",
                  note=f"Đang review · {len(files)} file · {commits['new']}/{commits['total']} commit mới · "
                       f"{mr.get('source_branch')} → {mr.get('target_branch')}")
    return {k: ws[k] for k in ("job_id", "worktree", "diff_refs", "files_changed", "lines_added",
                               "lines_removed", "merge", "codegraph")} | {"ws_path": ws_json,
                                                            "commits": {k: v for k, v in commits.items() if k != "list"}}


def check_stale(jid: str) -> dict:
    job = rc.load_job(jid)
    mr = rc.gitlab_mr(job["project_id"], job["iid"])
    now_head = (mr.get("diff_refs") or {}).get("head_sha") or mr.get("sha")
    reviewed = (job.get("diff_refs") or {}).get("head_sha") or job.get("head_sha")
    return {"job_id": jid, "reviewed_head": reviewed, "current_head": now_head,
            "state": mr.get("state"), "stale": now_head != reviewed or mr.get("state") != "opened"}


def diff(jid: str, stat: bool = False, path: str = "", max_lines: int = 800) -> dict:
    job = rc.load_job(jid)
    refs = job.get("diff_refs") or {}
    repo = open_repo(job["project_id"])
    args = ["diff", "-M"] + (["--stat"] if stat else ["-U5"]) + [refs["base_sha"], refs["head_sha"]]
    if path:
        args += ["--", path]
    lines = repo.git(*args).splitlines()
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
        else:
            out = gc(a.days)
    except (ValueError, RuntimeError, TimeoutError, subprocess.TimeoutExpired) as e:
        out = {"error": True, "message": str(e)}
    return rc.pc.emit(out)


if __name__ == "__main__":
    sys.exit(main())
