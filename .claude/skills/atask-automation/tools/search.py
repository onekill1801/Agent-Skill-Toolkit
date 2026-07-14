#!/usr/bin/env python3
"""Elasticsearch-backed task search tools for aTask AI-agent.

Wraps SearchTools via POST /api/ai/execute.

Usage:
  python3 search.py tasks [--query TEXT] [--list LIST_ID] [--project PROJECT_ID]
                          [--status S1,S2] [--priority P1,P2]
                          [--created-by LOGIN] [--start ISO] [--end ISO]
                          [--page N] [--size N] [--sort-by FIELD] [--sort-order asc|desc]

  python3 search.py my-tasks [--query TEXT] [--list LIST_ID] [--project PROJECT_ID]
                             [--status S1,S2] [--start ISO] [--end ISO]
                             [--page N] [--size N]

  python3 search.py dashboard <workspace_id> [--query TEXT] [--status S1,S2]
                              [--page N] [--size N]

  python3 search.py candidates [--query TEXT] [--exclude ID1,ID2] [--page N] [--size N]
"""

import argparse
import sys

import client
import config
import view


# ── Search wrappers ────────────────────────────────────────────────────────────

def search_tasks(query=None, list_task_id=None, project_id=None,
                 status=None, status_type=None, priority=None, assignee_ids=None,
                 created_by=None, date_start=None, date_end=None,
                 page=0, size=20, sort_by="sortOrder", sort_order="asc") -> dict:
    args = {"page": page, "size": size, "sort_by": sort_by, "sort_order": sort_order}
    if query:
        args["query"] = query
    if list_task_id:
        args["list_task_id"] = list_task_id
    if project_id:
        args["project_id"] = project_id
    if status:
        args["status"] = status if isinstance(status, list) else status.split(",")
    if status_type:
        args["status_type"] = status_type if isinstance(status_type, list) else status_type.split(",")
    if priority:
        args["priority"] = priority if isinstance(priority, list) else priority.split(",")
    if assignee_ids:
        ids = assignee_ids if isinstance(assignee_ids, list) else [int(x) for x in str(assignee_ids).split(",")]
        args["assignee_ids"] = ids
    if created_by:
        args["created_by"] = created_by
    if date_start:
        args["date_start"] = date_start
    if date_end:
        args["date_end"] = date_end
    r = client.execute_tool("search_tasks", args)
    client.check_error(r, "search_tasks")
    return r


def search_my_assigned_tasks(query=None, list_task_id=None, project_id=None,
                              status=None, status_type=None, date_start=None, date_end=None,
                              page=0, size=20) -> dict:
    args = {"page": page, "size": size}
    if query:
        args["query"] = query
    if list_task_id:
        args["list_task_id"] = list_task_id
    if project_id:
        args["project_id"] = project_id
    if status:
        args["status"] = status if isinstance(status, list) else status.split(",")
    if status_type:
        args["status_type"] = status_type if isinstance(status_type, list) else status_type.split(",")
    if date_start:
        args["date_start"] = date_start
    if date_end:
        args["date_end"] = date_end
    r = client.execute_tool("search_my_assigned_tasks", args)
    client.check_error(r, "search_my_assigned_tasks")
    return r


def search_dashboard_tasks(workspace_id: str, query=None, status=None, status_type=None,
                            page=0, size=20) -> dict:
    args = {"workspace_id": workspace_id, "page": page, "size": size}
    if query:
        args["query"] = query
    if status:
        args["status"] = status if isinstance(status, list) else status.split(",")
    if status_type:
        args["status_type"] = status_type if isinstance(status_type, list) else status_type.split(",")
    r = client.execute_tool("search_dashboard_tasks", args)
    client.check_error(r, "search_dashboard_tasks")
    return r


