#!/usr/bin/env python3
"""Read DB/app config straight from a Spring Boot project's application-<env>.yml.

Most of the owner's Java Spring projects keep their per-env DB connection in
`src/main/resources/application-dev.yml` (or .properties). Re-declaring that in
`work/projects.json` for every project is duplicate work and drifts. This tool
parses the Spring config so probes / flow_check / verify can hit the SAME database
the app itself uses — `project_config.resolve()` calls it to FILL GAPS (registry
values always win; spring only supplies what the registry doesn't).

Parses a pragmatic YAML subset (nested maps by indentation, scalars, `---` docs,
`${VAR:default}` placeholders) — enough for Spring config files; lists are ignored.
`.properties` files are supported as a fallback. Stdlib only.

Extracted: spring.datasource.url/username/password (+ driver) -> engine/host/port/
db/schema · server.port + server.servlet.context-path -> local base_url.

Usage:
    python spring_config.py read --project atask --env dev        # clone_dir từ registry
    python spring_config.py read --dir D:/work/atask --env dev [--show-secrets]

Output: one JSON object; password masked unless --show-secrets.
"""

import argparse
import json
import os
import re
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

_PLACEHOLDER = re.compile(r"\$\{([A-Za-z0-9_.\-]+)(?::([^}]*))?\}")
# jdbc:postgresql://h:p/db · jdbc:mysql://... · wrappers like jdbc:log4jdbc:mysql://...
_JDBC = re.compile(
    r"jdbc:(?:[\w]+:)?(postgresql|mysql|mariadb)://([^/:?;,]+)(?::(\d+))?/([^?;,]+)",
    re.IGNORECASE)
MAX_SCAN_DEPTH = 4  # multi-module Maven: */src/main/resources/


def _resolve_placeholders(value):
    """${VAR:default} -> env var if set, else default; ${VAR} without default kept as-is."""
    def repl(m):
        var, default = m.group(1), m.group(2)
        return os.environ.get(var, default if default is not None else m.group(0))
    return _PLACEHOLDER.sub(repl, value)


def _unquote(v):
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1]
    return v


def _parse_yaml_doc(lines):
    """Indentation-based subset: nested maps + scalars. Lists and block scalars skipped."""
    root = {}
    stack = [(-1, root)]  # (indent, container)
    for raw in lines:
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        line = raw.strip()
        if line.startswith("- "):
            continue  # list items: not needed for datasource/server keys
        if ":" not in line:
            continue
        key, _, rest = line.partition(":")
        key = _unquote(key)
        # strip trailing comment on scalar values (not inside quotes — pragmatic)
        rest = rest.strip()
        if rest and not (rest.startswith(("'", '"'))):
            rest = rest.split(" #", 1)[0].strip()
        while stack and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1] if stack else root
        if rest == "" or rest == "|" or rest == ">":
            child = parent.get(key)
            if not isinstance(child, dict):
                child = {}
                parent[key] = child
            stack.append((indent, child))
        else:
            parent[key] = _resolve_placeholders(_unquote(rest))
    return root


def _flatten(d, prefix=""):
    out = {}
    for k, v in d.items():
        dotted = f"{prefix}.{k}" if prefix else str(k)
        if isinstance(v, dict):
            out.update(_flatten(v, dotted))
        else:
            out[dotted.lower()] = v
    return out


def _doc_profile(flat):
    return (flat.get("spring.config.activate.on-profile")
            or flat.get("spring.profiles") or "")


def parse_spring_file(path, env=None):
    """Parse one application*.yml/.properties into a flat {dotted.key: value} map."""
    with open(path, encoding="utf-8") as f:
        text = f.read()
    flat = {}
    if path.endswith(".properties"):
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith(("#", "!")) or "=" not in line:
                continue
            k, _, v = line.partition("=")
            flat[k.strip().lower()] = _resolve_placeholders(_unquote(v))
        return flat
    for doc in re.split(r"^---\s*$", text, flags=re.MULTILINE):
        doc_flat = _flatten(_parse_yaml_doc(doc.splitlines()))
        profile = _doc_profile(doc_flat)
        if profile and env and env not in [p.strip() for p in str(profile).split(",")]:
            continue  # profile-scoped doc for a different env
        flat.update(doc_flat)  # later docs win
    return flat


