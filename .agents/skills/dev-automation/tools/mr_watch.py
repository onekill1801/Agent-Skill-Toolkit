#!/usr/bin/env python3
"""Watch GitLab for merge requests assigned to you and auto-launch a review.

Polls (default every 5 min) the global /merge_requests endpoint for OPENED MRs
where you are the reviewer (or assignee). For each MR not yet reviewed at its
current head SHA, it pulls the MR's latest code into an ISOLATED git worktree off
the registered clone (work/projects.json — without touching your current branch),
then opens ONE terminal running `claude` (Pro subscription, no API key) IN that
worktree, seeded to review the MR against the real code via the dev-automation
skill (/review-mr) and to ASK YOU before posting any comment ([WRITE] guardrail).
The worktree is removed when the review terminal closes. If the project has no
registered clone, it falls back to an API-diff-only review.

Re-reviews automatically when new commits change the MR's SHA.

First run establishes a BASELINE (marks current open MRs as seen, does NOT review
the backlog) so it doesn't open dozens of terminals at once — afterwards it only
reviews MRs that are NEW or get new commits. Use --review-existing to also work
through the backlog (throttled by --max-per-cycle).

Usage:
  python mr_watch.py                       # reviewer, 5-min poll, baseline first
  python mr_watch.py --who both            # reviewer + assignee
  python mr_watch.py --interval 120        # poll every 2 min
  python mr_watch.py --max-per-cycle 5     # up to 5 review terminals per poll (default 3)
  python mr_watch.py --review-existing     # also review the current backlog
  python mr_watch.py --once                # one pass, then exit
  python mr_watch.py --no-spawn            # just print matches (no terminal, no state write)
  python mr_watch.py --include-drafts      # also review Draft/WIP MRs
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time

import config
import daemon_common
import gitlab_api

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(TOOLS_DIR, "..", "..", "..", ".."))
INBOX = os.path.join(REPO_ROOT, "temp", "mr_incoming")
STATE = os.path.join(REPO_ROOT, "temp", "mr_reviewed.json")
WT_ROOT = os.path.join(REPO_ROOT, "temp", "mr_worktrees")
REVIEWS_DIR = os.path.join(REPO_ROOT, "temp", "mr_reviews")

_FT_DIR = os.path.abspath(os.path.join(TOOLS_DIR, "..", "..", "fork-terminal", "tools"))
sys.path.insert(0, _FT_DIR)
try:
    import fork_terminal
except Exception:
    fork_terminal = None


def _now():
    return time.strftime("%H:%M:%S")


def _load_state() -> dict:
    if os.path.isfile(STATE):
        try:
            with open(STATE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def _save_state(d: dict):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2)


def _work_dir():
    return config.get("WORK_DIR") or os.path.join(REPO_ROOT, "work")


def _load_registry() -> dict:
    try:
        with open(os.path.join(_work_dir(), "projects.json"), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _find_clone(project_id):
    """Return (name, clone_dir) of the registered project matching this gitlab id."""
    for name, p in _load_registry().items():
        if isinstance(p, dict) and str(p.get("gitlab_project_id")) == str(project_id):
            cd = p.get("clone_dir")
            if cd and os.path.isdir(os.path.join(cd, ".git")):
                return name, cd
    return None, None


def _git(clone, *args, timeout=300):
    return subprocess.run(["git", "-C", clone, *args], capture_output=True,
                          text=True, encoding="utf-8", timeout=timeout)


def _prepare_worktree(clone, pid, iid, sha, target_branch):
    """Fetch the MR head into `clone` and create an ISOLATED detached worktree at
    the MR's latest commit — without touching the clone's current branch.
    Returns the worktree path, or None on failure (caller falls back to API diff)."""
    os.makedirs(WT_ROOT, exist_ok=True)
    wt = os.path.normpath(os.path.join(WT_ROOT, f"{pid}_{iid}"))
    if os.path.exists(wt):                      # clean a leftover from a previous run
        _git(clone, "worktree", "remove", "--force", wt)
        shutil.rmtree(wt, ignore_errors=True)
    # fetch the MR head (brings the sha's objects in); target branch for a local diff base
    if _git(clone, "fetch", "-q", "origin", f"merge-requests/{iid}/head").returncode != 0:
        return None
    if target_branch:
        _git(clone, "fetch", "-q", "origin", target_branch, timeout=120)
    # check out the EXACT MR commit by sha (not FETCH_HEAD — the target fetch overwrote it)
    ref = sha or "FETCH_HEAD"
    if _git(clone, "worktree", "add", "--detach", "-f", wt, ref).returncode != 0:
        return None
    return wt


def _prune_worktrees():
    for p in _load_registry().values():
        cd = p.get("clone_dir") if isinstance(p, dict) else None
        if cd and os.path.isdir(os.path.join(cd, ".git")):
            try:
                _git(cd, "worktree", "prune", timeout=30)
            except Exception:
                pass


def _write_context(mr, worktree, clone) -> str:
    os.makedirs(INBOX, exist_ok=True)
    os.makedirs(REVIEWS_DIR, exist_ok=True)
    pid, iid = mr["project_id"], mr["iid"]
    path = os.path.join(INBOX, f"mr_{pid}_{iid}.md")
    tools = os.path.normpath(TOOLS_DIR)
    reviews_dir = os.path.normpath(REVIEWS_DIR)
    if worktree:
        code_block = (
            f"- **Code MR đã được pull về sẵn** (checkout đúng HEAD của MR trong một "
            f"git worktree TÁCH BIỆT — không ảnh hưởng nhánh bạn đang làm ở clone gốc).\n"
            f"- Terminal này đang đứng ở chính worktree đó. Hãy ĐỌC TOÀN BỘ source ở đây để hiểu "
            f"ngữ cảnh: lần theo caller/callee, kiểm tra impact, chạy build/test nếu cần.\n"
            f"- Lấy diff chuẩn của MR: `cd \"{tools}\" && python gitlab_api.py mr-changes {iid}` "
            f"(GITLAB_PROJECT_ID đã set sẵn). Diff local nhanh: `git diff origin/{mr['target_branch']}...HEAD`.\n"
        )
    else:
        code_block = (
            f"- (Project {pid} CHƯA có clone đăng ký trong `work/projects.json` → review theo DIFF API.)\n"
            f"- Lấy diff: `cd \"{tools}\" && python gitlab_api.py mr-changes {iid}` (GITLAB_PROJECT_ID đã set sẵn).\n"
        )
    body = f"""# Review Merge Request !{iid} — {mr['title']}

