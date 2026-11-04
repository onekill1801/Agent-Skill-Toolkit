#!/usr/bin/env python3
"""Redis probe for the stack-verify toolkit.

Speaks the RESP protocol directly over a TCP socket — no pip client (stdlib only).
Supports the read/verify operations a backend test needs: GET, EXISTS, TTL, KEYS,
TYPE, plus SET / DEL for fixture setup/teardown.

Connection (via .env / env, see config.py); flags override env:
    REDIS_HOST (default localhost)  REDIS_PORT (6379)
    REDIS_PASSWORD (optional)       REDIS_DB (0)

Usage:
    python probe_redis.py get user:42 --expect-exists
    python probe_redis.py get session:abc --expect-value '{"uid":42}'
    python probe_redis.py ttl user:42 --expect-ttl-min 30
    python probe_redis.py exists cart:42 --expect-missing
    python probe_redis.py set test:k v --ex 60        # fixture setup
    python probe_redis.py del test:k                  # teardown
    python probe_redis.py keys 'user:*'

Output: a single JSON object.
    {"passed": bool, "op": "...", "key": "...", "result": <...>, "checks": [...]}
"""

import argparse
import socket
import sys

import config
import project_config
import probe_common as pc


class RedisError(Exception):
    pass


class Redis:
    """Tiny RESP client: connect, send a command, read one reply."""

    def __init__(self, host, port, password, db, timeout):
        self.sock = socket.create_connection((host, int(port)), timeout=timeout)
        self.reader = self.sock.makefile("rb")
        if password:
            self.command("AUTH", password)
        if str(db) not in ("", "0"):
            self.command("SELECT", db)

    def command(self, *args):
        parts = [f"*{len(args)}\r\n".encode()]
        for a in args:
            b = str(a).encode()
            parts.append(f"${len(b)}\r\n".encode() + b + b"\r\n")
        self.sock.sendall(b"".join(parts))
        return self._read()

    def _read(self):
        line = self.reader.readline()
        if not line:
            raise RedisError("connection closed by server")
        tag, body = line[:1], line[1:].rstrip(b"\r\n")
        if tag == b"+":
            return body.decode()
        if tag == b"-":
            raise RedisError(body.decode())
        if tag == b":":
            return int(body)
        if tag == b"$":
            n = int(body)
            if n == -1:
                return None
            data = self.reader.read(n + 2)  # value + trailing CRLF
            return data[:-2].decode(errors="replace")
        if tag == b"*":
            n = int(body)
            if n == -1:
                return None
            return [self._read() for _ in range(n)]
        raise RedisError(f"unexpected RESP tag: {tag!r}")

    def close(self):
        try:
            self.reader.close()
            self.sock.close()
        except OSError:
            pass


def _connect(args) -> Redis:
    return Redis(
        host=args.host or config.get("REDIS_HOST", "localhost"),
        port=args.port or config.get("REDIS_PORT", "6379"),
        password=args.password or config.get("REDIS_PASSWORD"),
        db=args.db or config.get("REDIS_DB", "0"),
        timeout=args.timeout,
    )


def run(args) -> dict:
    try:
        r = _connect(args)
    except (OSError, RedisError) as e:
        return {"error": True, "passed": False, "message": f"connect failed: {e}"}

    out = {"op": args.op, "passed": True, "checks": []}
    try:
        if args.op == "get":
            val = r.command("GET", args.key)
            out.update(key=args.key, result=val)
            if args.expect_exists:
                out["checks"].append({"check": "exists", "passed": val is not None})
            if args.expect_missing:
                out["checks"].append({"check": "missing", "passed": val is None})
            if args.expect_value is not None:
                out["checks"].append({"check": "value", "expected": args.expect_value,
                                      "actual": val, "passed": pc.match_value(val, args.expect_value)})
        elif args.op == "exists":
            n = r.command("EXISTS", args.key)
            out.update(key=args.key, result=n)
            if args.expect_exists:
                out["checks"].append({"check": "exists", "passed": n == 1})
            if args.expect_missing:
                out["checks"].append({"check": "missing", "passed": n == 0})
        elif args.op == "ttl":
            t = r.command("TTL", args.key)
            out.update(key=args.key, result=t)
            if args.expect_ttl_min is not None:
                out["checks"].append({"check": "ttl_min", "expected": args.expect_ttl_min,
                                      "actual": t, "passed": t >= args.expect_ttl_min})
        elif args.op == "type":
            out.update(key=args.key, result=r.command("TYPE", args.key))
        elif args.op == "keys":
            keys = r.command("KEYS", args.key)
            out.update(pattern=args.key, result=keys, count=len(keys or []))
            if args.expect_count is not None:
                out["checks"].append({"check": "count", "expected": args.expect_count,
                                      "actual": len(keys or []), "passed": len(keys or []) == args.expect_count})
        elif args.op == "set":
            if args.ex:
                res = r.command("SET", args.key, args.value, "EX", args.ex)
            else:
                res = r.command("SET", args.key, args.value)
            out.update(key=args.key, result=res)
        elif args.op == "del":
            out.update(key=args.key, result=r.command("DEL", args.key))
        out["passed"] = all(c["passed"] for c in out["checks"]) if out["checks"] else True
    except RedisError as e:
        out = {"error": True, "passed": False, "op": args.op, "message": str(e)}
    finally:
        r.close()
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Redis probe (RESP over socket)")
    parser.add_argument("op", choices=["get", "exists", "ttl", "type", "keys", "set", "del"])
    parser.add_argument("key", help="key, or pattern for 'keys'")
    parser.add_argument("value", nargs="?", help="value for 'set'")
    parser.add_argument("--ex", type=int, help="expiry seconds for 'set'")
    parser.add_argument("--host")
    parser.add_argument("--port")
    parser.add_argument("--password")
    parser.add_argument("--db")
    parser.add_argument("--expect-exists", action="store_true")
    parser.add_argument("--expect-missing", action="store_true")
    parser.add_argument("--expect-value")
    parser.add_argument("--expect-ttl-min", type=int)
    parser.add_argument("--expect-count", type=int)
    parser.add_argument("--project", help="load this project's stack config from ./work/projects.json")
    parser.add_argument("--env", help="environment (local|dev|uat|sandbox|prod); default from project")
    parser.add_argument("--allow-prod", action="store_true", help="permit set/del against a protected env")
    parser.add_argument("--timeout", type=int, default=10)

    args = parser.parse_args()
    err = project_config.apply_args(args, mutating=args.op in ("set", "del"))
    if err:
        return pc.emit(err)
    return pc.emit(run(args))


if __name__ == "__main__":
    sys.exit(main())
