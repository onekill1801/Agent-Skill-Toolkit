#!/usr/bin/env python3
"""Project, sprint, workspace, and list/board tools for aTask AI-agent.

Wraps ProjectTools, WorkspaceTools, and ListTaskTools via POST /api/ai/execute.

Usage:
  python3 projects.py get-project <project_id>
  python3 projects.py my-projects [--filter NAME] [--page N] [--size N]
  python3 projects.py get-sprint <sprint_id>
  python3 projects.py sprints <project_id>
  python3 projects.py project-for-list <list_id>

  python3 projects.py workspace <workspace_id> [--type my]
  python3 projects.py get-list <list_id>
  python3 projects.py lists <workspace_id>
  python3 projects.py my-lists

  # [WRITE] (cần scope write + là thành viên project — server enforce authz)
  python3 projects.py create-project <name> [--code C] [--priority P] [--description D]
  python3 projects.py create-sprint <project_id> <name> [--goal G] [--start-date ISO] [--end-date ISO]
  python3 projects.py start-sprint <sprint_id>
  python3 projects.py complete-sprint <sprint_id>
  python3 projects.py create-list <name> [--description D] [--priority P]
"""

import argparse
import sys

import client
import config


# ── Project wrappers ───────────────────────────────────────────────────────────

def get_project(project_id: str) -> dict:
    r = client.execute_tool("get_project", {"project_id": project_id})
    client.check_error(r, "get_project")
    return r


def query_my_projects(name_filter: str = None, page: int = 0, size: int = 20) -> dict:
    args = {"page": page, "size": size}
    if name_filter:
        args["name_filter"] = name_filter
    r = client.execute_tool("query_my_projects", args)
    client.check_error(r, "query_my_projects")
    return r


def get_sprint(sprint_id: str) -> dict:
    r = client.execute_tool("get_sprint", {"sprint_id": sprint_id})
    client.check_error(r, "get_sprint")
    return r


def query_sprints(project_id: str) -> dict:
    r = client.execute_tool("query_sprints", {"project_id": project_id})
    client.check_error(r, "query_sprints")
    return r


def get_project_for_list(list_id: str) -> dict:
    r = client.execute_tool("get_project_for_list", {"list_id": list_id})
    client.check_error(r, "get_project_for_list")
    return r


# ── Workspace wrappers ─────────────────────────────────────────────────────────

def get_workspace(workspace_id: str, ws_type: str = "my") -> dict:
    r = client.execute_tool("get_workspace", {"workspace_id": workspace_id, "type": ws_type})
    client.check_error(r, "get_workspace")
    return r


# ── List/Board wrappers ────────────────────────────────────────────────────────

def get_list(list_id: str) -> dict:
    r = client.execute_tool("get_list", {"list_id": list_id})
    client.check_error(r, "get_list")
    return r


def query_lists_by_workspace(workspace_id: str) -> dict:
    r = client.execute_tool("query_lists_by_workspace", {"workspace_id": workspace_id})
    client.check_error(r, "query_lists_by_workspace")
    return r


def query_my_lists() -> dict:
    r = client.execute_tool("query_my_lists", {})
    client.check_error(r, "query_my_lists")
    return r


# ── WRITE wrappers (server enforce scope write + membership/tenant authz) ────────

def _compact(d: dict) -> dict:
    return {k: v for k, v in d.items() if v is not None}


def create_project(name, code=None, priority=None, description=None) -> dict:
    r = client.execute_tool("create_project", _compact(
        {"name": name, "code": code, "priority": priority, "description": description}))
    client.check_error(r, "create_project")
    return r


def create_sprint(project_id, name, goal=None, start_date=None, end_date=None) -> dict:
    r = client.execute_tool("create_sprint", _compact(
        {"project_id": project_id, "name": name, "goal": goal,
         "start_date": start_date, "end_date": end_date}))
    client.check_error(r, "create_sprint")
    return r


def start_sprint(sprint_id) -> dict:
    r = client.execute_tool("start_sprint", {"sprint_id": sprint_id})
    client.check_error(r, "start_sprint")
    return r


def complete_sprint(sprint_id) -> dict:
    r = client.execute_tool("complete_sprint", {"sprint_id": sprint_id})
    client.check_error(r, "complete_sprint")
    return r