- Project ID: `{pid}` · Tác giả: {mr['author']} · `{mr['source_branch']}` → `{mr['target_branch']}`
- URL: {mr['web_url']} · SHA: `{mr['sha']}`

## Nguồn để review
{code_block}
Tham khảo thêm: `python gitlab_api.py mr-detail {iid}`, `mr-discussions {iid}`.

## Việc cần làm (TỰ ĐỘNG — không hỏi, xong thì kết thúc)
1. Đọc `.claude/skills/dev-automation/cookbook/review-merge-request.md`
   + `java-standards.md` (nếu code Java/Spring Boot).
2. Đối chiếu diff (`python gitlab_api.py mr-changes {iid}`) với CODE THẬT đã pull:
   tìm bug/logic sai, lỗi bảo mật, hiệu năng, vi phạm convention, ảnh hưởng tới chỗ
   khác (lần theo caller của hàm vừa đổi trong worktree này).
3. Soạn nhận xét review rõ ràng (markdown), chỉ rõ file:line, mức độ (blocker/nên sửa/nit).
4. **LƯU** bản review vào `{reviews_dir}/mr_{pid}_{iid}.md`.
5. **TỰ POST** review lên MR (KHÔNG hỏi — đây là chế độ tự động đã được người dùng bật):
   ```
   cd "{tools}"
   python gitlab_api.py mr-comment {iid} "$(cat '{reviews_dir}/mr_{pid}_{iid}.md')"
   ```
   (Review dài/nhiều dòng → truyền qua `$(cat file)` như trên để tránh lỗi quote.)
