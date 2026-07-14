#!/usr/bin/env python3
"""Task CRUD + sprint tools for aTask AI-agent.

Wraps TaskTools via POST /api/ai/execute.

Usage:
  python3 tasks.py get <task_id>
  python3 tasks.py query <list_task_id> [--status STATUS] [--parent PARENT_ID]
  python3 tasks.py subtasks <parent_task_id>
  python3 tasks.py create --name NAME --list LIST_ID [--parent PARENT_ID]
                          [--desc DESC] [--priority 1|2|3|4]
                          [--start ISO_DATE] [--due ISO_DATE]
  python3 tasks.py update <task_id> [--name X] [--desc X] [--status X]
                          [--priority 1|2|3|4] [--start ISO] [--due ISO]

  Priority: 1=Khẩn cấp · 2=Cao · 3=Trung bình · 4=Thấp
  (cũng nhận nhãn LOW/MEDIUM/HIGH/URGENT — tự map sang số).
  python3 tasks.py complete <task_id>
  python3 tasks.py delete <task_id>
  python3 tasks.py move <task_id> <target_list_id>
  python3 tasks.py assign-sprint <task_id> <sprint_id>
  python3 tasks.py by-sprint <sprint_id>
  python3 tasks.py assign-users <task_id> <user_id1,user_id2> [--mode add|replace]
  python3 tasks.py unassign <task_id> [--users IDs] [--orgs CODES] [--groups IDs]
"""

import argparse
import sys

import client
import config
import view


# ── Priority ─────────────────────────────────────────────────────────────────
# aTask lưu priority dạng SỐ 1–4. Map cả nhãn EN/VI về số để người dùng nhập kiểu
# nào cũng được (số 1-4, LOW/MEDIUM/HIGH/URGENT, hoặc Khẩn cấp/Cao/Trung bình/Thấp).
#   1 = Khẩn cấp (Urgent) · 2 = Cao (High) · 3 = Trung bình (Medium) · 4 = Thấp (Low)
PRIORITY_MAP = {
    "1": 1, "URGENT": 1, "KHẨN CẤP": 1, "KHAN CAP": 1,
    "2": 2, "HIGH": 2, "CAO": 2,
    "3": 3, "MEDIUM": 3, "TRUNG BÌNH": 3, "TRUNG BINH": 3,
    "4": 4, "LOW": 4, "THẤP": 4, "THAP": 4,
}
PRIORITY_CHOICES = ["1", "2", "3", "4", "LOW", "MEDIUM", "HIGH", "URGENT"]


def normalize_priority(value) -> str:
    """Chuyển nhãn/số priority của người dùng về CHUỖI số "1"–"4" mà aTask yêu cầu.

    aTask API mong priority là chuỗi ("1".."4"); gửi int gây lỗi phía server
    `java.lang.Integer cannot be cast to java.lang.String`. Vì vậy luôn trả str."""
    key = str(value).strip().upper()
    if key not in PRIORITY_MAP:
        raise ValueError(
            f"priority không hợp lệ: {value!r}. Dùng 1-4 hoặc "
            "LOW/MEDIUM/HIGH/URGENT (1=Khẩn cấp, 2=Cao, 3=Trung bình, 4=Thấp)."
        )
    return str(PRIORITY_MAP[key])


# ── Tool wrappers ──────────────────────────────────────────────────────────────

def get_task(task_id: str) -> dict:
    result = client.execute_tool("get_task", {"task_id": task_id})
    client.check_error(result, "get_task")
    return result


def query_tasks(list_task_id: str, status: str = None, parent_id: str = None,
                page: int = None, size: int = None) -> dict:
    args = {"list_task_id": list_task_id}
    if status:
        args["status"] = status
    if parent_id:
        args["parent_id"] = parent_id
    if page is not None:
        args["page"] = page
    if size is not None:
        args["size"] = size
    result = client.execute_tool("query_tasks", args)
    client.check_error(result, "query_tasks")
    return result


def get_subtasks(parent_task_id: str) -> dict:
    result = client.execute_tool("get_subtasks", {"parent_task_id": parent_task_id})
    client.check_error(result, "get_subtasks")
    return result


def create_task(name: str, list_task_id: str, parent_id: str = None,
                description: str = None, priority: str = None,
                start_date: str = None, due_date: str = None) -> dict:
    args = {"name": name, "list_task_id": list_task_id}
    if parent_id:
        args["parent_id"] = parent_id
    if description:
        args["description"] = description
    if priority:
        args["priority"] = normalize_priority(priority)
    if start_date:
        args["start_date"] = start_date
    if due_date:
        args["due_date"] = due_date
    result = client.execute_tool("create_task", args)
    client.check_error(result, "create_task")
    return result


