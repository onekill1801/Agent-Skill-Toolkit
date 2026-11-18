#!/usr/bin/env python3
"""Runtime isolator — tránh xung đột cổng mạng & database giữa các agent song song.

Quét file cấu hình trong một worktree (do worktree_manager tạo) rồi:
  - Đổi cổng (PORT / server.port) sang số trong khoảng 8000-9000.
  - Đổi tên database thành `{tên_db_gốc}_task_{task_id}`.

Cổng được sinh tất định theo task_id (random có seed) => gọi lại cùng task cho cùng cổng,
giảm trôi cấu hình và lặp xung đột. Hậu tố DB là idempotent (không nối chồng nhiều lần).

Chỉ dùng Python stdlib (os + re + random) — không cần pip.

Usage (CLI, in JSON):
    python runtime_isolator.py --workspace <path> --task 123

Hoặc import:
    from runtime_isolator import isolate_environment
"""

import argparse
import json
import os
import random
import re
import sys

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

# Thứ tự quét: .env trước, rồi cấu hình Spring Boot (Java) ở vị trí chuẩn lẫn ở gốc.
CONFIG_CANDIDATES = [
    ".env",
    os.path.join("src", "main", "resources", "application.properties"),
    os.path.join("src", "main", "resources", "application.yml"),
    os.path.join("src", "main", "resources", "application.yaml"),
    "application.properties",
    "application.yml",
    "application.yaml",
]

# .env: chỉ coi các khoá này là CỔNG SERVER (tránh đụng DB_PORT/REDIS_PORT...).
_ENV_PORT_KEYS = {"PORT", "SERVER_PORT", "APP_PORT"}
# .env: các khoá tên database thuần (không phải URL).
_ENV_DB_NAME_KEYS = {
    "DB_NAME", "DATABASE_NAME", "DB_DATABASE",
    "MYSQL_DATABASE", "POSTGRES_DB", "POSTGRESQL_DATABASE",
}


def _port_for(task_id):
    """Cổng tất định theo task_id, trong [8000, 9000]."""
    return random.Random(str(task_id)).randint(8000, 9000)


def _db_suffix(task_id):
    return "_task_{}".format(task_id)


def isolated_db_name(base_name, task_id):
    """Expected isolated DB name for a base DB + task (idempotent).

    NOTE: isolation only RENAMES the DB in config — it does NOT create it. Pass this
    to `probe_db.py check-db --expect-db <name>` before integration probes so a
    missing/unprovisioned/wrong DB can never count as a passing probe.
    """
    suffix = _db_suffix(task_id)
    return base_name if base_name.endswith(suffix) else base_name + suffix


def _rewrite_jdbc_db(value, task_id):
    """Nối hậu tố vào tên database trong một JDBC URL. Idempotent."""
    suffix = _db_suffix(task_id)

    def repl(m):
        db = m.group(2)
        if db.endswith(suffix):
            return m.group(0)
        return m.group(1) + db + suffix

    # jdbc:<driver>://<host[:port]>/<DBNAME>[?params]
    return re.sub(r'(jdbc:[a-zA-Z0-9]+://[^/\s]+/)([A-Za-z0-9_$\-]+)', repl, value)


def _isolate_env(text, task_id, port):
    """Xử lý file .env theo từng dòng (KEY=VALUE)."""
    suffix = _db_suffix(task_id)
    changes = []
    out_lines = []
    for line in text.splitlines():
        m = re.match(r'^(\s*)([A-Za-z_]\w*)(\s*=\s*)(.*)$', line)
        if not m:
            out_lines.append(line)
            continue
        indent, key, eq, val = m.groups()
        ukey = key.upper()
        new_val = val
        if ukey in _ENV_PORT_KEYS and re.fullmatch(r'\s*\d+\s*', val):
            if val.strip() != str(port):
                new_val = str(port)
                changes.append("{}: {} -> {}".format(key, val.strip(), new_val))
        elif ukey in _ENV_DB_NAME_KEYS:
            base = val.strip()
            if base and not base.endswith(suffix):
                new_val = base + suffix
                changes.append("{}: {} -> {}".format(key, base, new_val))
        elif "jdbc:" in val.lower():
            rewritten = _rewrite_jdbc_db(val, task_id)
            if rewritten != val:
                new_val = rewritten
                changes.append("{}: db name suffixed in JDBC url".format(key))
        if new_val != val:
            line = "{}{}{}{}".format(indent, key, eq, new_val)
        out_lines.append(line)
    new_text = "\n".join(out_lines)
    if text.endswith("\n"):
        new_text += "\n"
    return new_text, changes


