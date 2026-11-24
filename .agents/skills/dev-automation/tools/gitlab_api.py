#!/usr/bin/env python3
"""GitLab Self-Hosted API client.

Provides functions to manage branches, merge requests, code review comments,
and retrieve diffs. Uses only Python stdlib (urllib + json).

Usage:
    python gitlab_api.py branches [project_id]
    python gitlab_api.py create-branch <branch_name> [ref]
    python gitlab_api.py create-mr <source_branch> <title> [target_branch]
    python gitlab_api.py mr-changes <mr_iid>
    python gitlab_api.py mr-comment <mr_iid> "Comment body"
    python gitlab_api.py merge-mr <mr_iid> [--keep-source] [--squash]   # [WRITE] accept/merge MR
    python gitlab_api.py list-mrs [state]
    python gitlab_api.py mr-discussions <mr_iid>
    python gitlab_api.py mr-detail <mr_iid>
    python gitlab_api.py whoami
    python gitlab_api.py my-review-mrs [reviewer|assignee|both]
"""

import json
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

import config


def _ssl_context() -> ssl.SSLContext:
    if config.get("SSL_VERIFY", "true").lower() in ("false", "0", "no"):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    return ssl.create_default_context()


def _project_id() -> str:
    return config.get("GITLAB_PROJECT_ID")


def _request(url: str, method: str = "GET", data: dict | None = None) -> dict | list:
    """Send an HTTP request to GitLab REST API v4."""
    token = config.get("GITLAB_PRIVATE_TOKEN")
    headers = {
        "PRIVATE-TOKEN": token,
        "Content-Type": "application/json",
    }
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, context=_ssl_context()) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        error_body = e.read().decode() if e.fp else ""
        return {"error": True, "status": e.code, "message": error_body}


def _api(path: str) -> str:
    base = config.gitlab_base_url()
    pid = urllib.parse.quote(str(_project_id()), safe="")
    return f"{base}/projects/{pid}{path}"


def create_branch(branch_name: str, ref: str = "main") -> dict:
    """Create a new branch from a given ref (default: main)."""
    url = _api("/repository/branches")
    return _request(url, method="POST", data={
        "branch": branch_name,
        "ref": ref,
    })


def delete_branch(branch_name: str) -> dict:
    """Delete a branch."""
    encoded = urllib.parse.quote(branch_name, safe="")
    url = _api(f"/repository/branches/{encoded}")
    return _request(url, method="DELETE")


def create_merge_request(source_branch: str, title: str,
                         target_branch: str = "develop",
                         description: str = "") -> dict:
    """Create a new merge request."""
    url = _api("/merge_requests")
    return _request(url, method="POST", data={
        "source_branch": source_branch,
        "target_branch": target_branch,
        "title": title,
        "description": description,
        "remove_source_branch": True,
    })


def get_merge_request(mr_iid: int) -> dict:
    """Get merge request details."""
    url = _api(f"/merge_requests/{mr_iid}")
    return _request(url)


def get_merge_request_changes(mr_iid: int) -> dict:
    """Get the diff/changes of a merge request for code review."""
    url = _api(f"/merge_requests/{mr_iid}/changes")
    result = _request(url)
    if isinstance(result, dict) and result.get("error"):
        return result

    changes = result.get("changes", [])
    return {
        "mr_iid": mr_iid,
        "title": result.get("title", ""),
        "description": result.get("description", ""),
        "source_branch": result.get("source_branch", ""),
        "target_branch": result.get("target_branch", ""),
        "state": result.get("state", ""),
        "changes_count": len(changes),
        "changes": [
            {
                "old_path": c.get("old_path", ""),
                "new_path": c.get("new_path", ""),
                "new_file": c.get("new_file", False),
                "deleted_file": c.get("deleted_file", False),
                "renamed_file": c.get("renamed_file", False),
                "diff": c.get("diff", ""),
            }
            for c in changes
        ],
    }


