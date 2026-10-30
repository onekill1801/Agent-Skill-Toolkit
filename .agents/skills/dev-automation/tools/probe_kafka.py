#!/usr/bin/env python3
"""Kafka probe for the stack-verify toolkit, via Confluent REST Proxy (v2 API).

Uses HTTP (stdlib urllib) — no broker client / pip driver. Lets a backend test
produce a message to a topic and assert that an expected message lands on a topic
within a timeout.

Config (via .env / env, see config.py):
    KAFKA_REST_URL       base URL of the Confluent REST Proxy, e.g. http://kafka-rest:8082
    KAFKA_REST_AUTH      optional header "Authorization: Basic ..."
    SSL_VERIFY           true|false

Usage:
    python probe_kafka.py produce --topic user.created --value '{"uid":42}' [--key 42]
    python probe_kafka.py consume --topic user.created --timeout 15 \
        --expect-contains '"uid":42'
    python probe_kafka.py consume --topic user.created --expect-json '$.uid=42' --min-count 1
    python probe_kafka.py produce --topic t --value '{}' --dry-run

Note on consume: a consumer instance is created, subscribed, polled until match or
timeout, then deleted. The REST Proxy's first poll usually returns empty (it
triggers partition assignment) — this tool polls repeatedly until the deadline.

Output: a single JSON object.
"""

import argparse
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request

import config
import project_config
import probe_common as pc

_JSON_V2 = "application/vnd.kafka.json.v2+json"


def _ssl_context() -> ssl.SSLContext:
    if config.get("SSL_VERIFY", "true").lower() in ("false", "0", "no"):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    return ssl.create_default_context()


def _base() -> str:
    url = config.get("KAFKA_REST_URL")
    if not url:
        raise ValueError("KAFKA_REST_URL is not set")
    return url.rstrip("/")


def _base_for(dry_run: bool) -> str:
    """Like _base(), but a --dry-run preview never needs a live config — it only
    prints the request it WOULD send, so an unset URL falls back to a placeholder
    instead of erroring out."""
    try:
        return _base()
    except ValueError:
        if dry_run:
            return "<KAFKA_REST_URL-not-set>"
        raise


def _req(method: str, url: str, body=None, accept: str = _JSON_V2, content_type: str = _JSON_V2):
    headers = {"Accept": accept}
    if body is not None:
        headers["Content-Type"] = content_type
    auth = config.get("KAFKA_REST_AUTH")
    if auth and ":" in auth:
        k, v = auth.split(":", 1)
        headers[k.strip()] = v.strip()
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, context=_ssl_context(), timeout=30) as resp:
        raw = resp.read().decode()
        return json.loads(raw) if raw else None


def cmd_produce(args) -> dict:
    base = _base_for(args.dry_run)
    url = f"{base}/topics/{args.topic}"
    try:
        value = json.loads(args.value)
    except json.JSONDecodeError:
        value = args.value
    record = {"value": value}
    if args.key is not None:
        record["key"] = args.key
    payload = {"records": [record]}

    if args.dry_run:
        return {"dry_run": True, "method": "POST", "url": url, "payload": payload}

    try:
        resp = _req("POST", url, payload)
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as e:
        return {"error": True, "passed": False, "url": url, "message": f"produce failed: {e}"}
    offsets = resp.get("offsets", []) if isinstance(resp, dict) else []
    failed = [o for o in offsets if o.get("error_code")]
    return {"passed": not failed, "op": "produce", "topic": args.topic, "offsets": offsets}


def _new_consumer(base, group, instance):
    url = f"{base}/consumers/{group}"
    body = {"name": instance, "format": "json", "auto.offset.reset": "earliest"}
    return _req("POST", url, body)


def cmd_consume(args) -> dict:
    base = _base_for(args.dry_run)
    group = args.group
    instance = f"probe-{args.topic}-{os.getpid()}-{int(time.time())}"

    if args.dry_run:
        return {"dry_run": True, "topic": args.topic, "group": group,
                "instance": instance, "rest_url": base}

    # Build the consumer-instance URL on the configured proxy host (the base_uri
    # the proxy returns often points at an internal hostname unreachable here).
    inst_path = f"/consumers/{group}/instances/{instance}"
    inst_url = base + inst_path
    try:
        _new_consumer(base, group, instance)
        _req("POST", inst_url + "/subscription", {"topics": [args.topic]})
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as e:
        return {"error": True, "passed": False, "message": f"consumer setup failed: {e}"}

    records = []
    matched = False
    deadline = time.time() + args.timeout
    try:
        while time.time() < deadline and not matched:
            try:
                batch = _req("GET", inst_url + "/records")
            except (urllib.error.URLError, urllib.error.HTTPError, OSError) as e:
                return {"error": True, "passed": False, "message": f"poll failed: {e}"}
            for rec in batch or []:
                records.append(rec)
                if _record_matches(rec, args):
                    matched = True
            if not matched and not batch:
                time.sleep(0.5)
    finally:
        try:
            _req("DELETE", inst_url, body=None, content_type=_JSON_V2)
        except (urllib.error.URLError, urllib.error.HTTPError, OSError):
            pass

    checks = []
    if args.expect_contains or args.expect_json:
        checks.append({"check": "match", "passed": matched})
    if args.min_count is not None:
        checks.append({"check": "min_count", "expected": args.min_count,
                       "actual": len(records), "passed": len(records) >= args.min_count})
    passed = all(c["passed"] for c in checks) if checks else (len(records) > 0)
    return {"passed": passed, "op": "consume", "topic": args.topic,
            "count": len(records), "matched": matched,
            "records": [r.get("value") for r in records[:10]], "checks": checks}


def _record_matches(rec, args) -> bool:
    value = rec.get("value")
    # Compact form (no spaces) so an --expect-contains like '"uid":42' matches
    # the common Kafka message serialization rather than Python's spaced dumps.
    raw = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if args.expect_contains and args.expect_contains not in raw:
        return False
    if args.expect_json:
        path, _, expected = args.expect_json.partition("=")
        try:
            actual = pc.jsonpath_get(value, path)
        except (KeyError, IndexError, TypeError):
            return False
        if not pc.match_value(actual, expected):
            return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Kafka probe via Confluent REST Proxy")
    sub = parser.add_subparsers(dest="action", required=True)

    p = sub.add_parser("produce")
    p.add_argument("--topic", required=True)
    p.add_argument("--value", required=True, help="JSON or raw string")
    p.add_argument("--key")
    p.add_argument("--dry-run", action="store_true")

    c = sub.add_parser("consume")
    c.add_argument("--topic", required=True)
    c.add_argument("--group", default="stack-verify-probe")
    c.add_argument("--timeout", type=int, default=15)
    c.add_argument("--expect-contains")
    c.add_argument("--expect-json", help="'$.path=value'")
    c.add_argument("--min-count", type=int)
    c.add_argument("--dry-run", action="store_true")
    for sp in (p, c):
        sp.add_argument("--project", help="load this project's stack config from ./work/projects.json")
        sp.add_argument("--env", help="environment (local|dev|uat|sandbox|prod); default from project")
        sp.add_argument("--allow-prod", action="store_true", help="permit produce against a protected env")

    args = parser.parse_args()
    err = project_config.apply_args(args, mutating=args.action == "produce")
    if err:
        return pc.emit(err)
    try:
        out = cmd_produce(args) if args.action == "produce" else cmd_consume(args)
    except ValueError as e:
        out = {"error": True, "passed": False, "message": str(e)}
    return pc.emit(out)


if __name__ == "__main__":
    sys.exit(main())
