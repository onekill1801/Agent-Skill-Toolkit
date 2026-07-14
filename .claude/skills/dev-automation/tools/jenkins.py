#!/usr/bin/env python3
"""Jenkins CI/CD client for the stack-verify toolkit.

Trigger a build, poll it to completion, and read the result + console tail —
over the Jenkins REST API (stdlib urllib + API-token basic auth, no pip).

Config (via .env / env, see config.py):
    JENKINS_URL      e.g. https://jenkins.internal
    JENKINS_USER     username
    JENKINS_TOKEN    API token (User > Configure > API Token)
    SSL_VERIFY       true|false

Job path: pass a simple job name with --job; for a job inside folders use the
full API path with --path, e.g. --path "job/team/job/my-service".

Usage:
    python jenkins.py build --job my-service --param BRANCH=develop --wait
    python jenkins.py trigger --job my-service --param ENV=dev
    python jenkins.py status --job my-service --number 42
    python jenkins.py console --job my-service --number 42 --tail 40
    python jenkins.py build --job my-service --dry-run

Output: a single JSON object.
"""

import argparse
import base64
import http.cookiejar
import json
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import config
import project_config
import probe_common as pc

CONSOLE_TAIL_LINES = 60


def _ssl_context() -> ssl.SSLContext:
    if config.get("SSL_VERIFY", "true").lower() in ("false", "0", "no"):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    return ssl.create_default_context()


def _base() -> str:
    url = config.get("JENKINS_URL")
    if not url:
        raise ValueError("JENKINS_URL is not set")
    return url.rstrip("/")


def _auth_header() -> dict:
    user = config.get("JENKINS_USER")
    token = config.get("JENKINS_TOKEN")
    if not user or not token:
        raise ValueError("JENKINS_USER / JENKINS_TOKEN not set")
    raw = f"{user}:{token}".encode()
    return {"Authorization": "Basic " + base64.b64encode(raw).decode()}


def _job_path(args) -> str:
    if getattr(args, "path", None):
        return args.path.strip("/")
    return f"job/{urllib.parse.quote(args.job)}"


# Shared opener with a cookie jar so the session cookie from /crumbIssuer is
# carried into the build POST (Jenkins crumbs are validated against the session).
_OPENER = None


def _opener():
    global _OPENER
    if _OPENER is None:
        _OPENER = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
            urllib.request.HTTPSHandler(context=_ssl_context()),
        )
    return _OPENER


def _req(method: str, url: str, data: bytes = None, extra_headers: dict = None):
    """Return (status, headers, body_text). Raises on network error."""
    headers = _auth_header()
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with _opener().open(req, timeout=30) as resp:
            return resp.status, dict(resp.headers), resp.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode(errors="replace") if e.fp else ""


def _crumb_header() -> dict:
    """Fetch a CSRF crumb if the instance issues one; {} if not required."""
    try:
        status, _, body = _req("GET", f"{_base()}/crumbIssuer/api/json")
    except (urllib.error.URLError, OSError):
        return {}
    if status == 200 and body:
        d = json.loads(body)
        field, crumb = d.get("crumbRequestField"), d.get("crumb")
        if field and crumb:
            return {field: crumb}
    return {}


def _trigger(args) -> dict:
    base = _base()
    jp = _job_path(args)
    params = dict(kv.split("=", 1) for kv in (args.param or []))
    if params:
        url = f"{base}/{jp}/buildWithParameters?" + urllib.parse.urlencode(params)
    else:
        url = f"{base}/{jp}/build"

    if args.dry_run:
        return {"dry_run": True, "method": "POST", "url": url, "params": params}

    status, headers, body = _req("POST", url, data=b"", extra_headers=_crumb_header())
    if status not in (200, 201, 302):
        hint = " (CSRF crumb may be required, or check token/permissions)" if status == 403 else ""
        return {"error": True, "passed": False, "status": status,
                "message": f"trigger failed: HTTP {status}{hint}", "body": body[:500]}
    return {"passed": True, "status": status, "queue_url": headers.get("Location"), "params": params}


def _wait_queue(queue_url: str, deadline: float):
    """Poll a queue item until it has an executable (build) number."""
    while time.time() < deadline:
        status, _, body = _req("GET", queue_url.rstrip("/") + "/api/json")
        if status == 200 and body:
            data = json.loads(body)
            execu = data.get("executable")
            if execu:
                return execu.get("number"), execu.get("url")
            if data.get("cancelled"):
                return None, None
        time.sleep(1)
    return None, None


def _build_status(args, number: int) -> dict:
    base = _base()
    jp = _job_path(args)
    status, _, body = _req("GET", f"{base}/{jp}/{number}/api/json")
    if status != 200 or not body:
        return {"error": True, "message": f"status fetch failed: HTTP {status}"}
    data = json.loads(body)
    return {"number": number, "building": data.get("building"), "result": data.get("result")}


def _console(args, number: int, tail: int) -> str:
    base = _base()
    jp = _job_path(args)
    _, _, body = _req("GET", f"{base}/{jp}/{number}/consoleText")
    lines = body.splitlines()
    return "\n".join(lines[-tail:]) if len(lines) > tail else body


