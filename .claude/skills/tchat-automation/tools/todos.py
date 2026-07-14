#!/usr/bin/env python3
"""TChat — todo / task tools.

Todos are tasks created from chat messages. Endpoints verified from traffic.

Usage:
  python todos.py list [--type BY_ME|TO_ME|IMPORTANT] [--group GROUP_ID]
                       [--filter EXPIRED] [--limit N] [--page N]
        # GET /message-query/todo
  python todos.py count-expired
        # GET /message-query/todo/total-expired

  [WRITE] python todos.py create --group GROUP_ID --title T [--detail D]
                 [--assignee USER_ID] [--assigner USER_ID]
                 [--due ISO] [--started ISO] [--important] [--yes]
        # POST /message-query/todo  (verified body)
        # Without --yes: prints a DRY-RUN preview and sends NOTHING.
        # assignee/assigner default to the current user (GET /user/me).

  [WRITE] python todos.py delete --id TODO_ID [--yes]
        # DELETE /message-query/todo/{id} (verified). Dry-run without --yes.
"""

import argparse
import sys
from datetime import datetime, timezone

import client
import config


def list_todos(todo_type=None, group_id=None, flt=None, limit=30, page=1) -> dict:
    r = client.api_get("/message-query/todo", {
        "todoType": todo_type, "groupId": group_id,
        "filter": flt, "limit": limit, "page": page,
    })
    client.check_error(r, "list_todos")
    return r


def count_expired() -> dict:
    r = client.api_get("/message-query/todo/total-expired")
    client.check_error(r, "count_expired")
    return r


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def build_todo_payload(group_id, title, detail="", assignee=None, assigner=None,
                       due_at=None, started_at=None, important=False) -> dict:
    """Build the verified POST /message-query/todo body."""
    me = None
    if not assignee or not assigner:
        r = client.api_get("/user/me")
        client.check_error(r, "get_me(for todo)")
        me = r.get("id")
    item = {
        "title": title,
        "detail": detail or "",
        "assigner": assigner or me,
        "assignee": assignee or me,
        "isImportant": bool(important),
        "startedAt": started_at or _now_iso(),
    }
    if due_at:
        item["dueAt"] = due_at
    return {"data": [item], "groupId": group_id}


def create_todo(payload: dict) -> dict:
    """[WRITE] POST /message-query/todo. Caller must have user confirmation."""
    r = client.api_post("/message-query/todo", payload)
    client.check_error(r, "create_todo")
    return r


def delete_todo(todo_id: str) -> dict:
    """[WRITE] DELETE /message-query/todo/{id} (verified). Confirm before calling."""
    r = client._http(f"{config.base_url()}/message-query/todo/{todo_id}", method="DELETE")
    client.check_error(r, "delete_todo")
    return r


if __name__ == "__main__":
    missing = config.validate()
    if missing:
        print(f"[ERROR] Missing config: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(0)

    cmd = sys.argv[1]
    if cmd == "list":
        p = argparse.ArgumentParser(prog="todos.py list")
        p.add_argument("--type", dest="todo_type", default=None,
                       choices=["BY_ME", "TO_ME", "IMPORTANT"])
        p.add_argument("--group", dest="group_id", default=None)
        p.add_argument("--filter", dest="flt", default=None)
        p.add_argument("--limit", type=int, default=30)
        p.add_argument("--page", type=int, default=1)
        a = p.parse_args(sys.argv[2:])
        client.print_json(list_todos(a.todo_type, a.group_id, a.flt, a.limit, a.page))
    elif cmd == "count-expired":
        client.print_json(count_expired())
    elif cmd == "delete":
        p = argparse.ArgumentParser(prog="todos.py delete")
        p.add_argument("--id", dest="todo_id", required=True)
        p.add_argument("--yes", action="store_true", help="actually delete; omit for dry-run")
        a = p.parse_args(sys.argv[2:])
        if not a.yes:
            print(f"[DRY-RUN] Would DELETE /message-query/todo/{a.todo_id}. Re-run with --yes.",
                  file=sys.stderr)
            sys.exit(0)
        client.print_json(delete_todo(a.todo_id))
    elif cmd == "create":
        p = argparse.ArgumentParser(prog="todos.py create")
        p.add_argument("--group", dest="group_id", required=True)
        p.add_argument("--title", required=True)
        p.add_argument("--detail", default="")
        p.add_argument("--assignee", default=None, help="user _id (default: me)")
        p.add_argument("--assigner", default=None, help="user _id (default: me)")
        p.add_argument("--due", dest="due_at", default=None, help="ISO 8601, e.g. 2026-06-15T16:59:59.000Z")
        p.add_argument("--started", dest="started_at", default=None, help="ISO 8601 (default: now)")
        p.add_argument("--important", action="store_true")
        p.add_argument("--yes", action="store_true", help="actually send; omit for dry-run")
        a = p.parse_args(sys.argv[2:])
        payload = build_todo_payload(a.group_id, a.title, a.detail, a.assignee,
                                     a.assigner, a.due_at, a.started_at, a.important)
        if not a.yes:
            print("[DRY-RUN] No request sent. Re-run with --yes to create this todo:",
                  file=sys.stderr)
            client.print_json(payload)
            sys.exit(0)
        client.print_json(create_todo(payload))
    else:
        print(f"Unknown command: {cmd}\n", file=sys.stderr)
        print(__doc__)
        sys.exit(1)
