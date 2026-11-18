#!/usr/bin/env python3
"""Azure DevOps Work Items API client.

Provides functions to read tasks, update state, add comments, assign users,
and query assigned work items. Uses only Python stdlib (urllib + json).

Usage:
    python azure_devops.py get <work_item_id>
    python azure_devops.py comment <work_item_id> "Your comment here"
    python azure_devops.py state <work_item_id> <New|Active|Resolved|Closed>
    python azure_devops.py assign <work_item_id> <user_email_or_name>
    python azure_devops.py list [assigned_to_email]
"""

import json
import ssl
import sys
import urllib.error
import urllib.request
from base64 import b64encode

import config


def _ssl_context() -> ssl.SSLContext:
    if config.get("SSL_VERIFY", "true").lower() in ("false", "0", "no"):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    return ssl.create_default_context()


def _request(url: str, method: str = "GET", data: dict | list | None = None,
             content_type: str = "application/json") -> dict:
    """Send an HTTP request to Azure DevOps REST API."""
    pat = config.get("AZURE_DEVOPS_PAT")
    token = b64encode(f":{pat}".encode()).decode()
    headers = {
        "Authorization": f"Basic {token}",
        "Content-Type": content_type,
    }
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, context=_ssl_context()) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        error_body = e.read().decode() if e.fp else ""
        return {"error": True, "status": e.code, "message": error_body}


def get_work_item(item_id: int) -> dict:
    """Fetch full details of a work item by ID.

    Returns dict with keys: id, title, description, acceptance_criteria,
    state, assigned_to, work_item_type, tags, url
    """
    base = config.azure_base_url()
    url = f"{base}/wit/workitems/{item_id}?$expand=all&api-version=7.0"
    raw = _request(url)
    if raw.get("error"):
        return raw

    fields = raw.get("fields", {})
    return {
        "id": raw.get("id"),
        "title": fields.get("System.Title", ""),
        "description": fields.get("System.Description", ""),
        "acceptance_criteria": fields.get("Microsoft.VSTS.Common.AcceptanceCriteria", ""),
        "root_cause": fields.get("Custom.0f4a02f2-15a9-4f35-a81d-63ba340900e5", ""),
        "solution": fields.get("Custom.371472b3-2345-4d6f-9f0f-502826a45d97", ""),
        "state": fields.get("System.State", ""),
        "assigned_to": fields.get("System.AssignedTo", {}).get("displayName", ""),
        "reviewed_by": fields.get("Microsoft.VSTS.Common.ReviewedBy", {}).get("displayName", "") if isinstance(fields.get("Microsoft.VSTS.Common.ReviewedBy"), dict) else "",
        "work_item_type": fields.get("System.WorkItemType", ""),
        "tags": fields.get("System.Tags", ""),
        "url": raw.get("_links", {}).get("html", {}).get("href", ""),
        "relations": [
            {
                "rel": r.get("rel", ""),
                "url": r.get("url", ""),
                "name": r.get("attributes", {}).get("name", ""),
            }
            for r in raw.get("relations", []) or []
        ],
    }


def update_work_item_state(item_id: int, state: str) -> dict:
    """Update the state of a work item (New, Active, Resolved, Closed)."""
    base = config.azure_base_url()
    url = f"{base}/wit/workitems/{item_id}?api-version=7.0"
    patch_doc = [
        {
            "op": "replace",
            "path": "/fields/System.State",
            "value": state,
        }
    ]
    return _request(url, method="PATCH", data=patch_doc,
                    content_type="application/json-patch+json")


def assign_work_item(item_id: int, assignee: str) -> dict:
    """Assign a work item to a user by email or display name.

    Args:
        item_id: Work item ID
        assignee: User email (e.g., 'user@domain.com') or display name

    Returns:
        dict with updated work item info or error
    """
    base = config.azure_base_url()
    url = f"{base}/wit/workitems/{item_id}?api-version=7.0"
    patch_doc = [
        {
            "op": "replace",
            "path": "/fields/System.AssignedTo",
            "value": assignee,
        }
    ]
    return _request(url, method="PATCH", data=patch_doc,
                    content_type="application/json-patch+json")


def add_comment(item_id: int, text: str) -> dict:
    """Add a comment to a work item."""
    base = config.azure_base_url()
    url = f"{base}/wit/workitems/{item_id}/comments?api-version=7.0-preview.4"
    return _request(url, method="POST", data={"text": text})


def list_my_work_items(assigned_to: str = "") -> list[dict]:
    """Query work items assigned to a user via WIQL.

    If assigned_to is empty, queries items assigned to the authenticated user (@Me).
    Returns a list of simplified work item dicts.
    """
    base = config.azure_base_url()
    url = f"{base}/wit/wiql?api-version=7.0"

    assignee = f"'{assigned_to}'" if assigned_to else "@Me"
    wiql = {
        "query": (
            f"SELECT [System.Id], [System.Title], [System.State], "
            f"[System.WorkItemType] "
            f"FROM WorkItems "
            f"WHERE [System.AssignedTo] = {assignee} "
            f"AND [System.State] <> 'Closed' "
            f"AND [System.State] <> 'Removed' "
            f"ORDER BY [System.ChangedDate] DESC"
        )
    }
    result = _request(url, method="POST", data=wiql)
    if result.get("error"):
        return [result]

    work_items = result.get("workItems", [])
    if not work_items:
        return []

    ids = ",".join(str(wi["id"]) for wi in work_items[:50])
    details_url = (
        f"{base}/wit/workitems?ids={ids}"
        f"&fields=System.Id,System.Title,System.State,System.WorkItemType"
        f"&api-version=7.0"
    )
    details = _request(details_url)
    if details.get("error"):
        return [details]

    return [
        {
            "id": wi.get("id"),
            "title": wi.get("fields", {}).get("System.Title", ""),
            "state": wi.get("fields", {}).get("System.State", ""),
            "type": wi.get("fields", {}).get("System.WorkItemType", ""),
        }
        for wi in details.get("value", [])
    ]


def _print_json(data):
    import sys
    output = json.dumps(data, indent=2, ensure_ascii=False)
    sys.stdout.buffer.write(output.encode("utf-8"))
    sys.stdout.buffer.write(b"\n")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1].lower()

    if cmd == "get" and len(sys.argv) >= 3:
        _print_json(get_work_item(int(sys.argv[2])))
    elif cmd == "comment" and len(sys.argv) >= 4:
        _print_json(add_comment(int(sys.argv[2]), sys.argv[3]))
    elif cmd == "state" and len(sys.argv) >= 4:
        _print_json(update_work_item_state(int(sys.argv[2]), sys.argv[3]))
    elif cmd == "assign" and len(sys.argv) >= 4:
        _print_json(assign_work_item(int(sys.argv[2]), sys.argv[3]))
    elif cmd == "list":
        assignee = sys.argv[2] if len(sys.argv) >= 3 else ""
        _print_json(list_my_work_items(assignee))
    else:
        print(__doc__)
        sys.exit(1)
