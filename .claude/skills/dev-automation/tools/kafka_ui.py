#!/usr/bin/env python3
"""Provectus Kafka UI client (read-only) for the stack-verify toolkit.

The target https://.../ runs Provectus **Kafka UI** (kafka-ui-api), NOT a Confluent
REST Proxy — so probe_kafka.py does not apply. This tool logs in with the LOGIN_FORM
flow (username/password -> session cookie) and reads clusters / topics / messages
through Kafka UI's own REST API.

Mostly read-only: list clusters, list/inspect topics, read recent messages, and
assert on them. `produce` is the one WRITE op (send a test message) — it is guarded
by the project_config prod-guard (refused against a protected env without --allow-prod),
so reads against any env are safe and writes against prod are blocked by default.

Config (via .env / env, see config.py):
    KAFKA_UI_URL          base URL, e.g. https://kafka-ui.example.com
    KAFKA_UI_USER         login username
    KAFKA_UI_PASSWORD     login password
    KAFKA_UI_LOGIN_PATH   form-login POST path (default /login)
    SSL_VERIFY            true|false

Usage:
    python kafka_ui.py clusters
    python kafka_ui.py topics --cluster <name>
    python kafka_ui.py topic --cluster <name> --topic <t>
    python kafka_ui.py messages --cluster <name> --topic <t> --limit 20 \
        --expect-contains '"uid":42' --expect-min-count 1
    python kafka_ui.py produce --cluster <name> --topic <t> --value '{"uid":42}' --key 42
    python kafka_ui.py login-check        # just verify auth works

Output: a single JSON object.
"""

import argparse
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

_OPENER = None
_JAR = http.cookiejar.CookieJar()


def _ssl_context() -> ssl.SSLContext:
    if config.get("SSL_VERIFY", "true").lower() in ("false", "0", "no"):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    return ssl.create_default_context()


def _opener():
    global _OPENER
    if _OPENER is None:
        _OPENER = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(_JAR),
            urllib.request.HTTPSHandler(context=_ssl_context()),
        )
    return _OPENER


def _xsrf_header() -> dict:
    """Echo an XSRF-TOKEN cookie back as a header if the server set one."""
    for c in _JAR:
        if c.name in ("XSRF-TOKEN", "X-XSRF-TOKEN"):
            return {"X-XSRF-TOKEN": c.value}
    return {}


def _base() -> str:
    url = config.get("KAFKA_UI_URL")
    if not url:
        raise ValueError("KAFKA_UI_URL is not set")
    return url.rstrip("/")


