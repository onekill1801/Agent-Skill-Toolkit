#!/usr/bin/env python3
"""Read DBeaver connection METADATA (no credentials) and run probe_db against it.

Reads only `data-sources.json` of every DBeaver project: name, folder, engine, host,
port, database, schema. DBeaver's credential store is NOT read — the user/password
for `probe` come from the environment / .env (DB_USER, DB_PASSWORD) or flags, exactly
like probe_db.py.

A connection is PROTECTED when its folder, name or DBeaver type contains "prod":
`probe` then only runs read-only SQL (SELECT / SHOW / EXPLAIN / WITH / DESCRIBE). Also
protected: a localhost connection whose port is a LIVE Lens forward to a prod service.

Connections on localhost are linked to the Lens port-forward listening on that port
(`k8s.py forwards`), so you can see which cluster/service is behind them and if it is up.

    python dbeaver.py list [--q text] [--folder PROD] [--engine postgres|mysql]
    python dbeaver.py show <name|id>
    python dbeaver.py forwards
    python dbeaver.py probe <name|id> --sql "select 1" [--max-rows 5] [--expect-value X]

Workspace: $DBEAVER_WORKSPACE, else the per-OS default (…/DBeaverData/workspace6).
"""

import argparse
import glob
import json
import os
import platform
import re
import subprocess
import sys
from urllib.parse import parse_qs, urlparse

import probe_common as pc

ENGINES = {"postgresql": "postgres", "postgres": "postgres", "mysql": "mysql", "mariadb": "mysql"}
READ_ONLY_SQL = re.compile(r"^\s*(select|show|explain|with|describe|desc|values)\b", re.I)
WRITE_WORDS = re.compile(
    r"\b(insert|update|delete|merge|upsert|alter|drop|truncate|create|grant|revoke|call|exec|execute|do|copy|"
    r"set|reset|rename|replace|lock|unlock|vacuum|analyze|reindex|cluster|refresh|load|handler|prepare|"
    r"into|outfile|dumpfile|for\s+update|for\s+share)\b", re.I)
# SELECT-able functions with side effects (not exhaustive — the real guard is a read-only DB user)
SIDE_EFFECT_FN = re.compile(
    r"\b(pg_terminate_backend|pg_cancel_backend|pg_reload_conf|pg_rotate_logfile|pg_promote|setval|nextval|"
    r"lo_import|lo_export|lo_unlink|dblink\w*|pg_read_file|pg_read_binary_file|pg_ls_dir|pg_sleep|"
    r"sys_exec|sys_eval|sleep|benchmark|get_lock|release_lock|load_file)\s*\(", re.I)
PROD_RE = re.compile(r"(^|[^a-z])(prod|prd|production)([^a-z]|$)", re.I)   # same idea as podfwd._protected
# probe_db flags that would retarget the query away from the connection that was checked
LOCKED_FLAGS = ("--host", "--port", "--name", "--engine", "--schema", "--sql", "--db", "--database")


def prod_sql_problem(sql: str):
    """Why `sql` is not allowed on a PROTECTED connection, or None."""
    body = sql.strip().rstrip(";").strip()
    if ";" in body:
        return "multiple statements are not allowed"
    if not READ_ONLY_SQL.match(body):
        return "must start with SELECT/SHOW/EXPLAIN/WITH/DESCRIBE/VALUES"
    m = WRITE_WORDS.search(body) or SIDE_EFFECT_FN.search(body)
    if m:
        return f"contains '{m.group(0).strip()}'"
    return None


def locked_flag(extra: list):
    for a in extra or []:
        flag = a.split("=", 1)[0].lower()
        if flag in LOCKED_FLAGS:
            return flag
    return None


def workspace() -> str:
    if os.environ.get("DBEAVER_WORKSPACE"):
        return os.environ["DBEAVER_WORKSPACE"]
    home = os.path.expanduser("~")
    system = platform.system()
    if system == "Windows":
        base = os.environ.get("APPDATA") or os.path.join(home, "AppData", "Roaming")
    elif system == "Darwin":
        base = os.path.join(home, "Library")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.join(home, ".local", "share")
    return os.path.join(base, "DBeaverData", "workspace6")


def _parse_url(url: str) -> dict:
    """jdbc:postgresql://h:5432/db?currentSchema=s -> {host, port, database, schema}."""
    if not url or not url.startswith("jdbc:"):
        return {}
    u = urlparse(url[5:])
    q = parse_qs(u.query)
    return {"host": u.hostname, "port": str(u.port) if u.port else None,
            "database": (u.path or "/").lstrip("/").split(";")[0] or None,
            "schema": (q.get("currentSchema") or q.get("searchpath") or [None])[0]}


