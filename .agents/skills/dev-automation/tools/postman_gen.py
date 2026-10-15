#!/usr/bin/env python3
"""Generate a Postman Collection (v2.1) from Spring Boot controllers.

For backends WITHOUT Swagger/OpenAPI. Scans *.java for @RestController classes and
extracts endpoints (HTTP method, path, path variables, @RequestParam query params,
@RequestBody type) using pragmatic regex parsing, then emits a Postman collection
that FE/testers import and run as-is — set {{baseUrl}} and {{token}} once.

Zero external dependencies (stdlib only).

LIMITATIONS (regex-based, best-effort — not a full Java parser):
  - Request bodies are emitted as an empty JSON skeleton with the DTO type noted in
    the description (no automatic field expansion).
  - Unusual annotation formatting may be missed. Always eyeball the result.

Usage:
    python postman_gen.py --src /path/to/spring/project --out collection.json
    python postman_gen.py --project atask                 # src = clone_dir from registry
    python postman_gen.py --src ./svc --name "atask API" --base-url https://atask.dev

Output: writes the collection JSON; prints a summary {controllers, endpoints, out}.
"""

import argparse
import json
import os
import re
import sys

import project_config

# Cross-platform: force UTF-8 stdout so the summary JSON doesn't crash on a Windows
# cp1252/cp437 console. No-op elsewhere.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

MAPPING = re.compile(r'@(Get|Post|Put|Delete|Patch|Request)Mapping\b\s*(?:\(([^)]*)\))?', re.S)
HTTP_BY_VERB = {"Get": "GET", "Post": "POST", "Put": "PUT", "Delete": "DELETE", "Patch": "PATCH"}
MODIFIERS = {"public", "protected", "private", "static", "final", "synchronized", "abstract", "default"}


def _repo_root() -> str:
    search = os.path.dirname(os.path.abspath(__file__))
    for _ in range(6):
        if os.path.isdir(os.path.join(search, ".git")) or os.path.isfile(os.path.join(search, "CLAUDE.md")):
            return search
        search = os.path.dirname(search)
    return os.getcwd()


def _strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"//[^\n]*", "", text)
    return text


def _path_from_args(args: str) -> str:
    """Extract the path string from a mapping annotation's argument list."""
    if not args:
        return ""
    m = re.search(r'(?:value|path)\s*=\s*"([^"]*)"', args)
    if m:
        return m.group(1)
    m = re.search(r'"([^"]*)"', args)  # first bare string literal
    return m.group(1) if m else ""


def _join(base: str, path: str) -> str:
    full = "/" + "/".join(p for p in (base.strip("/"), path.strip("/")) if p)
    return full or "/"