def update_task(task_id: str, name: str = None, description: str = None,
                status: str = None, priority: str = None,
                start_date: str = None, due_date: str = None) -> dict:
    args = {"task_id": task_id}
    if name is not None:
        args["name"] = name
    if description is not None:
        args["description"] = description
    if status is not None:
        args["status"] = status
    if priority is not None:
        args["priority"] = normalize_priority(priority)
    if start_date is not None:
        args["start_date"] = start_date
    if due_date is not None:
        args["due_date"] = due_date
    result = client.execute_tool("update_task", args)
    client.check_error(result, "update_task")
    return result


def complete_task(task_id: str) -> dict:
    result = client.execute_tool("complete_task", {"task_id": task_id})
    client.check_error(result, "complete_task")
    return result


def delete_task(task_id: str) -> dict:
    result = client.execute_tool("delete_task", {"task_id": task_id})
    client.check_error(result, "delete_task")
    return result


def move_task(task_id: str, target_list_id: str) -> dict:
    result = client.execute_tool("move_task", {"task_id": task_id, "target_list_id": target_list_id})
    client.check_error(result, "move_task")
    return result


def assign_task_to_sprint(task_id: str, sprint_id: str) -> dict:
    result = client.execute_tool("assign_task_to_sprint", {"task_id": task_id, "sprint_id": sprint_id})
    client.check_error(result, "assign_task_to_sprint")
    return result


def get_tasks_by_sprint(sprint_id: str) -> dict:
    result = client.execute_tool("get_tasks_by_sprint", {"sprint_id": sprint_id})
    client.check_error(result, "get_tasks_by_sprint")
    return result


def assign_task_users(task_id: str, user_ids: list, mode: str = "add") -> dict:
    """Gán USER (người) cho task. mode 'add' (mặc định) chỉ thêm; 'replace' đồng bộ
    đúng danh sách user_ids và XOÁ người khác (phá huỷ — confirm trước)."""
    args = {"task_id": task_id, "user_ids": [int(u) for u in user_ids]}
    if mode:
        args["mode"] = mode
    result = client.execute_tool("assign_task_users", args)
    client.check_error(result, "assign_task_users")
    return result


