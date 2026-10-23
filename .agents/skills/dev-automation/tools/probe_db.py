#!/usr/bin/env python3
"""Database probe for the stack-verify toolkit (PostgreSQL + MySQL/MariaDB).

Runs a SQL query and asserts on the result: row count, first value, emptiness, or
substring containment. No pip drivers (stdlib only).

Both engines talk the wire protocol directly over a socket — NO external CLI
(psql/mysql), same zero-dependency stance as probe_redis (RESP) / probe_kafka (HTTP):
  - MySQL/MariaDB (mysql_client.py): auth mysql_native_password (5.7 / MariaDB default).
  - PostgreSQL (postgres_client.py): auth SCRAM-SHA-256 / MD5 / cleartext (PG 10+ default
    is SCRAM). Use --schema to set search_path. Both are non-TLS (for TLS use the CLI).

Connection (via .env / env, see config.py); flags override env:
    DB_HOST DB_PORT DB_USER DB_PASSWORD DB_NAME      (generic, used by both)
  PostgreSQL also accepts:  PG_URL  (a full libpq URL: postgresql://user:pw@host/db
    ?currentSchema=...), parsed for host/port/user/password/db/schema.
  Engine default port: postgres=5432, mysql=3306.

Usage:
    python probe_db.py query --engine postgres \
        --sql "select status from users where id=42" \
        --expect-rows 1 --expect-value ACTIVE
    python probe_db.py query --engine mysql --sql "select count(*) from orders" \
        --expect-value 0 --host db --user app --name shop
    python probe_db.py query --engine postgres --sql "..." --dry-run

Output: a single JSON object.
    {"passed": bool, "engine": "...", "rows": int, "first_value": "...",
     "sample": [[...]], "checks": [...], "command": "<masked>"}
"""

import argparse
import sys
from urllib.parse import urlparse, parse_qs

import config
import project_config
import probe_common as pc
import mysql_client
import postgres_client

DEFAULT_PORT = {"postgres": "5432", "mysql": "3306"}


def _conn(args) -> dict:
    return {
        "host": args.host or config.get("DB_HOST", "localhost"),
        "port": args.port or config.get("DB_PORT") or DEFAULT_PORT[args.engine],
        "user": args.user or config.get("DB_USER"),
        "password": args.password or config.get("DB_PASSWORD"),
        "name": args.name or config.get("DB_NAME"),
    }


def _pg_params(args, c) -> dict:
    """Postgres connection params: a full PG_URL (libpq URL) wins, else the flat
    DB_* / flags in `c`. --schema (or currentSchema/search_path in the URL) sets the
    search_path so unqualified table names resolve to the right schema."""
    schema = getattr(args, "schema", None) or config.get("DB_SCHEMA") or None
    pg_url = config.get("PG_URL")
    if pg_url:
        u = urlparse(pg_url)
        q = parse_qs(u.query)
        schema = schema or (q.get("currentSchema") or q.get("search_path") or [None])[0]
        return {"host": u.hostname or c["host"], "port": u.port or c["port"],
                "user": u.username or c["user"], "password": u.password or c["password"],
                "name": u.path.lstrip("/") or c["name"], "schema": schema}
    return {"host": c["host"], "port": c["port"], "user": c["user"],
            "password": c["password"], "name": c["name"], "schema": schema}


def _masked(args, c) -> str:
    """The connection string we print (secrets masked) — for dry-run + results."""
    if args.engine == "mysql":
        return (f"mysql(stdlib socket) {c['host']}:{c['port']}/{c['name'] or ''} "
                f"user={c['user'] or ''} -e '<sql>'")
    p = _pg_params(args, c)
    sch = f" search_path={p['schema']}" if p.get("schema") else ""
    return (f"postgres(stdlib socket) {p['host']}:{p['port']}/{p['name'] or ''} "
            f"user={p['user'] or ''}{sch} -c '<sql>'")