def _balanced(text: str, open_idx: int) -> str:
    """Return content inside the parens starting at text[open_idx] == '('."""
    depth = 0
    for i in range(open_idx, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return text[open_idx + 1:i]
    return ""


def _method_signature(sig: str):
    """From text right after a mapping annotation, return (method_name, params_str)."""
    # Drop leading annotations (with optional balanced args) and modifiers.
    no_ann = re.sub(r'@\w+\s*(\([^)]*\))?', '', sig)
    tokens = no_ann.strip()
    # Find first 'identifier(' that is the method name (after return type).
    m = re.search(r'(\w+)\s*\(', tokens)
    if not m or m.group(1) in MODIFIERS:
        # try the next one
        m2 = re.search(r'(\w+)\s*\(', tokens[m.end():]) if m else None
        if not m2:
            return None, None
        name = m2.group(1)
    else:
        name = m.group(1)
    # Capture the real param list (annotations intact) from the original sig.
    pm = re.search(re.escape(name) + r'\s*\(', sig)
    if not pm:
        return name, ""
    params = _balanced(sig, pm.end() - 1)
    return name, params


def _query_params(params: str) -> list:
    """Extract @RequestParam names from a parameter list."""
    out = []
    for m in re.finditer(r'@RequestParam\b\s*(?:\(([^)]*)\))?\s*(?:final\s+)?[\w<>,.\[\]]+\s+(\w+)', params):
        args, java_name = m.group(1) or "", m.group(2)
        named = _path_from_args(args)  # value="name" or "name"
        out.append(named or java_name)
    return out


def _body_type(params: str):
    m = re.search(r'@RequestBody\b\s*(?:\([^)]*\)\s*)?(?:final\s+)?([\w<>,.\[\]]+)\s+\w+', params)
    return m.group(1) if m else None


def parse_controller(text: str, filename: str) -> dict:
    text = _strip_comments(text)
    if "@RestController" not in text and not ("@Controller" in text and "@ResponseBody" in text):
        return None
    cm = re.search(r'\b(?:public\s+|final\s+|abstract\s+)*class\s+(\w+)', text)
    class_name = cm.group(1) if cm else os.path.splitext(os.path.basename(filename))[0]
    prefix = text[:cm.start()] if cm else ""
    bm = re.search(r'@RequestMapping\b\s*\(([^)]*)\)', prefix)
    base = _path_from_args(bm.group(1)) if bm else ""

    endpoints = []
    for m in MAPPING.finditer(text):
        if cm and m.start() < cm.start():
            continue  # class-level @RequestMapping, not an endpoint
        verb, args = m.group(1), m.group(2) or ""
        if verb == "Request":
            hm = re.search(r'RequestMethod\.(\w+)', args)
            http = hm.group(1) if hm else "GET"
        else:
            http = HTTP_BY_VERB[verb]
        path = _path_from_args(args)
        name, params = _method_signature(text[m.end():m.end() + 1200])
        full = _join(base, path)
        endpoints.append({
            "name": name or "endpoint",
            "http": http,
            "path": full,
            "path_vars": re.findall(r'\{(\w+)\}', full),
            "query": _query_params(params or ""),
            "body_type": _body_type(params or ""),
        })
    if not endpoints:
        return None
    return {"controller": class_name, "endpoints": endpoints}


def _to_request(ep: dict) -> dict:
    # Convert {var} path segments to Postman :var and build url object.
    raw_path = re.sub(r'\{(\w+)\}', r':\1', ep["path"])
    segments = [s for s in raw_path.strip("/").split("/") if s]
    url = {
        "raw": "{{baseUrl}}" + raw_path,
        "host": ["{{baseUrl}}"],
        "path": segments,
    }
    if ep["query"]:
        url["query"] = [{"key": q, "value": ""} for q in ep["query"]]
        url["raw"] += "?" + "&".join(f"{q}=" for q in ep["query"])
    if ep["path_vars"]:
        url["variable"] = [{"key": v, "value": ""} for v in ep["path_vars"]]

    request = {"method": ep["http"], "header": [], "url": url}
    desc = []
    if ep["http"] in ("POST", "PUT", "PATCH"):
        request["header"].append({"key": "Content-Type", "value": "application/json"})
        request["body"] = {"mode": "raw", "raw": "{\n  \n}",
                           "options": {"raw": {"language": "json"}}}
        if ep["body_type"]:
            desc.append(f"Request body type: `{ep['body_type']}` (điền field theo DTO).")
    if desc:
        request["description"] = "\n".join(desc)
    return {"name": f"{ep['name']} ({ep['http']} {ep['path']})", "request": request}


def build_collection(controllers: list, name: str, base_url: str) -> dict:
    items = []
    for c in controllers:
        items.append({
            "name": c["controller"],
            "item": [_to_request(ep) for ep in c["endpoints"]],
        })
    return {
        "info": {
            "name": name,
            "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
            "description": "Auto-generated from Spring controllers by postman_gen.py. "
                           "Set the `baseUrl` and `token` collection variables, then run.",
        },
        "item": items,
        "auth": {"type": "bearer", "bearer": [{"key": "token", "value": "{{token}}", "type": "string"}]},
        "variable": [
            {"key": "baseUrl", "value": base_url or "http://localhost:8080"},
            {"key": "token", "value": ""},
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a Postman collection from Spring controllers")
    parser.add_argument("--src", help="Spring project source directory to scan")
    parser.add_argument("--project", help="resolve src from clone_dir in ./work/projects.json")
    parser.add_argument("--out", help="output file (default temp/<name>.postman_collection.json)")
    parser.add_argument("--name", help="collection name (default from project/src)")
    parser.add_argument("--base-url", default="", help="default {{baseUrl}} value")
    args = parser.parse_args()

    src = args.src
    if not src and args.project:
        src = project_config.load(args.project).get("clone_dir")
    if not src:
        print(json.dumps({"error": True, "message": "provide --src <dir> or --project <name> (with clone_dir)"}))
        return 1
    src = os.path.abspath(os.path.expanduser(src))
    if not os.path.isdir(src):
        print(json.dumps({"error": True, "message": f"src not found: {src}"}))
        return 1

    # Skip build output / generated / VCS dirs — only scan real source. Otherwise
    # mvn/gradle target dirs (generated MapStruct/JPA-metamodel .java) inflate counts.
    skip_dirs = {"target", "build", "out", "dist", "bin", ".git", ".gradle",
                 ".idea", ".mvn", "node_modules", "generated-sources", "generated-test-sources"}
    controllers = []
    scanned = 0
    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for fn in files:
            if not fn.endswith(".java"):
                continue
            scanned += 1
            try:
                with open(os.path.join(root, fn), encoding="utf-8", errors="replace") as f:
                    parsed = parse_controller(f.read(), fn)
            except OSError:
                continue
            if parsed:
                controllers.append(parsed)

    name = args.name or (args.project or os.path.basename(src)) + " API"
    collection = build_collection(controllers, name, args.base_url)

    out = args.out
    if not out:
        safe = re.sub(r'\W+', '-', (args.project or os.path.basename(src))).strip('-')
        out = os.path.join(_repo_root(), "temp", f"{safe}.postman_collection.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(collection, f, ensure_ascii=False, indent=2)

    total_ep = sum(len(c["endpoints"]) for c in controllers)
    print(json.dumps({
        "ok": True, "java_scanned": scanned, "controllers": len(controllers),
        "endpoints": total_ep, "out": out,
        "controller_names": [c["controller"] for c in controllers][:50],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