def unassign_task(task_id: str, user_ids: list = None, org_ins: list = None,
                  group_ids: list = None) -> dict:
    """Gỡ assignee khỏi task (giữ owner/reviewer/coordinator). Truyền bất kỳ tổ hợp
    user_ids / org_ins / group_ids."""
    args = {"task_id": task_id}
    if user_ids:
        args["user_ids"] = [int(u) for u in user_ids]
    if org_ins:
        args["org_ins"] = list(org_ins)
    if group_ids:
        args["group_ids"] = [int(g) for g in group_ids]
    result = client.execute_tool("unassign_task", args)
    client.check_error(result, "unassign_task")
    return result


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    missing = config.validate()
    if missing:
        print(f"[ERROR] Missing config: {', '.join(missing)} — run: python3 config.py", file=sys.stderr)
        sys.exit(1)

    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(0)

    cmd = sys.argv[1]

    if cmd == "get":
        parser = argparse.ArgumentParser(prog="tasks.py get")
        parser.add_argument("task_id")
        view.add_view_args(parser, default_format="json")
        args = parser.parse_args(sys.argv[2:])
        view.emit(get_task(args.task_id), args.format, view.parse_fields(args.fields))

    elif cmd == "get-detail":
        # REST trực tiếp GET /api/tasks/{id} — trả ĐẦY ĐỦ field hơn kênh ai/execute
        # (description/custom fields...). Cần ATASK_BEARER_TOKEN (session JWT), PAT bị 401.
        parser = argparse.ArgumentParser(prog="tasks.py get-detail")
        parser.add_argument("task_id")
        args = parser.parse_args(sys.argv[2:])
        client.print_json(client.api_get_user(f"/api/tasks/{args.task_id}"))

    elif cmd == "query":
        parser = argparse.ArgumentParser(prog="tasks.py query")
        parser.add_argument("list_task_id")
        parser.add_argument("--status", default=None)
        parser.add_argument("--parent", default=None)
        parser.add_argument("--page", type=int, default=None)
        parser.add_argument("--size", type=int, default=None)
        view.add_view_args(parser)
        args = parser.parse_args(sys.argv[2:])
        view.emit(query_tasks(args.list_task_id, args.status, args.parent, args.page, args.size),
                  args.format, view.parse_fields(args.fields))

    elif cmd == "subtasks":
        parser = argparse.ArgumentParser(prog="tasks.py subtasks")
        parser.add_argument("parent_task_id")
        view.add_view_args(parser)
        args = parser.parse_args(sys.argv[2:])
        view.emit(get_subtasks(args.parent_task_id), args.format, view.parse_fields(args.fields))

    elif cmd == "create":
        parser = argparse.ArgumentParser(prog="tasks.py create")
        parser.add_argument("--name", required=True)
        parser.add_argument("--list", dest="list_id", required=True)
        parser.add_argument("--parent", default=None)
        parser.add_argument("--desc", default=None)
        parser.add_argument("--priority", default=None, choices=PRIORITY_CHOICES,
                            help="1=Khẩn cấp, 2=Cao, 3=Trung bình, 4=Thấp (hoặc LOW/MEDIUM/HIGH/URGENT)")
        parser.add_argument("--start", default=None)
        parser.add_argument("--due", default=None)
        args = parser.parse_args(sys.argv[2:])
        client.print_json(create_task(
            args.name, args.list_id, args.parent, args.desc,
            args.priority, args.start, args.due
        ))

    elif cmd == "update":
        parser = argparse.ArgumentParser(prog="tasks.py update")
        parser.add_argument("task_id")
        parser.add_argument("--name", default=None)
        parser.add_argument("--desc", default=None)
        parser.add_argument("--status", default=None)
        parser.add_argument("--priority", default=None, choices=PRIORITY_CHOICES,
                            help="1=Khẩn cấp, 2=Cao, 3=Trung bình, 4=Thấp (hoặc LOW/MEDIUM/HIGH/URGENT)")
        parser.add_argument("--start", default=None)
        parser.add_argument("--due", default=None)
        args = parser.parse_args(sys.argv[2:])
        client.print_json(update_task(
            args.task_id, args.name, args.desc, args.status,
            args.priority, args.start, args.due
        ))

    elif cmd == "complete":
        parser = argparse.ArgumentParser(prog="tasks.py complete")
        parser.add_argument("task_id")
        args = parser.parse_args(sys.argv[2:])
        client.print_json(complete_task(args.task_id))

    elif cmd == "delete":
        parser = argparse.ArgumentParser(prog="tasks.py delete")
        parser.add_argument("task_id")
        args = parser.parse_args(sys.argv[2:])
        client.print_json(delete_task(args.task_id))

    elif cmd == "move":
        parser = argparse.ArgumentParser(prog="tasks.py move")
        parser.add_argument("task_id")
        parser.add_argument("target_list_id")
        args = parser.parse_args(sys.argv[2:])
        client.print_json(move_task(args.task_id, args.target_list_id))

    elif cmd == "assign-sprint":
        parser = argparse.ArgumentParser(prog="tasks.py assign-sprint")
        parser.add_argument("task_id")
        parser.add_argument("sprint_id")
        args = parser.parse_args(sys.argv[2:])
        client.print_json(assign_task_to_sprint(args.task_id, args.sprint_id))

    elif cmd == "by-sprint":
        parser = argparse.ArgumentParser(prog="tasks.py by-sprint")
        parser.add_argument("sprint_id")
        view.add_view_args(parser)
        args = parser.parse_args(sys.argv[2:])
        view.emit(get_tasks_by_sprint(args.sprint_id), args.format, view.parse_fields(args.fields))

    elif cmd == "assign-users":
        parser = argparse.ArgumentParser(prog="tasks.py assign-users")
        parser.add_argument("task_id")
        parser.add_argument("user_ids", help="Comma-separated aTask user IDs")
        parser.add_argument("--mode", default="add", choices=["add", "replace"])
        args = parser.parse_args(sys.argv[2:])
        client.print_json(assign_task_users(args.task_id, args.user_ids.split(","), args.mode))

    elif cmd == "unassign":
        parser = argparse.ArgumentParser(prog="tasks.py unassign")
        parser.add_argument("task_id")
        parser.add_argument("--users", default=None, help="Comma-separated user IDs")
        parser.add_argument("--orgs", default=None, help="Comma-separated org codes")
        parser.add_argument("--groups", default=None, help="Comma-separated group IDs")
        args = parser.parse_args(sys.argv[2:])
        client.print_json(unassign_task(
            args.task_id,
            args.users.split(",") if args.users else None,
            args.orgs.split(",") if args.orgs else None,
            args.groups.split(",") if args.groups else None,
        ))

    else:
        print(f"Unknown command: {cmd}\n", file=sys.stderr)
        print(__doc__)
        sys.exit(1)