def find_task_candidates(query=None, exclude_ids=None, page=0, size=20) -> dict:
    args = {"page": page, "size": size}
    if query:
        args["query"] = query
    if exclude_ids:
        args["exclude_ids"] = exclude_ids if isinstance(exclude_ids, list) else exclude_ids.split(",")
    r = client.execute_tool("find_task_candidates", args)
    client.check_error(r, "find_task_candidates")
    return r


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    missing = config.validate()
    if missing:
        print(f"[ERROR] Missing config: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)

    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(0)

    cmd = sys.argv[1]

    if cmd == "tasks":
        parser = argparse.ArgumentParser(prog="search.py tasks")
        parser.add_argument("--query", default=None)
        parser.add_argument("--list", dest="list_task_id", default=None)
        parser.add_argument("--project", dest="project_id", default=None)
        parser.add_argument("--status", default=None, help="Comma-separated status IDs")
        parser.add_argument("--status-type", dest="status_type", default=None,
                            help="Comma-separated status groups: todo,processing,approved,completed,closed,custom")
        parser.add_argument("--priority", default=None, help="Comma-separated priority values")
        parser.add_argument("--created-by", default=None)
        parser.add_argument("--start", dest="date_start", default=None)
        parser.add_argument("--end", dest="date_end", default=None)
        parser.add_argument("--page", type=int, default=0)
        parser.add_argument("--size", type=int, default=20)
        parser.add_argument("--sort-by", default="sortOrder")
        parser.add_argument("--sort-order", default="asc", choices=["asc", "desc"])
        view.add_view_args(parser)
        args = parser.parse_args(sys.argv[2:])
        view.emit(search_tasks(
            args.query, args.list_task_id, args.project_id,
            args.status, args.status_type, args.priority, None,
            args.created_by, args.date_start, args.date_end,
            args.page, args.size, args.sort_by, args.sort_order
        ), args.format, view.parse_fields(args.fields))

    elif cmd == "my-tasks":
        parser = argparse.ArgumentParser(prog="search.py my-tasks")
        parser.add_argument("--query", default=None)
        parser.add_argument("--list", dest="list_task_id", default=None)
        parser.add_argument("--project", dest="project_id", default=None)
        parser.add_argument("--status", default=None)
        parser.add_argument("--status-type", dest="status_type", default=None,
                            help="Comma-separated status groups: todo,processing,approved,completed,closed,custom")
        parser.add_argument("--start", dest="date_start", default=None)
        parser.add_argument("--end", dest="date_end", default=None)
        parser.add_argument("--page", type=int, default=0)
        parser.add_argument("--size", type=int, default=20)
        view.add_view_args(parser)
        args = parser.parse_args(sys.argv[2:])
        view.emit(search_my_assigned_tasks(
            args.query, args.list_task_id, args.project_id,
            args.status, args.status_type, args.date_start, args.date_end,
            args.page, args.size
        ), args.format, view.parse_fields(args.fields))

    elif cmd == "dashboard":
        parser = argparse.ArgumentParser(prog="search.py dashboard")
        parser.add_argument("workspace_id")
        parser.add_argument("--query", default=None)
        parser.add_argument("--status", default=None)
        parser.add_argument("--status-type", dest="status_type", default=None,
                            help="Comma-separated status groups: todo,processing,approved,completed,closed,custom")
        parser.add_argument("--page", type=int, default=0)
        parser.add_argument("--size", type=int, default=20)
        view.add_view_args(parser)
        args = parser.parse_args(sys.argv[2:])
        view.emit(search_dashboard_tasks(
            args.workspace_id, args.query, args.status, args.status_type, args.page, args.size
        ), args.format, view.parse_fields(args.fields))

    elif cmd == "candidates":
        parser = argparse.ArgumentParser(prog="search.py candidates")
        parser.add_argument("--query", default=None)
        parser.add_argument("--exclude", dest="exclude_ids", default=None, help="Comma-separated task IDs")
        parser.add_argument("--page", type=int, default=0)
        parser.add_argument("--size", type=int, default=20)
        view.add_view_args(parser)
        args = parser.parse_args(sys.argv[2:])
        view.emit(find_task_candidates(
            args.query, args.exclude_ids, args.page, args.size
        ), args.format, view.parse_fields(args.fields))

    else:
        print(f"Unknown command: {cmd}\n", file=sys.stderr)
        print(__doc__)
        sys.exit(1)