def create_list(name, description=None, priority=None) -> dict:
    r = client.execute_tool("create_list", _compact(
        {"name": name, "description": description, "priority": priority}))
    client.check_error(r, "create_list")
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

    if cmd == "get-project":
        parser = argparse.ArgumentParser(prog="projects.py get-project")
        parser.add_argument("project_id")
        args = parser.parse_args(sys.argv[2:])
        client.print_json(get_project(args.project_id))

    elif cmd == "my-projects":
        parser = argparse.ArgumentParser(prog="projects.py my-projects")
        parser.add_argument("--filter", dest="name_filter", default=None)
        parser.add_argument("--page", type=int, default=0)
        parser.add_argument("--size", type=int, default=20)
        args = parser.parse_args(sys.argv[2:])
        client.print_json(query_my_projects(args.name_filter, args.page, args.size))

    elif cmd == "get-sprint":
        parser = argparse.ArgumentParser(prog="projects.py get-sprint")
        parser.add_argument("sprint_id")
        args = parser.parse_args(sys.argv[2:])
        client.print_json(get_sprint(args.sprint_id))

    elif cmd == "sprints":
        parser = argparse.ArgumentParser(prog="projects.py sprints")
        parser.add_argument("project_id")
        args = parser.parse_args(sys.argv[2:])
        client.print_json(query_sprints(args.project_id))

    elif cmd == "project-for-list":
        parser = argparse.ArgumentParser(prog="projects.py project-for-list")
        parser.add_argument("list_id")
        args = parser.parse_args(sys.argv[2:])
        client.print_json(get_project_for_list(args.list_id))

    elif cmd == "workspace":
        parser = argparse.ArgumentParser(prog="projects.py workspace")
        parser.add_argument("workspace_id")
        parser.add_argument("--type", dest="ws_type", default="my")
        args = parser.parse_args(sys.argv[2:])
        client.print_json(get_workspace(args.workspace_id, args.ws_type))

    elif cmd == "get-list":
        parser = argparse.ArgumentParser(prog="projects.py get-list")
        parser.add_argument("list_id")
        args = parser.parse_args(sys.argv[2:])
        client.print_json(get_list(args.list_id))

    elif cmd == "lists":
        parser = argparse.ArgumentParser(prog="projects.py lists")
        parser.add_argument("workspace_id")
        args = parser.parse_args(sys.argv[2:])
        client.print_json(query_lists_by_workspace(args.workspace_id))

    elif cmd == "my-lists":
        client.print_json(query_my_lists())

    elif cmd == "create-project":
        parser = argparse.ArgumentParser(prog="projects.py create-project")
        parser.add_argument("name")
        parser.add_argument("--code", default=None)
        parser.add_argument("--priority", default=None)
        parser.add_argument("--description", default=None)
        args = parser.parse_args(sys.argv[2:])
        client.print_json(create_project(args.name, args.code, args.priority, args.description))

    elif cmd == "create-sprint":
        parser = argparse.ArgumentParser(prog="projects.py create-sprint")
        parser.add_argument("project_id")
        parser.add_argument("name")
        parser.add_argument("--goal", default=None)
        parser.add_argument("--start-date", dest="start_date", default=None)
        parser.add_argument("--end-date", dest="end_date", default=None)
        args = parser.parse_args(sys.argv[2:])
        client.print_json(create_sprint(args.project_id, args.name, args.goal, args.start_date, args.end_date))

    elif cmd == "start-sprint":
        parser = argparse.ArgumentParser(prog="projects.py start-sprint")
        parser.add_argument("sprint_id")
        args = parser.parse_args(sys.argv[2:])
        client.print_json(start_sprint(args.sprint_id))

    elif cmd == "complete-sprint":
        parser = argparse.ArgumentParser(prog="projects.py complete-sprint")
        parser.add_argument("sprint_id")
        args = parser.parse_args(sys.argv[2:])
        client.print_json(complete_sprint(args.sprint_id))

    elif cmd == "create-list":
        parser = argparse.ArgumentParser(prog="projects.py create-list")
        parser.add_argument("name")
        parser.add_argument("--description", default=None)
        parser.add_argument("--priority", default=None)
        args = parser.parse_args(sys.argv[2:])
        client.print_json(create_list(args.name, args.description, args.priority))

    else:
        print(f"Unknown command: {cmd}\n", file=sys.stderr)
        print(__doc__)
        sys.exit(1)
