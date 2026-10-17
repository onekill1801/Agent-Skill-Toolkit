#!/usr/bin/env python3
"""Statistics and analytics tools for aTask AI-agent.

Wraps StatisticsTools via POST /api/ai/execute.

Usage:
  python3 analytics.py stats
  python3 analytics.py by-status [--scope my|org] [--org-prefix PREFIX]
                                  [--start ISO] [--end ISO]
  python3 analytics.py by-assignee [--list LIST_ID] [--start ISO] [--end ISO]
  python3 analytics.py by-org [--scope my|org] [--start ISO] [--end ISO]
  python3 analytics.py by-priority [--start ISO] [--end ISO]
  python3 analytics.py overdue <list_task_id> [--start ISO] [--end ISO]
  python3 analytics.py trends --start ISO --end ISO --period day|week|month
  python3 analytics.py finish-rates [--dept DEPT] [--start ISO] [--end ISO]
  python3 analytics.py unassigned [--list LIST_ID] [--start ISO] [--end ISO]
  python3 analytics.py history [--scope my|org] [--page N] [--size N]
  python3 analytics.py my-dashboard [--start ISO] [--end ISO] [--day-left N]
  python3 analytics.py org-dashboard [--org ORG] [--start ISO] [--end ISO]
  python3 analytics.py user-dashboard <user_id> [--org ORG] [--start ISO] [--end ISO] [--day-left N]
  python3 analytics.py project-dashboard <project_id> [--start ISO] [--end ISO] [--day-left N]
  python3 analytics.py workload [--org ORG] [--q TEXT] [--start ISO] [--end ISO] [--page N] [--size N]
  python3 analytics.py by-metric --scope my|org|user|project --metric total|overdue|expiringSoon|urgent|completed|inProgress
                                 [--ref-id ID] [--org ORG] [--start ISO] [--end ISO] [--page N] [--size N]
  python3 analytics.py recent [--scope my|org] [--size N] [--org ORG]
"""

import argparse
import sys

import client
import config


# ── Analytics wrappers ─────────────────────────────────────────────────────────

def get_task_statistics() -> dict:
    r = client.execute_tool("get_task_statistics", {})
    client.check_error(r, "get_task_statistics")
    return r


def get_task_count_by_status(scope_type="my", org_prefix=None,
                              date_start=None, date_end=None) -> dict:
    args = {"scope_type": scope_type}
    if org_prefix:
        args["org_prefix"] = org_prefix
    if date_start:
        args["date_start"] = date_start
    if date_end:
        args["date_end"] = date_end
    r = client.execute_tool("get_task_count_by_status", args)
    client.check_error(r, "get_task_count_by_status")
    return r


def get_task_count_by_assignee(list_task_id=None, date_start=None, date_end=None) -> dict:
    args = {}
    if list_task_id:
        args["list_task_id"] = list_task_id
    if date_start:
        args["date_start"] = date_start
    if date_end:
        args["date_end"] = date_end
    r = client.execute_tool("get_task_count_by_assignee", args)
    client.check_error(r, "get_task_count_by_assignee")
    return r


def get_task_count_by_organization(scope_type="my", date_start=None, date_end=None) -> dict:
    args = {"scope_type": scope_type}
    if date_start:
        args["date_start"] = date_start
    if date_end:
        args["date_end"] = date_end
    r = client.execute_tool("get_task_count_by_organization", args)
    client.check_error(r, "get_task_count_by_organization")
    return r


def get_task_priority_chart(date_start=None, date_end=None) -> dict:
    args = {}
    if date_start:
        args["date_start"] = date_start
    if date_end:
        args["date_end"] = date_end
    r = client.execute_tool("get_task_priority_chart", args)
    client.check_error(r, "get_task_priority_chart")
    return r


def get_overdue_tasks(list_task_id: str, date_start=None, date_end=None) -> dict:
    args = {"list_task_id": list_task_id}
    if date_start:
        args["date_start"] = date_start
    if date_end:
        args["date_end"] = date_end
    r = client.execute_tool("get_overdue_tasks", args)
    client.check_error(r, "get_overdue_tasks")
    return r