def accept_merge_request(mr_iid: int, remove_source: bool = True,
                         squash: bool = False) -> dict:
    """[WRITE] Accept (merge) a merge request. Returns the MR object — `state`
    becomes 'merged' on success, else GitLab returns an error (e.g. 405 if not
    mergeable: conflicts, pipeline not passed, needs approval)."""
    url = _api(f"/merge_requests/{mr_iid}/merge")
    data = {"should_remove_source_branch": remove_source}
    if squash:
        data["squash"] = True
    return _request(url, method="PUT", data=data)


def add_mr_comment(mr_iid: int, body: str) -> dict:
    """Add a general note/comment to a merge request."""
    url = _api(f"/merge_requests/{mr_iid}/notes")
    return _request(url, method="POST", data={"body": body})


def add_mr_inline_comment(mr_iid: int, body: str, new_path: str,
                          new_line: int, old_path: str = "",
                          old_line: int | None = None) -> dict:
    """Add an inline diff comment on a specific line of a merge request."""
    url = _api(f"/merge_requests/{mr_iid}/discussions")
    position = {
        "position_type": "text",
        "new_path": new_path,
        "new_line": new_line,
        "base_sha": "",
        "head_sha": "",
        "start_sha": "",
    }
    if old_path:
        position["old_path"] = old_path
    if old_line is not None:
        position["old_line"] = old_line

    mr = get_merge_request(mr_iid)
    if isinstance(mr, dict) and not mr.get("error"):
        diff_refs = mr.get("diff_refs", {})
        if diff_refs:
            position["base_sha"] = diff_refs.get("base_sha", "")
            position["head_sha"] = diff_refs.get("head_sha", "")
            position["start_sha"] = diff_refs.get("start_sha", "")

    return _request(url, method="POST", data={
        "body": body,
        "position": position,
    })


def list_open_merge_requests(state: str = "opened") -> list[dict]:
    """List merge requests by state (opened, closed, merged, all)."""
    url = _api(f"/merge_requests?state={state}&order_by=updated_at&sort=desc")
    result = _request(url)
    if isinstance(result, dict) and result.get("error"):
        return [result]

    return [
        {
            "iid": mr.get("iid"),
            "title": mr.get("title", ""),
            "state": mr.get("state", ""),
            "source_branch": mr.get("source_branch", ""),
            "target_branch": mr.get("target_branch", ""),
            "author": mr.get("author", {}).get("name", ""),
            "web_url": mr.get("web_url", ""),
            "created_at": mr.get("created_at", ""),
            "updated_at": mr.get("updated_at", ""),
        }
        for mr in (result if isinstance(result, list) else [])
    ]


def current_user() -> dict:
    """Get the authenticated GitLab user (id, username, name)."""
    return _request(f"{config.gitlab_base_url()}/user")


def list_review_merge_requests(username: str, who: str = "reviewer",
                               state: str = "opened") -> list[dict]:
    """List opened MRs across ALL accessible projects where the user is tagged.

    who = 'reviewer' | 'assignee' | 'both'. Uses the global /merge_requests
    endpoint with reviewer_username / assignee_username filters.
    """
    base = config.gitlab_base_url()
    common = f"state={state}&order_by=updated_at&sort=desc&per_page=50&scope=all"
    queries = []
    if who in ("reviewer", "both"):
        queries.append(f"reviewer_username={urllib.parse.quote(username)}")
    if who in ("assignee", "both"):
        queries.append(f"assignee_username={urllib.parse.quote(username)}")

    merged, seen = [], set()
    for q in queries:
        result = _request(f"{base}/merge_requests?{common}&{q}")
        if isinstance(result, dict) and result.get("error"):
            return [result]
        for mr in (result if isinstance(result, list) else []):
            key = (mr.get("project_id"), mr.get("iid"))
            if key in seen:
                continue
            seen.add(key)
            merged.append({
                "project_id": mr.get("project_id"),
                "iid": mr.get("iid"),
                "title": mr.get("title", ""),
                "sha": mr.get("sha", ""),
                "state": mr.get("state", ""),
                "author": mr.get("author", {}).get("name", ""),
                "source_branch": mr.get("source_branch", ""),
                "target_branch": mr.get("target_branch", ""),
                "web_url": mr.get("web_url", ""),
                "updated_at": mr.get("updated_at", ""),
                "draft": mr.get("draft", mr.get("work_in_progress", False)),
            })
    return merged


