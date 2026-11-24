#!/usr/bin/env python3
"""Git worktree manager — cách ly không gian làm việc cho agent chạy song song.

Mỗi agent phụ được cấp một worktree riêng (thư mục song song với repo gốc, gắn nhánh
git riêng) để nhiều agent code đồng thời mà không giẫm lên nhau trên cùng một checkout.

Chỉ dùng Python stdlib (subprocess + os) — không cần pip install.

Usage (CLI, in JSON):
    python worktree_manager.py create --project <repo> --task 123 [--branch feature/x]
    python worktree_manager.py remove --project <repo> --workspace <path> [--force]

Hoặc import:
    from worktree_manager import create_agent_workspace, remove_agent_workspace
"""

import argparse
import json
import os
import subprocess
import sys

# Cross-platform: ép UTF-8 stdout/stderr để JSON có ký tự non-ASCII (tên nhánh/đường dẫn
# tiếng Việt) không crash trên console Windows cp1252. Khớp convention các tool khác.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass


def _git(project_path, *args):
    """Chạy một lệnh git trong project_path. Trả (returncode, stdout, stderr)."""
    cmd = ["git", "-C", project_path, *args]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def _is_git_repo(project_path):
    rc, _, _ = _git(project_path, "rev-parse", "--is-inside-work-tree")
    return rc == 0


def _branch_exists(project_path, branch):
    """True nếu nhánh local đã tồn tại."""
    rc, _, _ = _git(project_path, "rev-parse", "--verify", "--quiet", "refs/heads/" + branch)
    return rc == 0


def default_branch_name(task_id):
    """Quy ước nhánh cho agent phụ."""
    return "feature/task-{}".format(task_id)


def workspace_path_for(project_path, task_id):
    """Đường dẫn worktree: thư mục song song dạng {tên_project}-agent-{task_id}."""
    project_path = os.path.abspath(project_path)
    project_name = os.path.basename(project_path.rstrip("/\\"))
    parent = os.path.dirname(project_path)
    return os.path.join(parent, "{}-agent-{}".format(project_name, task_id))


def create_agent_workspace(project_path, task_id, branch_name=None):
    """Tạo worktree cho agent phụ.

    - Sinh thư mục tạm `{tên_project}-agent-{task_id}` song song với repo gốc.
    - Gắn với nhánh riêng (mặc định `feature/task-{task_id}`).
    - Nếu nhánh đã tồn tại: checkout nhánh đó sang worktree mới (không tạo lại).

    Trả dict JSON-friendly; lỗi trả {"error": True, ...}.
    """
    project_path = os.path.abspath(project_path)
    if not os.path.isdir(project_path):
        return {"error": True, "message": "Project path not found: {}".format(project_path)}
    if not _is_git_repo(project_path):
        return {"error": True, "message": "Not a git repository: {}".format(project_path)}

    branch = branch_name or default_branch_name(task_id)
    workspace_path = workspace_path_for(project_path, task_id)

    if os.path.exists(workspace_path):
        return {
            "error": True,
            "message": "Workspace already exists: {}".format(workspace_path),
            "workspace_path": workspace_path,
        }

    branch_existed = _branch_exists(project_path, branch)
    if branch_existed:
        # Nhánh có sẵn -> chỉ checkout sang worktree mới.
        rc, out, err = _git(project_path, "worktree", "add", workspace_path, branch)
    else:
        # Nhánh mới -> tạo nhánh dựa trên HEAD hiện tại rồi gắn vào worktree.
        rc, out, err = _git(project_path, "worktree", "add", "-b", branch, workspace_path)

    if rc != 0:
        return {
            "error": True,
            "message": err or out or "git worktree add failed",
            "branch": branch,
            "branch_existed": branch_existed,
            "workspace_path": workspace_path,
        }

    return {
        "error": False,
        "workspace_path": workspace_path,
        "branch": branch,
        "branch_existed": branch_existed,
        "project_path": project_path,
        "message": "Worktree created at {} on branch {}".format(workspace_path, branch),
    }


def remove_agent_workspace(project_path, workspace_path, force=False):
    """Dọn worktree sau khi agent phụ hoàn thành.

    Dùng `git worktree remove`; thêm `--force` nếu worktree còn thay đổi chưa commit.
    Sau khi xoá chạy `worktree prune` để gỡ metadata thừa.
    """
    project_path = os.path.abspath(project_path)
    workspace_path = os.path.abspath(workspace_path)
    if not _is_git_repo(project_path):
        return {"error": True, "message": "Not a git repository: {}".format(project_path)}

    args = ["worktree", "remove", workspace_path]
    if force:
        args.append("--force")
    rc, out, err = _git(project_path, *args)
    if rc != 0:
        return {
            "error": True,
            "message": err or out or "git worktree remove failed",
            "workspace_path": workspace_path,
            "hint": "Worktree có thay đổi chưa commit? Thử lại với --force.",
        }

    _git(project_path, "worktree", "prune")
    return {
        "error": False,
        "workspace_path": workspace_path,
        "message": "Worktree removed: {}".format(workspace_path),
    }


def _main():
    parser = argparse.ArgumentParser(description="Git worktree manager cho agent song song")
    sub = parser.add_subparsers(dest="action", required=True)

    p = sub.add_parser("create", help="Tạo worktree cho agent phụ")
    p.add_argument("--project", required=True, help="Đường dẫn repo gốc")
    p.add_argument("--task", required=True, help="Task id")
    p.add_argument("--branch", default=None, help="Tên nhánh (mặc định feature/task-<id>)")

    p = sub.add_parser("remove", help="Xoá worktree")
    p.add_argument("--project", required=True, help="Đường dẫn repo gốc")
    p.add_argument("--workspace", required=True, help="Đường dẫn worktree cần xoá")
    p.add_argument("--force", action="store_true", help="Buộc xoá kể cả khi còn thay đổi")

    args = parser.parse_args()
    if args.action == "create":
        out = create_agent_workspace(args.project, args.task, args.branch)
    else:
        out = remove_agent_workspace(args.project, args.workspace, args.force)

    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 1 if out.get("error") else 0


if __name__ == "__main__":
    sys.exit(_main())