def get_task_completion_trends(date_start: str, date_end: str, period_type: str) -> dict:
    r = client.execute_tool("get_task_completion_trends", {
        "date_start": date_start,
        "date_end": date_end,
        "period_type": period_type,
    })
    client.check_error(r, "get_task_completion_trends")
    return r


def get_task_finish_rates(is_department=None, date_start=None, date_end=None) -> dict:
    args = {}
    if is_department:
        args["is_department"] = is_department
    if date_start:
        args["date_start"] = date_start
    if date_end:
        args["date_end"] = date_end
    r = client.execute_tool("get_task_finish_rates", args)
    client.check_error(r, "get_task_finish_rates")
    return r


def get_unassigned_task_count(list_task_id=None, date_start=None, date_end=None) -> dict:
    args = {}
    if list_task_id:
        args["list_task_id"] = list_task_id
    if date_start:
        args["date_start"] = date_start
    if date_end:
        args["date_end"] = date_end
    r = client.execute_tool("get_unassigned_task_count", args)
    client.check_error(r, "get_unassigned_task_count")
    return r


def get_recent_task_history(scope_type="my", page=0, size=20) -> dict:
    r = client.execute_tool("get_recent_task_history", {
        "scope_type": scope_type, "page": page, "size": size
    })
    client.check_error(r, "get_recent_task_history")
    return r


# ── Dashboard summaries & drill-down (added for backend Phase 6 / scoring update) ──

def _opt(args: dict, **kv) -> dict:
    for k, v in kv.items():
        if v is not None:
            args[k] = v
    return args


def get_my_dashboard_summary(date_start=None, date_end=None, day_left=3) -> dict:
    r = client.execute_tool("get_my_dashboard_summary",
                            _opt({"day_left": day_left}, date_start=date_start, date_end=date_end))
    client.check_error(r, "get_my_dashboard_summary")
    return r


def get_org_dashboard_summary(org=None, date_start=None, date_end=None) -> dict:
    r = client.execute_tool("get_org_dashboard_summary",
                            _opt({}, org=org, date_start=date_start, date_end=date_end))
    client.check_error(r, "get_org_dashboard_summary")
    return r


def get_user_dashboard_summary(user_id: int, org=None, date_start=None, date_end=None, day_left=3) -> dict:
    r = client.execute_tool("get_user_dashboard_summary",
                            _opt({"user_id": user_id, "day_left": day_left},
                                 org=org, date_start=date_start, date_end=date_end))
    client.check_error(r, "get_user_dashboard_summary")
    return r


def get_project_dashboard_summary(project_id: str, date_start=None, date_end=None, day_left=3) -> dict:
    r = client.execute_tool("get_project_dashboard_summary",
                            _opt({"project_id": project_id, "day_left": day_left},
                                 date_start=date_start, date_end=date_end))
    client.check_error(r, "get_project_dashboard_summary")
    return r


def get_org_workload(org=None, q=None, date_start=None, date_end=None, page=0, size=20) -> dict:
    r = client.execute_tool("get_org_workload",
                            _opt({"page": page, "size": size},
                                 org=org, q=q, date_start=date_start, date_end=date_end))
    client.check_error(r, "get_org_workload")
    return r


def get_tasks_by_metric(scope: str, metric: str, ref_id=None, org=None,
                        date_start=None, date_end=None, day_left=3, page=0, size=20) -> dict:
    r = client.execute_tool("get_tasks_by_metric",
                            _opt({"scope": scope, "metric": metric, "day_left": day_left,
                                  "page": page, "size": size},
                                 ref_id=ref_id, org=org, date_start=date_start, date_end=date_end))
    client.check_error(r, "get_tasks_by_metric")
    return r