def get_mr_discussions(mr_iid: int) -> list[dict]:
    """Get all discussion threads on a merge request."""
    url = _api(f"/merge_requests/{mr_iid}/discussions")
    result = _request(url)
    if isinstance(result, dict) and result.get("error"):
        return [result]

    discussions = []
    for d in (result if isinstance(result, list) else []):
        notes = d.get("notes", [])
        discussions.append({
            "id": d.get("id", ""),
            "resolved": all(n.get("resolved", True) for n in notes),
            "notes": [
                {
                    "author": n.get("author", {}).get("name", ""),
                    "body": n.get("body", ""),
                    "created_at": n.get("created_at", ""),
                    "resolved": n.get("resolved", False),
                }
                for n in notes
            ],
        })
    return discussions


def get_file_content(file_path: str, ref: str = "main") -> str:
    """Get raw file content from a specific branch."""
    encoded_path = urllib.parse.quote(file_path, safe="")
    url = _api(f"/repository/files/{encoded_path}/raw?ref={ref}")
    token = config.get("GITLAB_PRIVATE_TOKEN")
    headers = {"PRIVATE-TOKEN": token}
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, context=_ssl_context()) as resp:
            return resp.read().decode()
    except urllib.error.HTTPError as e:
        return f"ERROR {e.code}: {e.read().decode() if e.fp else ''}"


def _print_json(data):
    output = json.dumps(data, indent=2, ensure_ascii=False)
    sys.stdout.buffer.write(output.encode("utf-8"))
    sys.stdout.buffer.write(b"\n")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1].lower()

    if cmd == "create-branch" and len(sys.argv) >= 3:
        ref = sys.argv[3] if len(sys.argv) >= 4 else "main"
        _print_json(create_branch(sys.argv[2], ref))

    elif cmd == "create-mr" and len(sys.argv) >= 4:
        target = sys.argv[4] if len(sys.argv) >= 5 else "develop"
        _print_json(create_merge_request(sys.argv[2], sys.argv[3], target))

    elif cmd == "mr-changes" and len(sys.argv) >= 3:
        _print_json(get_merge_request_changes(int(sys.argv[2])))

    elif cmd == "mr-comment" and len(sys.argv) >= 4:
        _print_json(add_mr_comment(int(sys.argv[2]), sys.argv[3]))

    elif cmd == "merge-mr" and len(sys.argv) >= 3:
        remove_source = "--keep-source" not in sys.argv
        squash = "--squash" in sys.argv
        _print_json(accept_merge_request(int(sys.argv[2]), remove_source, squash))

    elif cmd == "list-mrs":
        state = sys.argv[2] if len(sys.argv) >= 3 else "opened"
        _print_json(list_open_merge_requests(state))

    elif cmd == "whoami":
        _print_json(current_user())

    elif cmd == "my-review-mrs":
        who = sys.argv[2] if len(sys.argv) >= 3 else "reviewer"
        u = current_user()
        if isinstance(u, dict) and u.get("error"):
            _print_json(u)
        else:
            _print_json(list_review_merge_requests(u.get("username", ""), who))

    elif cmd == "mr-discussions" and len(sys.argv) >= 3:
        _print_json(get_mr_discussions(int(sys.argv[2])))

    elif cmd == "mr-detail" and len(sys.argv) >= 3:
        _print_json(get_merge_request(int(sys.argv[2])))

    else:
        print(__doc__)
        sys.exit(1)