def _isolate_properties(text, task_id, port):
    """Xử lý application.properties (key=value)."""
    suffix = _db_suffix(task_id)
    changes = []
    out_lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in line:
            out_lines.append(line)
            continue
        key, _eq, val = line.partition("=")
        k = key.strip().lower()
        v = val.strip()
        new_v = v
        if k == "server.port" and re.fullmatch(r'\d+', v):
            if v != str(port):
                new_v = str(port)
                changes.append("server.port: {} -> {}".format(v, new_v))
        elif "jdbc:" in v.lower():
            rewritten = _rewrite_jdbc_db(v, task_id)
            if rewritten != v:
                new_v = rewritten
                changes.append("{}: db name suffixed in JDBC url".format(key.strip()))
        elif k.endswith(".database") or k.endswith("db.name") or k == "spring.datasource.name":
            if v and not v.endswith(suffix):
                new_v = v + suffix
                changes.append("{}: {} -> {}".format(key.strip(), v, new_v))
        if new_v != v:
            line = "{}={}".format(key, new_v)
        out_lines.append(line)
    new_text = "\n".join(out_lines)
    if text.endswith("\n"):
        new_text += "\n"
    return new_text, changes


def _isolate_yaml(text, task_id, port):
    """Xử lý application.yml/.yaml: server.port + (các) datasource url JDBC."""
    changes = []

    def port_repl(m):
        if m.group(2) == str(port):
            return m.group(0)
        changes.append("server.port: {} -> {}".format(m.group(2), port))
        return "{}{}".format(m.group(1), port)

    # Khối server: ... port: <n> (chỉ đổi port nằm trong block server, không đụng port khác).
    new_text = re.sub(
        r'(?ms)^(server:[ \t]*\n(?:[ \t]+.*\n)*?[ \t]+port:[ \t]*)(\d+)',
        port_repl, text,
    )

    def url_repl(m):
        val = m.group(2)
        if "jdbc:" in val.lower():
            rewritten = _rewrite_jdbc_db(val, task_id)
            if rewritten != val:
                changes.append("datasource url: db name suffixed in JDBC url")
                return "{}{}".format(m.group(1), rewritten)
        return m.group(0)

    new_text = re.sub(r'(?im)^([ \t]*url:[ \t]*)(\S+)', url_repl, new_text)
    return new_text, changes


def isolate_environment(workspace_path, task_id):
    """Quét & rewrite cổng + tên DB trong các file cấu hình của worktree.

    Trả dict JSON-friendly mô tả cổng đã cấp, hậu tố DB, và thay đổi theo từng file.
    Chỉ ghi lại file nào thực sự có thay đổi.
    """
    workspace_path = os.path.abspath(workspace_path)
    if not os.path.isdir(workspace_path):
        return {"error": True, "message": "Workspace not found: {}".format(workspace_path)}

    port = _port_for(task_id)
    results = []
    for rel in CONFIG_CANDIDATES:
        path = os.path.join(workspace_path, rel)
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8") as f:
            text = f.read()
        name = os.path.basename(path).lower()
        if name == ".env":
            new_text, changes = _isolate_env(text, task_id, port)
        elif name.endswith(".properties"):
            new_text, changes = _isolate_properties(text, task_id, port)
        elif name.endswith((".yml", ".yaml")):
            new_text, changes = _isolate_yaml(text, task_id, port)
        else:
            continue
        if changes:
            with open(path, "w", encoding="utf-8", newline="") as f:
                f.write(new_text)
        results.append({
            "file": os.path.relpath(path, workspace_path).replace("\\", "/"),
            "modified": bool(changes),
            "changes": changes,
        })

    return {
        "error": False,
        "workspace_path": workspace_path,
        "task_id": str(task_id),
        "assigned_port": port,
        "db_suffix": _db_suffix(task_id),
        "files_found": len(results),
        "files_modified": sum(1 for r in results if r["modified"]),
        "results": results,
    }


def _main():
    parser = argparse.ArgumentParser(description="Cách ly cổng + database cho worktree agent")
    parser.add_argument("--workspace", required=True, help="Đường dẫn worktree")
    parser.add_argument("--task", required=True, help="Task id")
    args = parser.parse_args()
    out = isolate_environment(args.workspace, args.task)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 1 if out.get("error") else 0


if __name__ == "__main__":
    sys.exit(_main())
