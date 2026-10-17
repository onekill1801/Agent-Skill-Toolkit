#!/usr/bin/env python3
"""Checklist, comment, and attachment tools for aTask AI-agent.

Wraps ChecklistTools and CommentTools via POST /api/ai/execute.

Usage:
  python3 checklists.py list <task_id>
  python3 checklists.py get <checklist_id>
  python3 checklists.py create <task_id> <name> [--value checked|unchecked]
  python3 checklists.py delete <checklist_id>
  python3 checklists.py count <task_id>
  python3 checklists.py attachments <task_id>

  python3 checklists.py comments <task_id>
  python3 checklists.py comment <comment_id>
  python3 checklists.py add-comment <task_id> <content>
  python3 checklists.py del-comment <comment_id>
  python3 checklists.py count-comments <task_id>
"""

import argparse
import sys

import client
import config


# ── Checklist wrappers ─────────────────────────────────────────────────────────

def get_checklists(task_id: str) -> dict:
    r = client.execute_tool("get_checklists", {"task_id": task_id})
    client.check_error(r, "get_checklists")
    return r


def get_checklist(checklist_id: str) -> dict:
    r = client.execute_tool("get_checklist", {"checklist_id": checklist_id})
    client.check_error(r, "get_checklist")
    return r


def create_checklist(task_id: str, name: str, value: str = None) -> dict:
    args = {"task_id": task_id, "name": name}
    if value:
        args["value"] = value
    r = client.execute_tool("create_checklist", args)
    client.check_error(r, "create_checklist")
    return r


def delete_checklist(checklist_id: str) -> dict:
    r = client.execute_tool("delete_checklist", {"checklist_id": checklist_id})
    client.check_error(r, "delete_checklist")
    return r


def count_checklists(task_id: str) -> dict:
    r = client.execute_tool("count_checklists", {"task_id": task_id})
    client.check_error(r, "count_checklists")
    return r


def get_attachments(task_id: str) -> dict:
    r = client.execute_tool("get_attachments", {"task_id": task_id})
    client.check_error(r, "get_attachments")
    return r


# ── Comment wrappers ───────────────────────────────────────────────────────────

def get_comments(task_id: str) -> dict:
    r = client.execute_tool("get_comments", {"task_id": task_id})
    client.check_error(r, "get_comments")
    return r


def get_comment(comment_id: str) -> dict:
    r = client.execute_tool("get_comment", {"comment_id": comment_id})
    client.check_error(r, "get_comment")
    return r


def create_comment(task_id: str, content: str) -> dict:
    r = client.execute_tool("create_comment", {"task_id": task_id, "content": content})
    client.check_error(r, "create_comment")
    return r


def delete_comment(comment_id: str) -> dict:
    r = client.execute_tool("delete_comment", {"comment_id": comment_id})
    client.check_error(r, "delete_comment")
    return r


def count_comments(task_id: str) -> dict:
    r = client.execute_tool("count_comments", {"task_id": task_id})
    client.check_error(r, "count_comments")
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

    if cmd == "list":
        parser = argparse.ArgumentParser(prog="checklists.py list")
        parser.add_argument("task_id")
        args = parser.parse_args(sys.argv[2:])
        client.print_json(get_checklists(args.task_id))

    elif cmd == "get":
        parser = argparse.ArgumentParser(prog="checklists.py get")
        parser.add_argument("checklist_id")
        args = parser.parse_args(sys.argv[2:])
        client.print_json(get_checklist(args.checklist_id))

    elif cmd == "create":
        parser = argparse.ArgumentParser(prog="checklists.py create")
        parser.add_argument("task_id")
        parser.add_argument("name")
        parser.add_argument("--value", default=None)
        args = parser.parse_args(sys.argv[2:])
        client.print_json(create_checklist(args.task_id, args.name, args.value))

    elif cmd == "delete":
        parser = argparse.ArgumentParser(prog="checklists.py delete")
        parser.add_argument("checklist_id")
        args = parser.parse_args(sys.argv[2:])
        client.print_json(delete_checklist(args.checklist_id))

    elif cmd == "count":
        parser = argparse.ArgumentParser(prog="checklists.py count")
        parser.add_argument("task_id")
        args = parser.parse_args(sys.argv[2:])
        client.print_json(count_checklists(args.task_id))

    elif cmd == "attachments":
        parser = argparse.ArgumentParser(prog="checklists.py attachments")
        parser.add_argument("task_id")
        args = parser.parse_args(sys.argv[2:])
        client.print_json(get_attachments(args.task_id))

    elif cmd == "comments":
        parser = argparse.ArgumentParser(prog="checklists.py comments")
        parser.add_argument("task_id")
        args = parser.parse_args(sys.argv[2:])
        client.print_json(get_comments(args.task_id))

    elif cmd == "comment":
        parser = argparse.ArgumentParser(prog="checklists.py comment")
        parser.add_argument("comment_id")
        args = parser.parse_args(sys.argv[2:])
        client.print_json(get_comment(args.comment_id))

    elif cmd == "add-comment":
        parser = argparse.ArgumentParser(prog="checklists.py add-comment")
        parser.add_argument("task_id")
        parser.add_argument("content")
        args = parser.parse_args(sys.argv[2:])
        client.print_json(create_comment(args.task_id, args.content))

    elif cmd == "del-comment":
        parser = argparse.ArgumentParser(prog="checklists.py del-comment")
        parser.add_argument("comment_id")
        args = parser.parse_args(sys.argv[2:])
        client.print_json(delete_comment(args.comment_id))

    elif cmd == "count-comments":
        parser = argparse.ArgumentParser(prog="checklists.py count-comments")
        parser.add_argument("task_id")
        args = parser.parse_args(sys.argv[2:])
        client.print_json(count_comments(args.task_id))

    else:
        print(f"Unknown command: {cmd}\n", file=sys.stderr)
        print(__doc__)
        sys.exit(1)