def cmd_build(args) -> dict:
    trig = _trigger(args)
    if args.dry_run or trig.get("error") or not args.wait:
        return trig
    queue_url = trig.get("queue_url")
    if not queue_url:
        return {"error": True, "passed": False, "message": "no queue URL returned to wait on"}

    deadline = time.time() + args.timeout
    number, _ = _wait_queue(queue_url, deadline)
    if number is None:
        return {"error": True, "passed": False, "message": "build did not start before timeout (or cancelled)"}

    while time.time() < deadline:
        st = _build_status(args, number)
        if st.get("error"):
            return {"passed": False, **st}
        if st.get("building") is False:
            result = st.get("result")
            return {
                "passed": result == "SUCCESS",
                "number": number,
                "result": result,
                "console_tail": _console(args, number, CONSOLE_TAIL_LINES),
            }
        time.sleep(args.poll)
    return {"error": True, "passed": False, "number": number,
            "message": f"build #{number} did not finish before {args.timeout}s timeout"}


def cmd_info(args) -> dict:
    """Read-only: job summary + last/last-successful build numbers (no side effects)."""
    base = _base()
    jp = _job_path(args)
    status, _, body = _req("GET", f"{base}/{jp}/api/json")
    if status != 200 or not body:
        hint = " (check user/token or job path)" if status in (401, 403, 404) else ""
        return {"error": True, "passed": False, "status": status,
                "message": f"info failed: HTTP {status}{hint}", "body": body[:300]}
    d = json.loads(body)
    lb = d.get("lastBuild") or {}
    return {
        "passed": True,
        "name": d.get("fullDisplayName") or d.get("displayName") or d.get("name"),
        "buildable": d.get("buildable"),
        "color": d.get("color"),
        "url": d.get("url"),
        "last_build": lb.get("number"),
        "last_build_url": lb.get("url"),
        "last_completed_build": (d.get("lastCompletedBuild") or {}).get("number"),
        "last_successful_build": (d.get("lastSuccessfulBuild") or {}).get("number"),
        "last_failed_build": (d.get("lastFailedBuild") or {}).get("number"),
    }


def cmd_jobs(args) -> dict:
    """Read-only: list child jobs/folders under a path (root if none) — for discovery."""
    base = _base()
    if getattr(args, "path", None):
        jp = args.path.strip("/")
    elif getattr(args, "job", None):
        jp = "job/" + urllib.parse.quote(args.job)
    else:
        jp = ""
    url = f"{base}/{jp}/api/json?tree=jobs[name,url,_class]" if jp else f"{base}/api/json?tree=jobs[name,url,_class]"
    status, _, body = _req("GET", url)
    if status != 200 or not body:
        return {"error": True, "passed": False, "status": status, "message": f"jobs list failed: HTTP {status}"}
    data = json.loads(body)
    jobs = [{"name": j.get("name"), "url": j.get("url"),
             "is_folder": "Folder" in (j.get("_class") or "")} for j in data.get("jobs", [])]
    return {"passed": True, "path": jp or "(root)", "count": len(jobs), "jobs": jobs}


def main() -> int:
    parser = argparse.ArgumentParser(description="Jenkins CI/CD client")
    sub = parser.add_subparsers(dest="action", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--job", help="simple job name")
    common.add_argument("--path", help="full API path, e.g. job/folder/job/name")
    common.add_argument("--project", help="load this project's stack config from ./work/projects.json")
    common.add_argument("--env", help="environment (local|dev|uat|sandbox|prod); default from project")
    common.add_argument("--allow-prod", action="store_true", help="permit triggering a build against a protected env")

    t = sub.add_parser("trigger", parents=[common])
    t.add_argument("--param", action="append", help="build param key=value (repeatable)")
    t.add_argument("--dry-run", action="store_true")

    b = sub.add_parser("build", parents=[common])
    b.add_argument("--param", action="append")
    b.add_argument("--wait", action="store_true", help="wait for completion and report result")
    b.add_argument("--timeout", type=int, default=1800)
    b.add_argument("--poll", type=int, default=5)
    b.add_argument("--dry-run", action="store_true")

    s = sub.add_parser("status", parents=[common])
    s.add_argument("--number", type=int, required=True)

    c = sub.add_parser("console", parents=[common])
    c.add_argument("--number", type=int, required=True)
    c.add_argument("--tail", type=int, default=CONSOLE_TAIL_LINES)

    sub.add_parser("info", parents=[common])  # read-only job summary
    sub.add_parser("jobs", parents=[common])   # read-only: list child jobs/folders

    args = parser.parse_args()
    err = project_config.apply_args(args, mutating=args.action in ("trigger", "build"))
    if err:
        return pc.emit(err)
    try:
        # Fall back to the project's configured Jenkins job if none was given.
        if not getattr(args, "job", None) and not getattr(args, "path", None) and getattr(args, "project", None):
            args.path = project_config.default_jenkins_job(args.project, getattr(args, "env", None)) or None
        # 'jobs' can list the root (no job/path needed); others require a target.
        if args.action != "jobs" and not getattr(args, "job", None) and not getattr(args, "path", None):
            raise ValueError("provide --job or --path (or set stack.jenkins.job for --project)")
        if args.action == "trigger":
            out = _trigger(args)
        elif args.action == "build":
            out = cmd_build(args)
        elif args.action == "status":
            out = _build_status(args, args.number)
        elif args.action == "info":
            out = cmd_info(args)
        elif args.action == "jobs":
            out = cmd_jobs(args)
        else:
            out = {"number": args.number, "console_tail": _console(args, args.number, args.tail)}
    except ValueError as e:
        out = {"error": True, "passed": False, "message": str(e)}
    return pc.emit(out)


if __name__ == "__main__":
    sys.exit(main())