def connections() -> list:
    out = []
    for f in sorted(glob.glob(os.path.join(workspace(), "*", ".dbeaver", "data-sources*.json"))):
        project = os.path.basename(os.path.dirname(os.path.dirname(f)))
        try:
            with open(f, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        for cid, c in (data.get("connections") or {}).items():
            cfg = c.get("configuration") or {}
            manual = {"host": cfg.get("host"), "port": cfg.get("port"), "database": cfg.get("database"),
                      "schema": None}
            parsed = _parse_url(cfg.get("url", ""))
            eff = parsed if cfg.get("configurationType") == "URL" and parsed else manual
            folder, name, ctype = c.get("folder") or "", c.get("name") or cid, cfg.get("type") or ""
            out.append({
                "id": cid, "name": name, "folder": folder, "project": project,
                "engine": ENGINES.get(c.get("provider", ""), c.get("provider")),
                "driver": c.get("driver"),
                "host": eff.get("host"), "port": eff.get("port"), "database": eff.get("database"),
                "schema": eff.get("schema"),
                "via_tunnel_handler": bool(cfg.get("handlers")),
                "protected": bool(PROD_RE.search(f"{folder} {name} {ctype}")),
            })
    return out


def find(ref: str) -> dict:
    conns = connections()
    if "/" in ref and not ref.startswith(("jdbc", "/")):          # "Folder/Name"
        folder, _, name = ref.partition("/")
        conns = [c for c in conns if c["folder"].lower() == folder.lower()] or conns
        ref = name
    exact = [c for c in conns if ref in (c["id"], c["name"])]
    if len(exact) == 1:
        return exact[0]
    loose = [c for c in conns if ref.lower() in c["name"].lower()]
    if len(loose) == 1:
        return loose[0]
    names = [f"{c['folder']}/{c['name']}" for c in (exact or loose)][:15]
    raise ValueError(f"'{ref}' matches {len(exact or loose)} connections: {names}" if names
                     else f"no DBeaver connection named '{ref}'")


def _forwards_by_port() -> dict:
    try:
        import k8s
        return {int(r["local_port"]): r for r in k8s.lens_forwards() if r.get("local_port")}
    except Exception:  # noqa: BLE001 — Lens absent, kubectl missing...
        return {}


def with_forward(c: dict, fwd: dict) -> dict:
    if c.get("host") in ("localhost", "127.0.0.1") and str(c.get("port") or "").isdigit():
        f = fwd.get(int(c["port"]))
        if f:
            c = dict(c, lens_forward={k: f.get(k) for k in ("context", "namespace", "target", "remote_port",
                                                            "lens_status", "listening", "note", "start_cmd")})
            live = f.get("lens_status") == "Active" and f.get("listening")
            if live and PROD_RE.search(f"{f.get('target')} {f.get('context')}") and not c["protected"]:
                c["protected"] = True
                c["protected_reason"] = "localhost port is a live Lens forward to a prod service"
    return c


def probe(ref: str, sql: str, extra: list) -> dict:
    c = with_forward(find(ref), _forwards_by_port())   # protection also follows the tunnel target
    if c["engine"] not in ("postgres", "mysql"):
        raise ValueError(f"engine '{c['engine']}' not supported by probe_db (postgres/mysql only)")
    bad_flag = locked_flag(extra)
    if bad_flag:
        raise PermissionError(f"{bad_flag} is fixed by the DBeaver connection and cannot be overridden")
    if c["protected"]:
        why = prod_sql_problem(sql)
        if why:
            raise PermissionError(f"'{c['folder']}/{c['name']}' is PROD — only one read-only statement: {why}")
    args = [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "probe_db.py"),
            "query", "--engine", c["engine"], "--sql", sql, "--host", c["host"] or "",
            "--port", str(c["port"] or ""), "--name", c["database"] or ""]
    if c.get("schema") and c["engine"] == "postgres":
        args += ["--schema", c["schema"]]
    p = subprocess.run(args + extra, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=120)
    try:
        out = json.loads(p.stdout)
    except ValueError:
        out = {"error": True, "message": (p.stderr or p.stdout)[-400:]}
    out["connection"] = {k: c[k] for k in ("folder", "name", "engine", "host", "port", "database", "protected")}
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="dbeaver.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("list")
    p.add_argument("--q", default="")
    p.add_argument("--folder", default="")
    p.add_argument("--engine", default="")
    p = sub.add_parser("show")
    p.add_argument("ref")
    sub.add_parser("forwards")
    p = sub.add_parser("probe")
    p.add_argument("ref")
    p.add_argument("--sql", required=True)
    a, extra = ap.parse_known_args(argv)
    try:
        if a.cmd == "list":
            rows = [c for c in connections()
                    if (not a.q or a.q.lower() in f"{c['name']} {c['host']} {c['database']}".lower())
                    and (not a.folder or c["folder"].lower() == a.folder.lower())
                    and (not a.engine or c["engine"] == a.engine)]
            out = {"workspace": workspace(), "count": len(rows), "connections": rows}
        elif a.cmd == "show":
            out = with_forward(find(a.ref), _forwards_by_port())
        elif a.cmd == "forwards":
            fwd = _forwards_by_port()
            rows = [r for r in (with_forward(c, fwd) for c in connections()) if r.get("lens_forward")]
            out = {"count": len(rows), "connections": rows}
        else:
            out = probe(a.ref, a.sql, extra)
    except (ValueError, PermissionError) as e:
        out = {"error": True, "message": str(e)}
    return pc.emit(out)


if __name__ == "__main__":
    sys.exit(main())