def _req(method, path, data=None, headers=None, timeout=30, stream=False):
    url = path if path.startswith("http") else _base() + path
    h = {"Accept": "application/json"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    resp = _opener().open(req, timeout=timeout)
    if stream:
        return resp  # caller reads the stream and closes
    with resp:
        body = resp.read().decode(errors="replace")
        return resp.status, body


def login() -> dict:
    """LOGIN_FORM: POST username/password form-encoded, keep the session cookie."""
    user = config.get("KAFKA_UI_USER")
    pwd = config.get("KAFKA_UI_PASSWORD")
    if not user or not pwd:
        return {"error": True, "message": "KAFKA_UI_USER / KAFKA_UI_PASSWORD not set"}
    path = config.get("KAFKA_UI_LOGIN_PATH", "/login")
    body = urllib.parse.urlencode({"username": user, "password": pwd}).encode()
    try:
        # Browser-like Accept: form login replies with a 302 redirect to an HTML
        # page; Accept: application/json would make that redirect target 406.
        status, _ = _req("POST", path, data=body,
                         headers={"Content-Type": "application/x-www-form-urlencoded",
                                  "Accept": "text/html,*/*"})
    except urllib.error.HTTPError as e:
        # Spring form-login redirects (302) on success; urllib follows it. A 401/403
        # or a redirect to ?error means bad creds.
        status = e.code
    except (urllib.error.URLError, OSError) as e:
        return {"error": True, "message": f"login request failed: {e}"}

    # Verify by hitting an authenticated API endpoint.
    try:
        s, b = _req("GET", "/api/clusters")
    except urllib.error.HTTPError as e:
        return {"error": True, "message": f"auth check failed: HTTP {e.code}", "login_status": status}
    except (urllib.error.URLError, OSError) as e:
        return {"error": True, "message": f"auth check failed: {e}"}
    try:
        clusters = json.loads(b)
        return {"ok": True, "login_status": status, "clusters": clusters}
    except json.JSONDecodeError:
        return {"error": True,
                "message": "login did not yield an authenticated session "
                           "(/api/clusters did not return JSON). Check creds / KAFKA_UI_LOGIN_PATH.",
                "login_status": status, "sample": b[:200]}


def _ensure_login():
    """Login and return the clusters list, or raise RuntimeError with a message."""
    res = login()
    if res.get("error"):
        raise RuntimeError(res["message"])
    return res.get("clusters", [])


def cmd_login_check(args) -> dict:
    res = login()
    if res.get("error"):
        return {"passed": False, **res}
    names = [c.get("name") for c in res.get("clusters", []) if isinstance(c, dict)]
    return {"passed": True, "authenticated": True, "clusters": names}


def cmd_clusters(args) -> dict:
    try:
        clusters = _ensure_login()
    except RuntimeError as e:
        return {"error": True, "passed": False, "message": str(e)}
    return {"passed": True, "count": len(clusters),
            "clusters": [{"name": c.get("name"), "status": c.get("status")}
                         for c in clusters if isinstance(c, dict)]}


def cmd_topics(args) -> dict:
    try:
        _ensure_login()
        s, b = _req("GET", f"/api/clusters/{urllib.parse.quote(args.cluster)}/topics")
    except (RuntimeError, urllib.error.HTTPError, urllib.error.URLError, OSError) as e:
        return {"error": True, "passed": False, "message": f"topics failed: {e}"}
    data = json.loads(b)
    topics = data.get("topics", data) if isinstance(data, dict) else data
    names = [t.get("name") for t in topics if isinstance(t, dict)]
    checks = []
    if args.expect_contains_topic:
        checks.append({"check": "topic exists", "expected": args.expect_contains_topic,
                       "passed": args.expect_contains_topic in names})
    passed = all(c["passed"] for c in checks) if checks else True
    return {"passed": passed, "cluster": args.cluster, "count": len(names),
            "topics": names[:100], "checks": checks}


def cmd_topic(args) -> dict:
    try:
        _ensure_login()
        s, b = _req("GET", f"/api/clusters/{urllib.parse.quote(args.cluster)}"
                          f"/topics/{urllib.parse.quote(args.topic)}")
    except (RuntimeError, urllib.error.HTTPError, urllib.error.URLError, OSError) as e:
        return {"error": True, "passed": False, "message": f"topic failed: {e}"}
    d = json.loads(b)
    return {"passed": True, "cluster": args.cluster, "topic": d.get("name"),
            "partitions": d.get("partitionCount"), "replication": d.get("replicationFactor"),
            "internal": d.get("internal")}


def cmd_messages(args) -> dict:
    """Read recent messages via the Kafka UI SSE stream (newest first)."""
    try:
        _ensure_login()
    except RuntimeError as e:
        return {"error": True, "passed": False, "message": str(e)}
    # latest = newest messages (read backward from end); beginning = oldest first.
    if args.from_ == "beginning":
        seek_dir, seek_type = "FORWARD", "BEGINNING"
    else:
        seek_dir, seek_type = "BACKWARD", "LATEST"
    q = urllib.parse.urlencode({
        "limit": args.limit, "seekDirection": seek_dir, "seekType": seek_type,
    })
    path = (f"/api/clusters/{urllib.parse.quote(args.cluster)}"
            f"/topics/{urllib.parse.quote(args.topic)}/messages?{q}")
    messages = []
    try:
        resp = _req("GET", path, headers={"Accept": "text/event-stream"},
                    timeout=args.timeout, stream=True)
    except (urllib.error.HTTPError, urllib.error.URLError, OSError) as e:
        return {"error": True, "passed": False, "message": f"messages stream failed: {e}"}

    deadline = time.time() + args.timeout
    try:
        for raw in resp:
            if time.time() > deadline:
                break
            line = raw.decode(errors="replace").strip()
            if not line.startswith("data:"):
                continue
            try:
                evt = json.loads(line[5:].strip())
            except json.JSONDecodeError:
                continue
            if evt.get("type") == "MESSAGE" and "message" in evt:
                messages.append(evt["message"])
                if len(messages) >= args.limit:
                    break
            elif evt.get("type") == "DONE":
                break
    finally:
        resp.close()

    def _content(m):
        c = m.get("content")
        return c if isinstance(c, str) else json.dumps(c, ensure_ascii=False, separators=(",", ":"))

    checks = []
    if args.expect_min_count is not None:
        checks.append({"check": "min_count", "expected": args.expect_min_count,
                       "actual": len(messages), "passed": len(messages) >= args.expect_min_count})
    if args.expect_contains:
        hit = any(args.expect_contains in _content(m) for m in messages)
        checks.append({"check": "contains", "expected": args.expect_contains, "passed": hit})
    passed = all(c["passed"] for c in checks) if checks else (len(messages) > 0)
    return {"passed": passed, "cluster": args.cluster, "topic": args.topic,
            "count": len(messages),
            "sample": [{"offset": m.get("offset"), "partition": m.get("partition"),
                        "key": m.get("key"), "content": _content(m)[:300]} for m in messages[:10]],
            "checks": checks}


def cmd_produce(args) -> dict:
    """WRITE: send one message to a topic via Kafka UI's create-message API."""
    try:
        _ensure_login()
    except RuntimeError as e:
        return {"error": True, "passed": False, "message": str(e)}
    payload = {
        "key": args.key,
        "content": args.value,
        "keySerde": args.key_serde,
        "valueSerde": args.value_serde,
        "partition": args.partition if args.partition is not None else 0,
        "headers": {},
    }
    path = (f"/api/clusters/{urllib.parse.quote(args.cluster)}"
            f"/topics/{urllib.parse.quote(args.topic)}/messages")
    headers = {"Content-Type": "application/json"}
    headers.update(_xsrf_header())
    try:
        status, body = _req("POST", path, data=json.dumps(payload).encode(), headers=headers)
    except urllib.error.HTTPError as e:
        b = e.read().decode(errors="replace") if e.fp else ""
        hint = " (CSRF? permissions?)" if e.code in (401, 403) else ""
        return {"error": True, "passed": False, "status": e.code,
                "message": f"produce failed: HTTP {e.code}{hint}", "body": b[:300]}
    except (urllib.error.URLError, OSError) as e:
        return {"error": True, "passed": False, "message": f"produce failed: {e}"}
    ok = status in (200, 201)
    return {"passed": ok, "status": status, "cluster": args.cluster, "topic": args.topic,
            "sent": {"key": args.key, "content": args.value}}


def main() -> int:
    parser = argparse.ArgumentParser(description="Provectus Kafka UI client (read + produce)")
    sub = parser.add_subparsers(dest="action", required=True)

    # Shared: pick a project/env so KAFKA_UI_URL (and creds) come from the registry.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--project", help="load this project's stack config from ./work/projects.json")
    common.add_argument("--env", help="environment (local|dev|uat|sandbox|prod); default from project")
    common.add_argument("--allow-prod", action="store_true", help="permit produce against a protected env")

    sub.add_parser("login-check", parents=[common])
    sub.add_parser("clusters", parents=[common])

    t = sub.add_parser("topics", parents=[common])
    t.add_argument("--cluster", required=True)
    t.add_argument("--expect-contains-topic", help="assert this topic name is present")

    td = sub.add_parser("topic", parents=[common])
    td.add_argument("--cluster", required=True)
    td.add_argument("--topic", required=True)

    m = sub.add_parser("messages", parents=[common])
    m.add_argument("--cluster", required=True)
    m.add_argument("--topic", required=True)
    m.add_argument("--limit", type=int, default=20)
    m.add_argument("--from", dest="from_", choices=["latest", "beginning"], default="latest",
                   help="latest = newest messages (default); beginning = oldest first")
    m.add_argument("--timeout", type=int, default=30)
    m.add_argument("--expect-contains")
    m.add_argument("--expect-min-count", type=int)

    pr = sub.add_parser("produce", parents=[common], help="WRITE: send one message to a topic")
    pr.add_argument("--cluster", required=True)
    pr.add_argument("--topic", required=True)
    pr.add_argument("--value", required=True, help="message content (string)")
    pr.add_argument("--key", default=None)
    pr.add_argument("--partition", type=int)
    pr.add_argument("--key-serde", default="String")
    pr.add_argument("--value-serde", default="String")

    args = parser.parse_args()
    err = project_config.apply_args(args, mutating=args.action == "produce")
    if err:
        return pc.emit(err)
    handlers = {
        "login-check": cmd_login_check, "clusters": cmd_clusters,
        "topics": cmd_topics, "topic": cmd_topic, "messages": cmd_messages,
        "produce": cmd_produce,
    }
    try:
        out = handlers[args.action](args)
    except ValueError as e:
        out = {"error": True, "passed": False, "message": str(e)}
    return pc.emit(out)


if __name__ == "__main__":
    sys.exit(main())