6. In tóm tắt ngắn rồi KẾT THÚC (đừng chờ thêm input — terminal sẽ tự đóng).
"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)
    return path


def _spawn_review(mr):
    pid, iid = mr["project_id"], mr["iid"]
    name, clone = _find_clone(pid)
    worktree = _prepare_worktree(clone, pid, iid, mr.get("sha"), mr.get("target_branch")) if clone else None
    if clone and not worktree:
        print(f"[{_now()}]        (không tạo được worktree cho {name} — review theo diff API)")
    ctx = _write_context(mr, worktree, clone)
    prompt = f"Read and follow the review instructions in {ctx}"
    # Headless `claude -p` so it runs the whole review and EXITS when done → the
    # terminal then auto-closes (cmd /c). --dangerously-skip-permissions lets it run
    # git / gitlab_api / read the worktree without permission prompts. Default model
    # 'sonnet' (claude -p defaults to a gated model otherwise); override MR_REVIEW_MODEL.
    model = config.get("MR_REVIEW_MODEL", "sonnet") or "sonnet"
    claude_cmd = f'claude -p --dangerously-skip-permissions --model {model} "{prompt}"'
    log = os.path.normpath(os.path.join(REVIEWS_DIR, f"mr_{pid}_{iid}.log"))
    cwd = worktree or REPO_ROOT
    try:
        if os.name == "nt":
            launcher = os.path.normpath(os.path.join(INBOX, f"launch_{pid}_{iid}.cmd"))
            with open(launcher, "w", encoding="utf-8") as f:
                f.write("@echo off\r\n")
                f.write(f"set GITLAB_PROJECT_ID={pid}\r\n")
                f.write(f'cd /d "{os.path.normpath(cwd)}"\r\n')
                f.write(f"echo Reviewing MR !{iid} (tu dong, se tu dong dong)...\r\n")
                f.write(f'{claude_cmd} > "{log}" 2>&1\r\n')   # output -> log; window auto-closes
                if worktree:                       # cleanup after claude exits (cd out first)
                    f.write(f'cd /d "{os.path.normpath(clone)}"\r\n')
                    f.write(f'git worktree remove --force "{worktree}"\r\n')
            # cmd /c → close the window once the script finishes
            subprocess.Popen(f'start "Review MR !{iid}" cmd /c "{launcher}"', shell=True)
        elif fork_terminal is not None:
            extra = f' ; git -C "{clone}" worktree remove --force "{worktree}"' if worktree else ""
            fork_terminal.fork_terminal(f'cd "{cwd}" && GITLAB_PROJECT_ID={pid} {claude_cmd}{extra}')
        else:
            print(f"[{_now()}] [WARN] no terminal spawner; context at {ctx}", file=sys.stderr)
            return
        where = f"worktree {worktree}" if worktree else "diff API"
        print(f"[{_now()}] -> auto-review MR !{iid} (project {pid}, {where}) → log: {log}")
    except Exception as e:
        print(f"[{_now()}] [WARN] không mở được terminal: {e}; context at {ctx}", file=sys.stderr)