def _run_sql(args, c, sql) -> tuple:
    """Execute `sql`, returning (rows, masked, error_dict|None).

    Both engines speak the wire protocol over a socket — NO external CLI:
      mysql    -> mysql_client    (mysql_native_password)
      postgres -> postgres_client (SCRAM-SHA-256 / MD5 / cleartext)
    rows is list[list[str|None]]; error_dict (if any) is ready to emit as-is.
    """
    masked = _masked(args, c)

    if args.engine == "mysql":
        try:
            cli = mysql_client.connect(c["host"], c["port"], c["user"],
                                       c["password"], c["name"], timeout=args.timeout)
        except (mysql_client.MySQLError, OSError) as e:
            return None, masked, {"error": True, "passed": False, "engine": "mysql",
                                  "command": masked, "message": f"mysql connect failed: {e}"}
        try:
            rows = cli.query(sql)
        except mysql_client.MySQLError as e:
            return None, masked, {"error": True, "passed": False, "engine": "mysql",
                                  "command": masked, "message": str(e)}
        finally:
            cli.close()
        return rows, masked, None

    # postgres
    p = _pg_params(args, c)
    try:
        cli = postgres_client.connect(p["host"], p["port"], p["user"], p["password"],
                                      p["name"], schema=p["schema"], timeout=args.timeout)
    except (postgres_client.PgError, OSError) as e:
        return None, masked, {"error": True, "passed": False, "engine": "postgres",
                              "command": masked, "message": f"postgres connect failed: {e}"}
    try:
        rows = cli.query(sql)
    except postgres_client.PgError as e:
        return None, masked, {"error": True, "passed": False, "engine": "postgres",
                              "command": masked, "message": str(e)}
    finally:
        cli.close()
    return rows, masked, None


_READ_ONLY_SQL = ("select", "show", "explain", "desc", "describe", "values", "with")


def _is_write_sql(sql: str) -> bool:
    """Conservative: anything not clearly a read statement is treated as a write."""
    first = sql.strip().lstrip("(").split(None, 1)[0].lower() if sql.strip() else ""
    return first not in _READ_ONLY_SQL


def cmd_query(args) -> dict:
    c = _conn(args)

    if args.dry_run:
        return {"dry_run": True, "engine": args.engine, "command": _masked(args, c), "sql": args.sql}

    rows, masked, err = _run_sql(args, c, args.sql)
    if err:
        return err

    first_value = rows[0][0] if rows and rows[0] else None
    # Flat text of the result set for substring containment (cells joined by '|').
    stdout_text = "\n".join("|".join("" if cell is None else str(cell) for cell in row)
                            for row in rows)

    checks = []
    if args.expect_rows is not None:
        checks.append({"check": "rows", "expected": args.expect_rows,
                       "actual": len(rows), "passed": len(rows) == args.expect_rows})
    if args.expect_empty:
        checks.append({"check": "empty", "actual": len(rows), "passed": len(rows) == 0})
    if args.expect_value is not None:
        ok = pc.match_value(first_value, args.expect_value)
        checks.append({"check": "first_value", "expected": args.expect_value,
                       "actual": first_value, "passed": ok})
    for substr in args.expect_contains or []:
        checks.append({"check": "contains", "expected": substr,
                       "passed": substr in stdout_text})

    passed = all(ch["passed"] for ch in checks) if checks else True
    return {
        "passed": passed,
        "engine": args.engine,
        "rows": len(rows),
        "first_value": first_value,
        "sample": rows[:getattr(args, "max_rows", 5)],
        "checks": checks,
        "command": masked,
    }


def _check_db_verdict(current, expect_db, engine, masked) -> dict:
    """Pure verdict for check-db: connected, but is it the EXPECTED (isolated) DB?"""
    result = {"passed": True, "engine": engine, "current_database": current,
              "expected_database": expect_db, "command": masked}
    if expect_db is not None and current != expect_db:
        # We connected, but NOT to the isolated DB — an integration probe here would
        # falsely test the wrong (likely shared) database. Treat as error, not pass.
        result["passed"] = False
        result["error"] = True
        result["message"] = (f"connected to '{current}', expected isolated DB "
                             f"'{expect_db}' — isolation not applied to the probe's config")
    return result


