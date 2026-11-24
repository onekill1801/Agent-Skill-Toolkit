#!/usr/bin/env python3
"""HTTP API probe for the stack-verify toolkit.

Call an endpoint and assert on the response: status code, JSON fields
(JSONPath-lite), and substring containment. Optionally extract values to reuse
in later flow steps.

Zero external dependencies — Python stdlib (urllib) only.

Config (via .env / env, see config.py):
    API_BASE_URL        default base URL if --url is a path
    API_AUTH_HEADER     optional, e.g. "Authorization: Bearer xxx"
    SSL_VERIFY          true|false (shared with gitlab/azure)

Usage:
    python probe_api.py call --method GET --url /health --expect-status 200
    python probe_api.py call --method POST --url /users \
        --body '{"name":"A"}' --expect-status 201 \
        --expect-json '$.status=ACTIVE' --save 'uid=$.id'
    python probe_api.py call --url https://api.example.com/x --expect-contains '"ok":true'

Output: a single JSON object.
    {"passed": bool, "status": int, "checks": [...], "saved": {...}, "body": <parsed-or-text>}
"""

import argparse
import json
import ssl
import sys
import urllib.error
import urllib.request

import config
import project_config
import probe_common as pc


def _ssl_context() -> ssl.SSLContext:
    if config.get("SSL_VERIFY", "true").lower() in ("false", "0", "no"):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    return ssl.create_default_context()


def _full_url(url: str) -> str:
    if url.startswith("http://") or url.startswith("https://"):
        return url
    base = config.get("API_BASE_URL").rstrip("/")
    if not base:
        raise ValueError(f"relative url '{url}' but API_BASE_URL is not set")
    return base + "/" + url.lstrip("/")


def _parse_kv_list(items, sep="="):
    """['a=b','c=d'] -> [('a','b'), ('c','d')]. Splits on first sep only."""
    out = []
    for it in items or []:
        if sep not in it:
            raise ValueError(f"expected key{sep}value, got '{it}'")
        k, v = it.split(sep, 1)
        out.append((k.strip(), v.strip()))
    return out


def cmd_call(args) -> dict:
    url = _full_url(args.url)
    headers = {"Content-Type": "application/json"}
    auth = config.get("API_AUTH_HEADER")
    if auth and ":" in auth:
        k, v = auth.split(":", 1)
        headers[k.strip()] = v.strip()
    for k, v in _parse_kv_list(args.header, sep=":"):
        headers[k] = v

    data = args.body.encode() if args.body else None
    req = urllib.request.Request(url, data=data, headers=headers, method=args.method)

    status = None
    raw = ""
    try:
        with urllib.request.urlopen(req, context=_ssl_context(), timeout=args.timeout) as resp:
            status = resp.status
            raw = resp.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        status = e.code
        raw = e.read().decode(errors="replace") if e.fp else ""
    except (urllib.error.URLError, OSError) as e:
        return {"error": True, "passed": False, "url": url, "message": f"request failed: {e}"}

    try:
        parsed = json.loads(raw) if raw else None
    except json.JSONDecodeError:
        parsed = None

    checks = []
    saved = {}

    if args.expect_status is not None:
        ok = status == args.expect_status
        checks.append({"check": "status", "expected": args.expect_status, "actual": status, "passed": ok})

    for path, expected in _parse_kv_list(args.expect_json):
        try:
            actual = pc.jsonpath_get(parsed, path)
            ok = pc.match_value(actual, expected)
        except (KeyError, IndexError, TypeError):
            actual, ok = None, False
        checks.append({"check": f"json {path}", "expected": expected, "actual": actual, "passed": ok})

    for substr in args.expect_contains or []:
        ok = substr in raw
        checks.append({"check": "contains", "expected": substr, "passed": ok})

    for name, path in _parse_kv_list(args.save):
        try:
            saved[name] = pc.jsonpath_get(parsed, path)
        except (KeyError, IndexError, TypeError):
            checks.append({"check": f"save {name}", "expected": path, "actual": None, "passed": False})

    passed = all(c["passed"] for c in checks) if checks else (status is not None)
    return {
        "passed": passed,
        "status": status,
        "url": url,
        "checks": checks,
        "saved": saved,
        "body": parsed if parsed is not None else raw[:2000],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="HTTP API probe with assertions")
    sub = parser.add_subparsers(dest="action", required=True)
    p = sub.add_parser("call")
    p.add_argument("--method", default="GET")
    p.add_argument("--url", required=True, help="full URL or path (uses API_BASE_URL)")
    p.add_argument("--body", help="raw request body (usually JSON)")
    p.add_argument("--header", action="append", help="extra header 'Key: Value' (repeatable)")
    p.add_argument("--expect-status", type=int)
    p.add_argument("--expect-json", action="append", help="'$.path=value' (repeatable, '*'=any)")
    p.add_argument("--expect-contains", action="append", help="substring that must appear (repeatable)")
    p.add_argument("--save", action="append", help="'name=$.path' extract for later steps (repeatable)")
    p.add_argument("--project", help="load this project's stack config from ./work/projects.json")
    p.add_argument("--env", help="environment (local|dev|uat|sandbox|prod); default from project")
    p.add_argument("--allow-prod", action="store_true", help="permit a write request against a protected env")
    p.add_argument("--timeout", type=int, default=30)

    args = parser.parse_args()
    mutating = args.method.upper() not in ("GET", "HEAD", "OPTIONS")
    err = project_config.apply_args(args, mutating=mutating)
    if err:
        return pc.emit(err)
    try:
        out = cmd_call(args)
    except ValueError as e:
        out = {"error": True, "passed": False, "message": str(e)}
    return pc.emit(out)


if __name__ == "__main__":
    sys.exit(main())