def poll_once(username, who, include_drafts, spawn, state, max_per_cycle=3, baseline=False):
    mrs = gitlab_api.list_review_merge_requests(username, who)
    if mrs and isinstance(mrs[0], dict) and mrs[0].get("error"):
        # Let the supervisor classify: 401/403 -> stop+notify; network/5xx -> backoff.
        daemon_common.guard(mrs[0], "GitLab")
    spawned = 0
    for mr in mrs:
        if mr.get("draft") and not include_drafts:
            continue
        key = f"{mr['project_id']}:{mr['iid']}"
        if state.get(key) == mr["sha"]:
            continue   # already handled at this SHA

        if baseline:
            state[key] = mr["sha"]      # first run: record silently, don't review backlog
            continue

        tag = "cập nhật (commit mới)" if key in state else "mới"
        print(f"[{_now()}] [MR {tag}] !{mr['iid']} {mr['title']!r} bởi {mr['author']} — {mr['web_url']}")
        if not spawn:
            continue                    # preview mode: don't mark as reviewed
        if spawned >= max_per_cycle:
            print(f"[{_now()}]        (đã đạt {max_per_cycle} review/vòng — phần còn lại để vòng sau)")
            break
        _spawn_review(mr)
        spawned += 1
        state[key] = mr["sha"]
        _save_state(state)

    if baseline:
        _save_state(state)
        print(f"[{_now()}] baseline: đánh dấu {len(state)} MR hiện có (không review tồn đọng). "
              f"Sẽ chỉ review MR mới/cập nhật từ giờ. (dùng --review-existing để review cả tồn đọng)")


def run(who, interval, include_drafts, spawn, max_per_cycle, review_existing):
    missing = [k for k in ("GITLAB_URL", "GITLAB_PRIVATE_TOKEN") if not config.get(k)]
    if missing:
        print(f"[ERROR] thiếu config: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)
    u = gitlab_api.current_user()
    if isinstance(u, dict) and u.get("error"):
        print(f"[ERROR] không xác thực được GitLab: {u.get('message')}", file=sys.stderr)
        sys.exit(1)
    username = u.get("username", "")
    print(f"[{_now()}] watching MRs cho @{username} | who={who} | mỗi {interval}s | "
          f"tối đa {max_per_cycle} review/vòng | Ctrl+C để dừng")
    _prune_worktrees()   # clean up any leftover review worktrees from a previous run
    state = _load_state()
    pending_baseline = not state and not review_existing  # first run: baseline, skip backlog

    def _poll():
        nonlocal pending_baseline
        if pending_baseline:
            poll_once(username, who, include_drafts, spawn, state, max_per_cycle, baseline=True)
            pending_baseline = False
        else:
            poll_once(username, who, include_drafts, spawn, state, max_per_cycle)

    # Supervised loop: exponential backoff on network/5xx, stop+log on 401/403 (token).
    daemon_common.supervise(_poll, interval=interval, label="mr_watch")


if __name__ == "__main__":
    p = argparse.ArgumentParser(prog="mr_watch.py")
    p.add_argument("--who", choices=["reviewer", "assignee", "both"], default="reviewer")
    p.add_argument("--interval", type=int, default=300, help="poll seconds (default 300)")
    p.add_argument("--once", action="store_true", help="one pass then exit")
    p.add_argument("--no-spawn", dest="spawn", action="store_false", help="print only, no terminal")
    p.add_argument("--include-drafts", action="store_true")
    p.add_argument("--max-per-cycle", type=int, default=3, help="max reviews launched per poll (default 3)")
    p.add_argument("--review-existing", action="store_true",
                   help="also review the current backlog (skip first-run baseline)")
    a = p.parse_args()
    if a.once:
        u = gitlab_api.current_user()
        if isinstance(u, dict) and u.get("error"):
            print(f"[ERROR] {u.get('message')}", file=sys.stderr); sys.exit(1)
        try:
            poll_once(u.get("username", ""), a.who, a.include_drafts, a.spawn, _load_state(),
                      a.max_per_cycle)
        except daemon_common.DaemonError as e:
            print(f"[ERROR] {e}", file=sys.stderr); sys.exit(1)
    else:
        try:
            run(a.who, a.interval, a.include_drafts, a.spawn, a.max_per_cycle, a.review_existing)
        except KeyboardInterrupt:
            print("\nstopped.")
