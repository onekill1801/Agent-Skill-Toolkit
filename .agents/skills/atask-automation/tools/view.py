#!/usr/bin/env python3
"""Output shaping for aTask list/read tools.

The AI-agent API returns very verbose records (a single `my-tasks` page of 30
tasks is ~1.9MB of nested JSON). That is expensive in tokens and unreadable for
humans. This module projects each record down to the handful of fields that
matter and renders one of three shapes:

  - summary : one compact block per record (default)
  - table   : aligned columns (pick columns with --fields)
  - json    : full raw passthrough (back-compat / when you need every field)

`statusType` (todo/processing/approved/completed/closed) is used as the readable
status because it is always present and meaningful, unlike the opaque per-list
`status` ID. The raw ID is still there under --format json.

Imported by search.py and tasks.py; not run directly.
"""

import json
import sys

# statusType -> short Vietnamese label.
STATUS_LABELS = {
    "todo": "Chưa làm",
    "processing": "Đang làm",
    "approved": "Đã duyệt",
    "completed": "Hoàn thành",
    "closed": "Đã đóng",
}

# Lean projection: the fields worth seeing when scanning a task list.
DEFAULT_FIELDS = ["id", "name", "status", "priority", "due", "project"]


def _due(r):
    return (r.get("dueDate") or "")[:10] or "-"


def _start(r):
    return (r.get("startDate") or "")[:10] or "-"


def _status(r):
    # Prefer the list-specific status name (incl. custom statuses) when the server resolved it
    # (port-path tools: query/subtasks/by-sprint/get); fall back to the statusType label (ES search).
    name = r.get("statusName")
    if name:
        return name
    st = r.get("statusType")
    return STATUS_LABELS.get(st, st or "-")


# Friendly field name -> how to pull it from a raw record.
_FIELD_GETTERS = {
    "id": lambda r: r.get("id") or "-",
    "name": lambda r: r.get("name") or "-",
    "status": _status,
    "statusName": lambda r: r.get("statusName") or "-",
    "statusType": lambda r: r.get("statusType") or "-",
    "priority": lambda r: "-" if r.get("priority") in (None, "") else str(r.get("priority")),
    "due": _due,
    "start": _start,
    "project": lambda r: r.get("projectName") or "-",
    "list": lambda r: r.get("listTaskId") or "-",
    "parent": lambda r: r.get("parentId") or "-",
}


def _get(r, field):
    g = _FIELD_GETTERS.get(field)
    if g:
        return g(r)
    v = r.get(field)
    return "-" if v in (None, "") else str(v)


def extract_records(result):
    """Return (records, meta) from any list/read response shape.

    Returns (None, None) when this isn't a shapeable success envelope (error or
    unknown shape) — the caller should fall back to printing the raw result.
    """
    if not isinstance(result, dict) or result.get("success") is False or result.get("error"):
        return None, None
    content = result.get("content")
    if isinstance(content, dict):
        if isinstance(content.get("data"), list):
            meta = {k: content[k] for k in ("totalRecords", "page", "size") if k in content}
            return content["data"], meta
        return [content], {}            # single record, e.g. get_task
    if isinstance(content, list):
        return content, {}
    return None, None


def _clip(s, n):
    s = str(s)
    return s if len(s) <= n else s[:n - 1] + "…"


def _render_summary(records):
    lines = []
    for i, r in enumerate(records, 1):
        lines.append(f"{i:>2}. [{_get(r, 'status')}] {_get(r, 'name')}")
        lines.append(f"     id={_get(r, 'id')} | due={_get(r, 'due')} | "
                     f"prio={_get(r, 'priority')} | {_get(r, 'project')}")
    return "\n".join(lines) if lines else "(no records)"


def _render_table(records, fields):
    if not records:
        return "(no records)"
    headers = [f.upper() for f in fields]
    rows = [[_clip(_get(r, f), 60) for f in fields] for r in records]
    widths = [len(h) for h in headers]
    for row in rows:
        for j, cell in enumerate(row):
            widths[j] = max(widths[j], len(cell))

    def fmt(cells):
        return "  ".join(str(c).ljust(widths[j]) for j, c in enumerate(cells))

    out = [fmt(headers), fmt(["-" * w for w in widths])]
    out += [fmt(r) for r in rows]
    return "\n".join(out)


def _out(s):
    sys.stdout.buffer.write(s.encode("utf-8"))
    sys.stdout.buffer.write(b"\n")


def _print_json(data):
    _out(json.dumps(data, indent=2, ensure_ascii=False))


def emit(result, fmt="summary", fields=None):
    """Print a shaped view of a list/read result. fmt: summary|table|json."""
    if fmt == "json":
        _print_json(result)
        return
    records, meta = extract_records(result)
    if records is None:                 # error or unknown shape -> show raw
        _print_json(result)
        return
    fields = fields or DEFAULT_FIELDS
    _out(_render_table(records, fields) if fmt == "table" else _render_summary(records))
    total = (meta or {}).get("totalRecords")
    _out(f"\n{len(records)} shown" + (f" / {total} total" if total is not None else ""))


def add_view_args(parser, default_format="summary"):
    """Attach --format / --fields to an argparse parser.

    default_format lets single-record reads (e.g. `tasks.py get`) default to the
    full `json` detail, while list reads default to the lean `summary`.
    """
    parser.add_argument("--format", default=default_format, choices=["summary", "table", "json"],
                        help=f"Output shape (default: {default_format}; json = full raw)")
    parser.add_argument("--fields", default=None,
                        help="Columns for --format table, e.g. id,name,status,due,priority,project")


def parse_fields(s):
    return [f.strip() for f in s.split(",") if f.strip()] if s else None
