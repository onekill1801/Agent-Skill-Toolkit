#!/usr/bin/env python3
"""Ingest a (possibly very long) product requirement into a normalized brief.

A detailed spec is too long/awkward to pass as a slash-command argument, so this
tool reads it from a FILE, from STDIN, or from an inline string, persists it
verbatim, and scaffolds a brief template for the Design step. It also does a light,
mechanical pass to surface candidate feature bullets and headings — the agent does
the real decomposition (skeleton scope vs deferred backlog); this only gives it a
structured starting point.

Task sources (Azure DevOps / aTask): fetch with the existing tools and pipe in, e.g.
    python ../../dev-automation/tools/azure_devops.py get 6955 | python intake.py build --name my-app -
    python ../../atask-automation/tools/tasks.py get-detail 123 | python intake.py build --name my-app -

Stdlib only. Returns JSON; errors as `{"error": true, "message": ...}`.

Usage:
    python intake.py build --name <n> --file <path>     # from a requirements file
    python intake.py build --name <n> --text "..."      # inline (short specs)
    python intake.py build --name <n> -                 # from STDIN (pipe/paste)

Writes under temp/products/<name>/:
    requirement.md   verbatim source (audit trail)
    brief.md         template with the sections the Design step fills in
"""

import argparse
import json
import os
import re
import sys

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass


def _repo_root() -> str:
    search = os.path.dirname(os.path.abspath(__file__))
    for _ in range(6):
        if os.path.isdir(os.path.join(search, ".git")) or os.path.isfile(os.path.join(search, "CLAUDE.md")):
            return search
        search = os.path.dirname(search)
    return os.path.dirname(os.path.abspath(__file__))


def _out_dir(name: str) -> str:
    return os.path.join(_repo_root(), "temp", "products", name)


# Lines that read like a feature / requirement bullet.
_BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*\S)")
_HEADING = re.compile(r"^\s*#{1,6}\s+(.*\S)")
# Verbs that hint at a discrete capability worth becoming a backlog item.
_CAP_HINT = re.compile(
    r"\b(create|add|manage|list|view|edit|update|delete|search|filter|export|import|"
    r"login|register|authenticate|authorize|upload|download|notify|schedule|report|"
    r"tạo|thêm|sửa|xóa|xoá|quản lý|tìm kiếm|đăng nhập|đăng ký|tải|thông báo|báo cáo)\b",
    re.IGNORECASE)


def _extract(text: str) -> dict:
    headings, bullets = [], []
    for line in text.splitlines():
        h = _HEADING.match(line)
        if h:
            headings.append(h.group(1).strip())
            continue
        b = _BULLET.match(line)
        if b:
            bullets.append(b.group(1).strip())
    # Candidate capabilities: bullets that name an action verb (dedup, keep order).
    seen, caps = set(), []
    for b in bullets:
        if _CAP_HINT.search(b):
            key = b.lower()
            if key not in seen:
                seen.add(key)
                caps.append(b)
    return {"headings": headings, "bullets": bullets, "candidate_capabilities": caps}


def _brief_template(name: str, struct: dict) -> str:
    caps = struct["candidate_capabilities"] or struct["bullets"][:10]
    cap_lines = "\n".join(f"- [ ] {c}" for c in caps) or "- [ ] (none auto-detected — read requirement.md)"
    heading_lines = "\n".join(f"- {h}" for h in struct["headings"][:20]) or "- (none)"
    return f"""# {name} — product brief

> Source spec: `requirement.md` (this folder). Fill the sections below during the
> Design step, then present for approval before scaffolding.

## Goal (1–2 sentences)
<what this product does and for whom>

## Stack
<chosen stack + why — confirm with `scaffold.py check <stack>`>

## Data / entities
<the core nouns the product stores>

## Capabilities (from the spec — split into skeleton vs backlog)

### Skeleton scope (build NOW — keep minimal)
- [ ] health endpoint + one core capability wired end-to-end
- [ ] one passing test

### Deferred backlog (hand to /auto-dev, one feature per run)
{cap_lines}

## Non-functional
<auth, persistence engine, integrations, perf, deployment target>

## Detected section headings (for reference)
{heading_lines}
"""


def cmd_build(args) -> dict:
    if args.file:
        if not os.path.isfile(args.file):
            return {"error": True, "message": f"file not found: {args.file}"}
        with open(args.file, encoding="utf-8") as f:
            text = f.read()
        source = os.path.abspath(args.file)
    elif args.text:
        text, source = args.text, "inline --text"
    elif args.stdin:
        text = sys.stdin.read()
        source = "stdin"
    else:
        return {"error": True, "message": "provide one of: --file <path>, --text \"...\", or - (stdin)"}

    text = text.strip()
    if not text:
        return {"error": True, "message": "requirement is empty"}

    out = _out_dir(args.name)
    os.makedirs(out, exist_ok=True)
    req_path = os.path.join(out, "requirement.md")
    with open(req_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text + "\n")

    struct = _extract(text)
    brief_path = os.path.join(out, "brief.md")
    if os.path.isfile(brief_path) and not args.force:
        brief_written = False
    else:
        with open(brief_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(_brief_template(args.name, struct))
        brief_written = True

    words = len(text.split())
    return {"ok": True, "name": args.name, "source": source,
            "requirement": req_path, "brief": brief_path, "brief_written": brief_written,
            "stats": {"chars": len(text), "words": words,
                      "headings": len(struct["headings"]), "bullets": len(struct["bullets"]),
                      "candidate_capabilities": len(struct["candidate_capabilities"])},
            "candidate_capabilities": struct["candidate_capabilities"],
            "note": "read requirement.md in full; fill brief.md during Design; "
                    "split capabilities into skeleton-scope vs deferred backlog"}


def _print(obj) -> None:
    out = json.dumps(obj, indent=2, ensure_ascii=False)
    sys.stdout.buffer.write(out.encode("utf-8"))
    sys.stdout.buffer.write(b"\n")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--name", required=True)
    b.add_argument("--file", default="")
    b.add_argument("--text", default="")
    b.add_argument("stdin", nargs="?", default="", help="pass '-' to read STDIN")
    b.add_argument("--force", action="store_true", help="overwrite an existing brief.md")
    args = p.parse_args()
    args.stdin = args.stdin == "-"
    result = cmd_build(args)
    _print(result)
    return 1 if isinstance(result, dict) and result.get("error") else 0


if __name__ == "__main__":
    sys.exit(main())
