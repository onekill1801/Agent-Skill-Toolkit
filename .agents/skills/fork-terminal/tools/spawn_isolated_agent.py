#!/usr/bin/env python3
"""Orchestrator — spawn một agent phụ trong không gian làm việc đã CÁCH LY.

Nối chuỗi 3 bước:
  1. worktree_manager.create_agent_workspace  -> worktree + nhánh riêng
  2. runtime_isolator.isolate_environment      -> đổi cổng + tên DB tránh xung đột
  3. fork_terminal.fork_terminal(command, cwd) -> mở terminal/CLI mới, CWD = worktree

Nhờ đó nhiều agent (Claude/Codex/Gemini...) chạy song song mà không giẫm lên nhau:
mỗi agent có checkout riêng, cổng riêng, database riêng.

Chỉ dùng Python stdlib. Khi worktree tạo xong nhưng isolate lỗi -> tự rollback (xoá worktree)
để không bỏ lại không gian dở dang.

Usage (CLI, in JSON):
    python spawn_isolated_agent.py spawn --project <repo> --task 123 \
        --cmd "claude" [--branch feature/x] [--dry-run]
    python spawn_isolated_agent.py cleanup --project <repo> --task 123 [--force]
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import worktree_manager as wm        # noqa: E402
import runtime_isolator as ri        # noqa: E402
import fork_terminal as ft           # noqa: E402

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass


def spawn_isolated_agent(project_path, task_id, command, branch_name=None, dry_run=False):
    """Tạo worktree + cách ly cấu hình + fork terminal chạy `command` trong worktree đó."""
    workspace = wm.create_agent_workspace(project_path, task_id, branch_name)
    if workspace.get("error"):
        return {"error": True, "step": "create_workspace", "detail": workspace}

    workspace_path = workspace["workspace_path"]

    isolation = ri.isolate_environment(workspace_path, task_id)
    if isolation.get("error"):
        # Rollback: worktree đã tạo nhưng không cách ly được -> dọn để tránh rác.
        rollback = wm.remove_agent_workspace(project_path, workspace_path, force=True)
        return {
            "error": True,
            "step": "isolate_environment",
            "detail": isolation,
            "rollback": rollback,
        }

    result = {
        "error": False,
        "workspace": workspace,
        "isolation": isolation,
        "command": command,
        "cwd": workspace_path,
    }

    if dry_run:
        result["launched"] = False
        result["note"] = "dry-run: worktree + cách ly đã thực hiện, KHÔNG mở terminal."
        return result

    # CWD của terminal mới trỏ thẳng vào worktree đã cách ly (KHÔNG phải repo gốc).
    launch_output = ft.fork_terminal(command, cwd=workspace_path)
    result["launched"] = True
    result["launch_output"] = launch_output
    return result


def cleanup_agent(project_path, task_id, force=False):
    """Xoá worktree của một task (suy ra đường dẫn theo quy ước)."""
    workspace_path = wm.workspace_path_for(project_path, task_id)
    return wm.remove_agent_workspace(project_path, workspace_path, force=force)


def _main():
    parser = argparse.ArgumentParser(description="Spawn agent phụ trong worktree đã cách ly")
    sub = parser.add_subparsers(dest="action", required=True)

    p = sub.add_parser("spawn", help="Tạo worktree + cách ly + fork terminal")
    p.add_argument("--project", required=True, help="Đường dẫn repo gốc")
    p.add_argument("--task", required=True, help="Task id")
    p.add_argument("--cmd", required=True, help="Lệnh/CLI chạy trong terminal mới (vd: claude)")
    p.add_argument("--branch", default=None, help="Tên nhánh (mặc định feature/task-<id>)")
    p.add_argument("--dry-run", action="store_true", help="Chỉ tạo+cách ly, không mở terminal")

    p = sub.add_parser("cleanup", help="Xoá worktree của task")
    p.add_argument("--project", required=True, help="Đường dẫn repo gốc")
    p.add_argument("--task", required=True, help="Task id")
    p.add_argument("--force", action="store_true", help="Buộc xoá kể cả khi còn thay đổi")

    args = parser.parse_args()
    if args.action == "spawn":
        out = spawn_isolated_agent(args.project, args.task, args.cmd, args.branch, args.dry_run)
    else:
        out = cleanup_agent(args.project, args.task, args.force)

    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 1 if out.get("error") else 0


if __name__ == "__main__":
    sys.exit(_main())