def cmd_check_db(args) -> dict:
    """Pre-flight for integration probes on an isolated env.

    Guards the runtime_isolator false-pass: isolation only RENAMES the DB in config,
    it never creates it. This asserts the DB is reachable AND (with --expect-db) that
    we are actually on the isolated database, returning {error:true} otherwise so a
    missing/unprovisioned/wrong DB never counts as a passing probe.
    """
    c = _conn(args)
    ident_sql = "select current_database()" if args.engine == "postgres" else "select database()"
    if args.dry_run:
        return {"dry_run": True, "engine": args.engine, "command": _masked(args, c),
                "expected_database": args.expect_db}
    rows, masked, err = _run_sql(args, c, ident_sql)
    if err:
        # Make the failure check-db-specific: a missing/unprovisioned isolated DB.
        err["message"] = ("cannot connect / identify database (does it exist / is the "
                          "isolated DB provisioned?): " + err.get("message", ""))
        return err
    current = rows[0][0] if rows and rows[0] else None
    return _check_db_verdict(current, args.expect_db, args.engine, masked)


def main() -> int:
    parser = argparse.ArgumentParser(description="DB probe (stdlib socket: mysql + postgres)")
    sub = parser.add_subparsers(dest="action", required=True)
    p = sub.add_parser("query")
    p.add_argument("--engine", choices=["postgres", "mysql"],
                   help="postgres|mysql (default: db.engine from --project, else required)")
    p.add_argument("--sql", required=True)
    p.add_argument("--host")
    p.add_argument("--port")
    p.add_argument("--user")
    p.add_argument("--password")
    p.add_argument("--name", help="database name")
    p.add_argument("--schema", help="postgres: set search_path to this schema")
    p.add_argument("--max-rows", type=int, default=5,
                   help="how many rows to return in 'sample' (default 5; raise to browse data)")
    p.add_argument("--expect-rows", type=int)
    p.add_argument("--expect-empty", action="store_true")
    p.add_argument("--expect-value", help="match the first cell of the first row ('*'=any)")
    p.add_argument("--expect-contains", action="append")
    p.add_argument("--dry-run", action="store_true", help="print resolved command without running")
    p.add_argument("--project", help="load this project's stack config from ./work/projects.json")
    p.add_argument("--env", help="environment (local|dev|uat|sandbox|prod); default from project")
    p.add_argument("--allow-prod", action="store_true", help="permit a write query against a protected env")
    p.add_argument("--timeout", type=int, default=60)

    c = sub.add_parser("check-db", help="assert the (isolated) DB is reachable and is the expected one")
    c.add_argument("--engine", choices=["postgres", "mysql"],
                   help="postgres|mysql (default: db.engine from --project, else required)")
    c.add_argument("--expect-db", help="isolated DB name that MUST be the connected one "
                                       "(e.g. atask_task_123); mismatch -> error, not pass")
    c.add_argument("--host")
    c.add_argument("--port")
    c.add_argument("--user")
    c.add_argument("--password")
    c.add_argument("--name", help="database name to connect to")
    c.add_argument("--schema", help="postgres: set search_path to this schema")
    c.add_argument("--dry-run", action="store_true")
    c.add_argument("--project", help="load this project's stack config from ./work/projects.json")
    c.add_argument("--env", help="environment (local|dev|uat|sandbox|prod); default from project")
    c.add_argument("--timeout", type=int, default=60)

    args = parser.parse_args()
    if args.action == "check-db":
        err = project_config.apply_args(args, mutating=False)
        if err:
            return pc.emit(err)
        if (err := _resolve_engine(args)):
            return pc.emit(err)
        return pc.emit(cmd_check_db(args))
    # Engine may come from the registry (db.engine), so resolve write-vs-read on SQL only.
    err = project_config.apply_args(args, mutating=_is_write_sql(args.sql))
    if err:
        return pc.emit(err)
    if (err := _resolve_engine(args)):
        return pc.emit(err)
    return pc.emit(cmd_query(args))


def _resolve_engine(args):
    """Fill args.engine from the registry (DB_ENGINE injected by --project) if the flag
    was omitted. Returns an error dict if neither yields a valid engine."""
    if not args.engine:
        args.engine = config.get("DB_ENGINE") or None
    if args.engine not in ("postgres", "mysql"):
        return {"error": True, "passed": False,
                "message": "no DB engine: pass --engine postgres|mysql "
                           "(or set db.engine for --project in work/projects.json)"}
    return None


if __name__ == "__main__":
    sys.exit(main())