def find_config_files(clone_dir, env):
    """Locate application-<env>.* (+ base application.*) under src/main/resources.
    Shallowest match wins (root module before submodules)."""
    hits_env, hits_base = [], []
    base_depth = clone_dir.rstrip("/\\").count(os.sep)
    for dirpath, dirnames, filenames in os.walk(clone_dir):
        if dirpath.count(os.sep) - base_depth > MAX_SCAN_DEPTH + 3:
            dirnames[:] = []
            continue
        dirnames[:] = [d for d in dirnames
                       if d not in {".git", "node_modules", "target", "build", ".idea"}]
        norm = dirpath.replace("\\", "/")
        # Chuẩn Spring: src/main/resources · JHipster: src/main/resources/config
        if not (norm.endswith("src/main/resources") or
                norm.endswith("src/main/resources/config")):
            continue
        for fn in filenames:
            low = fn.lower()
            full = os.path.join(dirpath, fn)
            if env and low in (f"application-{env}.yml", f"application-{env}.yaml",
                               f"application-{env}.properties"):
                hits_env.append(full)
            elif low in ("application.yml", "application.yaml", "application.properties"):
                hits_base.append(full)
    key = lambda p: (p.count(os.sep), len(p))  # noqa: E731
    return sorted(hits_base, key=key), sorted(hits_env, key=key)


def _db_from_flat(flat):
    url = flat.get("spring.datasource.url") or flat.get("spring.datasource.jdbc-url") or ""
    m = _JDBC.search(url)
    if not m:
        return {}
    engine = "mysql" if m.group(1).lower() in ("mysql", "mariadb") else "postgres"
    db = {"engine": engine, "host": m.group(2),
          "port": m.group(3) or ("3306" if engine == "mysql" else "5432"),
          "name": m.group(4)}
    qm = re.search(r"[?&](?:currentSchema|searchpath|search_path)=([^&]+)", url, re.I)
    if qm:
        db["schema"] = qm.group(1)
    user = flat.get("spring.datasource.username")
    pw = flat.get("spring.datasource.password")
    if user:
        db["user"] = user
    if pw is not None:
        db["password"] = pw
    return db


def load(clone_dir, env=None):
    """Merged spring config for clone_dir+env -> {db:{...}, server_port, base_url,
    sources:[files]}. {} when nothing found; never raises."""
    try:
        base_files, env_files = find_config_files(clone_dir, env)
    except OSError:
        return {}
    if not base_files and not env_files:
        return {}
    flat = {}
    for p in base_files[:1] + env_files[:1]:  # base first, env overrides
        try:
            flat.update(parse_spring_file(p, env=env))
        except (OSError, UnicodeDecodeError):
            continue
    out = {"sources": base_files[:1] + env_files[:1]}
    db = _db_from_flat(flat)
    if db:
        out["db"] = db
    port = flat.get("server.port")
    if port:
        out["server_port"] = str(port)
        ctx = flat.get("server.servlet.context-path") or ""
        out["base_url"] = f"http://localhost:{port}{ctx if ctx.startswith('/') else ''}"
    return out


def cmd_read(args):
    clone_dir = args.dir
    if not clone_dir and args.project:
        import project_config
        clone_dir = project_config.load(args.project).get("clone_dir")
    if not clone_dir or not os.path.isdir(clone_dir):
        return {"error": True, "message": f"no usable clone_dir (got: {clone_dir!r}); "
                                          f"pass --dir or fix registry"}
    cfg = load(clone_dir, env=args.env)
    if not cfg:
        return {"error": True,
                "message": f"no application config found under {clone_dir} (env={args.env})"}
    if not args.show_secrets and cfg.get("db", {}).get("password"):
        cfg = json.loads(json.dumps(cfg))
        cfg["db"]["password"] = "***"
    cfg["ok"] = True
    return cfg


def main():
    ap = argparse.ArgumentParser(description="Read DB/app config from Spring application-<env>.yml")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("read", help="parse and print resolved config")
    r.add_argument("--project", default=None, help="registry name -> clone_dir")
    r.add_argument("--dir", default=None, help="Spring project root (overrides --project)")
    r.add_argument("--env", default=None, help="profile: dev|uat|... -> application-<env>.yml")
    r.add_argument("--show-secrets", action="store_true", help="in mật khẩu thật thay vì ***")
    args = ap.parse_args()
    try:
        out = cmd_read(args)
    except (OSError, ValueError) as e:
        out = {"error": True, "message": str(e)}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 1 if out.get("error") else 0


if __name__ == "__main__":
    sys.exit(main())