def get_recent_tasks(scope_type="my", size=3, org=None, date_start=None, date_end=None, day_left=None) -> dict:
    r = client.execute_tool("get_recent_tasks",
                            _opt({"scope_type": scope_type, "size": size},
                                 org=org, date_start=date_start, date_end=date_end, day_left=day_left))
    client.check_error(r, "get_recent_tasks")
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

    if cmd == "stats":
        client.print_json(get_task_statistics())

    elif cmd == "by-status":
        parser = argparse.ArgumentParser(prog="analytics.py by-status")
        parser.add_argument("--scope", default="my", choices=["my", "org"])
        parser.add_argument("--org-prefix", default=None)
        parser.add_argument("--start", dest="date_start", default=None)
        parser.add_argument("--end", dest="date_end", default=None)
        args = parser.parse_args(sys.argv[2:])
        client.print_json(get_task_count_by_status(args.scope, args.org_prefix, args.date_start, args.date_end))

    elif cmd == "by-assignee":
        parser = argparse.ArgumentParser(prog="analytics.py by-assignee")
        parser.add_argument("--list", dest="list_task_id", default=None)
        parser.add_argument("--start", dest="date_start", default=None)
        parser.add_argument("--end", dest="date_end", default=None)
        args = parser.parse_args(sys.argv[2:])
        client.print_json(get_task_count_by_assignee(args.list_task_id, args.date_start, args.date_end))

    elif cmd == "by-org":
        parser = argparse.ArgumentParser(prog="analytics.py by-org")
        parser.add_argument("--scope", default="my", choices=["my", "org"])
        parser.add_argument("--start", dest="date_start", default=None)
        parser.add_argument("--end", dest="date_end", default=None)
        args = parser.parse_args(sys.argv[2:])
        client.print_json(get_task_count_by_organization(args.scope, args.date_start, args.date_end))

    elif cmd == "by-priority":
        parser = argparse.ArgumentParser(prog="analytics.py by-priority")
        parser.add_argument("--start", dest="date_start", default=None)
        parser.add_argument("--end", dest="date_end", default=None)
        args = parser.parse_args(sys.argv[2:])
        client.print_json(get_task_priority_chart(args.date_start, args.date_end))

    elif cmd == "overdue":
        parser = argparse.ArgumentParser(prog="analytics.py overdue")
        parser.add_argument("list_task_id")
        parser.add_argument("--start", dest="date_start", default=None)
        parser.add_argument("--end", dest="date_end", default=None)
        args = parser.parse_args(sys.argv[2:])
        client.print_json(get_overdue_tasks(args.list_task_id, args.date_start, args.date_end))

    elif cmd == "trends":
        parser = argparse.ArgumentParser(prog="analytics.py trends")
        parser.add_argument("--start", dest="date_start", required=True)
        parser.add_argument("--end", dest="date_end", required=True)
        parser.add_argument("--period", dest="period_type", required=True, choices=["day", "week", "month"])
        args = parser.parse_args(sys.argv[2:])
        client.print_json(get_task_completion_trends(args.date_start, args.date_end, args.period_type))

    elif cmd == "finish-rates":
        parser = argparse.ArgumentParser(prog="analytics.py finish-rates")
        parser.add_argument("--dept", dest="is_department", default=None)
        parser.add_argument("--start", dest="date_start", default=None)
        parser.add_argument("--end", dest="date_end", default=None)
        args = parser.parse_args(sys.argv[2:])
        client.print_json(get_task_finish_rates(args.is_department, args.date_start, args.date_end))

    elif cmd == "unassigned":
        parser = argparse.ArgumentParser(prog="analytics.py unassigned")
        parser.add_argument("--list", dest="list_task_id", default=None)
        parser.add_argument("--start", dest="date_start", default=None)
        parser.add_argument("--end", dest="date_end", default=None)
        args = parser.parse_args(sys.argv[2:])
        client.print_json(get_unassigned_task_count(args.list_task_id, args.date_start, args.date_end))

    elif cmd == "history":
        parser = argparse.ArgumentParser(prog="analytics.py history")
        parser.add_argument("--scope", default="my", choices=["my", "org"])
        parser.add_argument("--page", type=int, default=0)
        parser.add_argument("--size", type=int, default=20)
        args = parser.parse_args(sys.argv[2:])
        client.print_json(get_recent_task_history(args.scope, args.page, args.size))

    elif cmd == "my-dashboard":
        parser = argparse.ArgumentParser(prog="analytics.py my-dashboard")
        parser.add_argument("--start", dest="date_start", default=None)
        parser.add_argument("--end", dest="date_end", default=None)
        parser.add_argument("--day-left", type=int, default=3)
        args = parser.parse_args(sys.argv[2:])
        client.print_json(get_my_dashboard_summary(args.date_start, args.date_end, args.day_left))

    elif cmd == "org-dashboard":
        parser = argparse.ArgumentParser(prog="analytics.py org-dashboard")
        parser.add_argument("--org", default=None)
        parser.add_argument("--start", dest="date_start", default=None)
        parser.add_argument("--end", dest="date_end", default=None)
        args = parser.parse_args(sys.argv[2:])
        client.print_json(get_org_dashboard_summary(args.org, args.date_start, args.date_end))

    elif cmd == "user-dashboard":
        parser = argparse.ArgumentParser(prog="analytics.py user-dashboard")
        parser.add_argument("user_id", type=int)
        parser.add_argument("--org", default=None)
        parser.add_argument("--start", dest="date_start", default=None)
        parser.add_argument("--end", dest="date_end", default=None)
        parser.add_argument("--day-left", type=int, default=3)
        args = parser.parse_args(sys.argv[2:])
        client.print_json(get_user_dashboard_summary(args.user_id, args.org, args.date_start, args.date_end, args.day_left))

    elif cmd == "project-dashboard":
        parser = argparse.ArgumentParser(prog="analytics.py project-dashboard")
        parser.add_argument("project_id")
        parser.add_argument("--start", dest="date_start", default=None)
        parser.add_argument("--end", dest="date_end", default=None)
        parser.add_argument("--day-left", type=int, default=3)
        args = parser.parse_args(sys.argv[2:])
        client.print_json(get_project_dashboard_summary(args.project_id, args.date_start, args.date_end, args.day_left))

    elif cmd == "workload":
        parser = argparse.ArgumentParser(prog="analytics.py workload")
        parser.add_argument("--org", default=None)
        parser.add_argument("--q", default=None)
        parser.add_argument("--start", dest="date_start", default=None)
        parser.add_argument("--end", dest="date_end", default=None)
        parser.add_argument("--page", type=int, default=0)
        parser.add_argument("--size", type=int, default=20)
        args = parser.parse_args(sys.argv[2:])
        client.print_json(get_org_workload(args.org, args.q, args.date_start, args.date_end, args.page, args.size))

    elif cmd == "by-metric":
        parser = argparse.ArgumentParser(prog="analytics.py by-metric")
        parser.add_argument("--scope", required=True, choices=["my", "org", "user", "project"])
        parser.add_argument("--metric", required=True,
                            choices=["total", "overdue", "expiringSoon", "urgent", "completed", "inProgress"])
        parser.add_argument("--ref-id", dest="ref_id", default=None, help="userId or projectId for scope=user|project")
        parser.add_argument("--org", default=None)
        parser.add_argument("--start", dest="date_start", default=None)
        parser.add_argument("--end", dest="date_end", default=None)
        parser.add_argument("--day-left", type=int, default=3)
        parser.add_argument("--page", type=int, default=0)
        parser.add_argument("--size", type=int, default=20)
        args = parser.parse_args(sys.argv[2:])
        client.print_json(get_tasks_by_metric(args.scope, args.metric, args.ref_id, args.org,
                                               args.date_start, args.date_end, args.day_left, args.page, args.size))

    elif cmd == "recent":
        parser = argparse.ArgumentParser(prog="analytics.py recent")
        parser.add_argument("--scope", dest="scope_type", default="my", choices=["my", "org"])
        parser.add_argument("--size", type=int, default=3)
        parser.add_argument("--org", default=None)
        parser.add_argument("--start", dest="date_start", default=None)
        parser.add_argument("--end", dest="date_end", default=None)
        parser.add_argument("--day-left", dest="day_left", type=int, default=None)
        args = parser.parse_args(sys.argv[2:])
        client.print_json(get_recent_tasks(args.scope_type, args.size, args.org,
                                            args.date_start, args.date_end, args.day_left))

    else:
        print(f"Unknown command: {cmd}\n", file=sys.stderr)
        print(__doc__)
        sys.exit(1)
