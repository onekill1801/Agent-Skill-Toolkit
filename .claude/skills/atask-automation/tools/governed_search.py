#!/usr/bin/env python3
"""Governed query tool for aTask AI-agent (added for backend Phase 6.2.1).

Wraps the `governed_search` tool via POST /api/ai/execute — a SAFE, read-only DSL
query over whitelisted data. The server compiles the query (rejecting any
entity/field/op not on its whitelist), injects the tenant server-side, and enforces
a row limit. No raw SQL is sent.

VIRTUAL MODEL (semantic layer) — 4 entity logic, field ánh xạ cột vật lý (sharded), tự route SQL/ES:
  - task: id, status, priority, listTaskId, parentId, projectId (EQ/IN) · name (CONTAINS) ·
          startDate, dueDate (GT/GTE/LT/LTE) · daysOverdue [computed] (GT/GTE/LT/LTE) ·
          isMine, createdByMe [current-user] (EQ) · projectName, creatorName [selectable, tự điền].
          projectId EQ -> route SQL 1-shard; cross-project (cần isMine/createdByMe=true) -> ES read-model.
  - project: id, name (CONTAINS), code, status, startDate. Scope = chỉ project mình là thành viên.
  - list_task: id, name (CONTAINS), priority, startDate, dueDate, template (EQ). Scope theo list-task của mình.
  - sprint: id, name (CONTAINS), projectId, status, startDate, endDate. Scope theo project mình là thành viên.
  Quan hệ điều hướng (relationship) trả trong `describe` — agent tự query nhiều bước phẳng (engine KHÔNG join).

DISCOVERY (nạp vào prompt chatbot — KHÔNG cần dò bằng lỗi):
  python3 governed_search.py list-entities                 # entity + schemaVersion
  python3 governed_search.py describe --entity task        # field/ops/kind/selectable/sensitivity/relationships

Usage:
  python3 governed_search.py search --entity task --filter "isMine:EQ:true" --filter "daysOverdue:GTE:3"
  python3 governed_search.py search --entity task --filter "projectId:EQ:P1" --filter "priority:IN:HIGH,URGENT"
  python3 governed_search.py search --entity project   --filter "name:CONTAINS:kpi"
  python3 governed_search.py search --entity list_task --filter "template:EQ:false"

Filter format: "field:op:value"  (repeatable --filter). Op: EQ/IN/CONTAINS/GT/GTE/LT/LTE (theo field).
  - op nhiều giá trị (IN): value là danh sách phân tách dấu phẩy -> gửi dạng mảng.
  - Server enforce whitelist entity/field/op + inject tenant + ACL + LIMIT; field/op ngoài whitelist ->
    GOVERNED_QUERY_REJECTED (dùng để biết cái gì được phép). Selectable (projectName/creatorName) tự điền,
    tenant-safe; KHÔNG lọc theo selectable.
"""

import argparse
import sys

import client
import config

# ops whose value is naturally a list (comma-separated on the CLI -> array payload)
_LIST_OPS = {"in", "nin", "not_in", "between", "any", "all"}


def _parse_filter(raw: str) -> dict:
    """'field:op:value' -> {field, op, value}. Value split to list for list-ops."""
    parts = raw.split(":", 2)
    if len(parts) != 3:
        raise ValueError(f"bad --filter '{raw}', expected 'field:op:value'")
    field, op, value = parts[0].strip(), parts[1].strip(), parts[2]
    if op.lower() in _LIST_OPS and "," in value:
        value = [v.strip() for v in value.split(",")]
    return {"field": field, "op": op, "value": value}


def governed_search(entity: str, filters: list, limit=None) -> dict:
    args = {"entity": entity, "filters": filters}
    if limit is not None:
        args["limit"] = limit
    r = client.execute_tool("governed_search", args)
    client.check_error(r, "governed_search")
    return r


def list_entities() -> dict:
    """Liệt kê các entity ảo + schemaVersion (discovery — nạp vào prompt chatbot)."""
    r = client.execute_tool("list_entities", {})
    client.check_error(r, "list_entities")
    return r


def describe_entity(entity: str) -> dict:
    """Mô tả schema 1 entity: field, ops, kind, selectable, sensitivity, relationships (persona-aware server-side)."""
    r = client.execute_tool("describe_entity", {"entity": entity})
    client.check_error(r, "describe_entity")
    return r


if __name__ == "__main__":
    missing = config.validate()
    if missing:
        print(f"[ERROR] Missing config: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)

    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(0)

    cmd = sys.argv[1]

    if cmd == "search":
        parser = argparse.ArgumentParser(prog="governed_search.py search")
        parser.add_argument("--entity", default="task", help="whitelisted entity: task | project | list_task")
        parser.add_argument("--filter", dest="filters", action="append", default=[],
                            help="'field:op:value' (repeatable)")
        parser.add_argument("--limit", type=int, default=None, help="max rows (server caps, e.g. 200)")
        args = parser.parse_args(sys.argv[2:])
        try:
            filters = [_parse_filter(f) for f in args.filters]
        except ValueError as e:
            print(f"[ERROR] {e}", file=sys.stderr)
            sys.exit(1)
        client.print_json(governed_search(args.entity, filters, args.limit))

    elif cmd == "list-entities":
        client.print_json(list_entities())

    elif cmd == "describe":
        parser = argparse.ArgumentParser(prog="governed_search.py describe")
        parser.add_argument("--entity", required=True, help="task | project | list_task | sprint")
        args = parser.parse_args(sys.argv[2:])
        client.print_json(describe_entity(args.entity))

    else:
        print(f"Unknown command: {cmd}\n", file=sys.stderr)
        print(__doc__)
        sys.exit(1)
