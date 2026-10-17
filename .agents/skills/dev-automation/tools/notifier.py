#!/usr/bin/env python3
"""Notification service via Azure DevOps Work Item comments.

Sends formatted status updates to Azure DevOps work items so team members
(testers, PMs) can track AI agent progress.

Usage:
    python notifier.py started <work_item_id> <branch_name>
    python notifier.py mr-created <work_item_id> <mr_url>
    python notifier.py review-done <work_item_id> <mr_url> <summary>
    python notifier.py deploy-done <work_item_id> <env_url>
    python notifier.py custom <work_item_id> "Custom message here"
"""

import json
import sys
from datetime import datetime, timezone

import azure_devops

AGENT_TAG = "[AI Agent]"


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def notify_task_started(item_id: int, branch_name: str) -> dict:
    """Notify that the AI agent has started working on a task."""
    msg = (
        f"<b>{AGENT_TAG} Task Started</b><br/>"
        f"<b>Branch:</b> <code>{branch_name}</code><br/>"
        f"<b>Time:</b> {_timestamp()}<br/>"
        f"<br/>"
        f"AI Agent is analyzing the requirements and implementing the solution. "
        f"Updates will be posted as progress is made."
    )
    return azure_devops.add_comment(item_id, msg)


def notify_mr_created(item_id: int, mr_url: str,
                      mr_title: str = "") -> dict:
    """Notify that a merge request has been created."""
    title_line = f"<b>Title:</b> {mr_title}<br/>" if mr_title else ""
    msg = (
        f"<b>{AGENT_TAG} Merge Request Created</b><br/>"
        f"{title_line}"
        f"<b>MR Link:</b> <a href=\"{mr_url}\">{mr_url}</a><br/>"
        f"<b>Time:</b> {_timestamp()}<br/>"
        f"<br/>"
        f"Code changes are ready for review. Please check the merge request."
    )
    return azure_devops.add_comment(item_id, msg)


def notify_review_done(item_id: int, mr_url: str,
                       summary: str = "") -> dict:
    """Notify that code review has been completed by the AI agent."""
    summary_line = f"<b>Summary:</b><br/>{summary}<br/>" if summary else ""
    msg = (
        f"<b>{AGENT_TAG} Code Review Completed</b><br/>"
        f"<b>MR Link:</b> <a href=\"{mr_url}\">{mr_url}</a><br/>"
        f"<b>Time:</b> {_timestamp()}<br/>"
        f"<br/>"
        f"{summary_line}"
        f"Review comments have been posted on the merge request."
    )
    return azure_devops.add_comment(item_id, msg)


def notify_deploy_done(item_id: int, env_url: str = "",
                       mr_url: str = "") -> dict:
    """Notify testers that deployment to dev is complete."""
    env_line = f"<b>Dev URL:</b> <a href=\"{env_url}\">{env_url}</a><br/>" if env_url else ""
    mr_line = f"<b>MR Link:</b> <a href=\"{mr_url}\">{mr_url}</a><br/>" if mr_url else ""
    msg = (
        f"<b>{AGENT_TAG} Deployed to Dev Environment</b><br/>"
        f"{env_line}"
        f"{mr_line}"
        f"<b>Time:</b> {_timestamp()}<br/>"
        f"<br/>"
        f"The changes have been deployed to the dev environment. "
        f"<b>@Testers:</b> Please verify the implementation against the acceptance criteria."
    )
    return azure_devops.add_comment(item_id, msg)


def notify_custom(item_id: int, message: str) -> dict:
    """Post a custom notification message."""
    msg = (
        f"<b>{AGENT_TAG} Update</b><br/>"
        f"<b>Time:</b> {_timestamp()}<br/>"
        f"<br/>"
        f"{message}"
    )
    return azure_devops.add_comment(item_id, msg)


def _print_json(data):
    output = json.dumps(data, indent=2, ensure_ascii=False)
    sys.stdout.buffer.write(output.encode("utf-8"))
    sys.stdout.buffer.write(b"\n")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1].lower()
    wid = int(sys.argv[2])

    if cmd == "started" and len(sys.argv) >= 4:
        _print_json(notify_task_started(wid, sys.argv[3]))
    elif cmd == "mr-created" and len(sys.argv) >= 4:
        _print_json(notify_mr_created(wid, sys.argv[3]))
    elif cmd == "review-done" and len(sys.argv) >= 4:
        summary = sys.argv[4] if len(sys.argv) >= 5 else ""
        _print_json(notify_review_done(wid, sys.argv[3], summary))
    elif cmd == "deploy-done":
        env_url = sys.argv[3] if len(sys.argv) >= 4 else ""
        _print_json(notify_deploy_done(wid, env_url))
    elif cmd == "custom" and len(sys.argv) >= 4:
        _print_json(notify_custom(wid, sys.argv[3]))
    else:
        print(__doc__)
        sys.exit(1)
