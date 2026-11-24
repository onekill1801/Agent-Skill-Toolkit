#!/usr/bin/env python3
"""Scaffold a new product skeleton for a chosen tech stack.

Three scaffolding methods, picked per recipe (see RECIPES):
  - http_zip : download a generated starter (e.g. start.spring.io) via urllib and
               unpack with zipfile. NO external CLI needed — works for Java/Spring.
  - cli      : shell out to an official generator (npx create-next-app, npm create
               vite, ...). Requires the CLI to be installed; `check` reports this.
  - template : write a small embedded starter directly. Fully OFFLINE; the fallback
               for stacks with no canonical generator (FastAPI, Express, static, CLI).

Stdlib only (urllib, zipfile, subprocess, shutil). Returns JSON on stdout; errors
follow `{"error": true, "message": ...}`. Network/CLI failures are reported, never
raised as crashes — the caller (agent) can fall back to hand-writing files.

Usage:
    python scaffold.py recipes                      # list known stacks + method
    python scaffold.py check <stack>                # is this stack scaffoldable here?
    python scaffold.py run <stack> --name <n> --dir <path> [opts]   # [WRITE]

Common run opts:
    --java-version 21 --group com.example --deps web,data-jpa,postgresql   (spring)
    --package-manager npm|pnpm|yarn                                         (cli stacks)
    --force                                                                 (dir may exist/non-empty)
"""

import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
import zipfile

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

SPRING_INITIALIZR = "https://start.spring.io/starter.zip"

# Each recipe: method + metadata. `cli` recipes name the executable to probe and
# build their command in _run_cli. `template` recipes name an embedded writer.
RECIPES = {
    "java-spring": {
        "method": "http_zip",
        "language": "Java / Spring Boot",
        "default_deps": ["web", "data-jpa", "postgresql", "validation", "lombok"],
        "build_cmd": "mvn -B -q -DskipTests package",
        "test_cmd": "mvn -B test",
        "lint_cmd": "mvn -B -q checkstyle:check",
    },
    "next": {
        "method": "cli",
        "language": "Next.js (React/TS)",
        "cli": "npx",
        "build_cmd": "npm run build",
        "test_cmd": "npm test",
        "lint_cmd": "npm run lint",
    },
    "react-vite": {
        "method": "cli",
        "language": "React + Vite (TS)",
        "cli": "npm",
        "build_cmd": "npm run build",
        "test_cmd": "npm test",
        "lint_cmd": "npm run lint",
    },
    "node-express": {
        "method": "template",
        "language": "Node.js + Express",
        "build_cmd": "npm run build",
        "test_cmd": "npm test",
        "lint_cmd": "npm run lint",
    },
    "fastapi": {
        "method": "template",
        "language": "Python + FastAPI",
        "build_cmd": "python -m pip install -e .",
        "test_cmd": "python -m pytest",
        "lint_cmd": "python -m ruff check .",
    },
    "python-cli": {
        "method": "template",
        "language": "Python CLI (stdlib)",
        "build_cmd": "",
        "test_cmd": "python -m pytest",
        "lint_cmd": "python -m ruff check .",
    },
    "static": {
        "method": "template",
        "language": "Static HTML/CSS/JS",
        "build_cmd": "",
        "test_cmd": "",
        "lint_cmd": "",
    },
}


def _which(exe: str) -> str:
    """Cross-platform: on Windows npx/npm resolve to .cmd shims."""
    for cand in (exe, exe + ".cmd", exe + ".exe"):
        found = shutil.which(cand)
        if found:
            return found
    return ""


def _ssl_ctx():
    import ssl
    return ssl.create_default_context()


def _ensure_dir(path: str, force: bool):
    if os.path.isdir(path) and os.listdir(path) and not force:
        raise FileExistsError(f"target dir '{path}' exists and is not empty (pass --force)")
    os.makedirs(path, exist_ok=True)


# ---------------------------------------------------------------------------
# http_zip: Spring Initializr
# ---------------------------------------------------------------------------
def _run_spring(name: str, target: str, args) -> dict:
    deps = args.deps.split(",") if args.deps else RECIPES["java-spring"]["default_deps"]
    group = args.group or "com.example"
    artifact = name.replace("_", "-").lower()
    params = {
        "type": "maven-project",
        "language": "java",
        "bootVersion": "",  # empty = Initializr's current default
        "baseDir": ".",
        "groupId": group,
        "artifactId": artifact,
        "name": artifact,
        "packageName": f"{group}.{artifact.replace('-', '')}",
        "javaVersion": args.java_version or "21",
        "dependencies": ",".join(d.strip() for d in deps if d.strip()),
    }
    params = {k: v for k, v in params.items() if v != ""}
    url = SPRING_INITIALIZR + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "new-product-skill"})
    try:
        with urllib.request.urlopen(req, context=_ssl_ctx(), timeout=60) as resp:
            blob = resp.read()
    except Exception as e:  # noqa: BLE001 — report, never crash
        return {"error": True, "message": f"start.spring.io unreachable: {e}. "
                "Fall back to hand-writing a pom.xml + main class, or retry with network."}
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            zf.extractall(target)
    except zipfile.BadZipFile:
        return {"error": True, "message": "start.spring.io returned a non-zip response "
                "(check dependency ids with `check java-spring`)"}
    return {"dependencies": params["dependencies"], "group": group, "artifact": artifact,
            "source": url}


# ---------------------------------------------------------------------------
# cli: npx / npm generators
# ---------------------------------------------------------------------------
def _run_cli(stack: str, name: str, target: str, args) -> dict:
    pm = args.package_manager or "npm"
    if stack == "next":
        exe = _which("npx")
        if not exe:
            return {"error": True, "message": "npx not found on PATH. Install Node.js, "
                    "or scaffold offline with stack=node-express as a fallback."}
        cmd = [exe, "--yes", "create-next-app@latest", ".",
               "--ts", "--eslint", "--app", "--use-" + pm,
               "--src-dir", "--no-tailwind", "--no-import-alias"]
    elif stack == "react-vite":
        exe = _which(pm) or _which("npm")
        if not exe:
            return {"error": True, "message": f"{pm}/npm not found on PATH. Install Node.js, "
                    "or scaffold offline with stack=static as a fallback."}
        # npm create vite@latest . -- --template react-ts
        cmd = [exe, "create", "vite@latest", ".", "--", "--template", "react-ts"]
    else:
        return {"error": True, "message": f"no cli recipe for '{stack}'"}

    try:
        proc = subprocess.run(cmd, cwd=target, capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"error": True, "message": f"generator failed to run: {e}"}
    if proc.returncode != 0:
        return {"error": True, "message": f"generator exited {proc.returncode}",
                "stderr": (proc.stderr or "")[-2000:]}
    return {"generator": " ".join(cmd), "stdout_tail": (proc.stdout or "")[-500:]}


# ---------------------------------------------------------------------------
# template: embedded offline starters
# ---------------------------------------------------------------------------
def _w(target: str, rel: str, content: str):
    path = os.path.join(target, rel)
    os.makedirs(os.path.dirname(path) or target, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)


def _tpl_node_express(name: str, target: str) -> dict:
    _w(target, "package.json", json.dumps({
        "name": name, "version": "0.1.0", "type": "module", "main": "src/index.js",
        "scripts": {"start": "node src/index.js", "dev": "node --watch src/index.js",
                    "test": "node --test", "build": "echo \"no build step\"",
                    "lint": "echo \"add eslint\""},
        "dependencies": {"express": "^4.19.2"},
    }, indent=2) + "\n")
    _w(target, "src/index.js",
       "import express from 'express';\n\n"
       "const app = express();\n"
       "app.use(express.json());\n\n"
       "app.get('/health', (_req, res) => res.json({ status: 'ok' }));\n\n"
       "const port = process.env.PORT || 3000;\n"
       "app.listen(port, () => console.log(`listening on ${port}`));\n\n"
       "export default app;\n")
    _w(target, "test/health.test.js",
       "import { test } from 'node:test';\nimport assert from 'node:assert';\n\n"
       "test('placeholder', () => { assert.equal(1 + 1, 2); });\n")
    return {"files": ["package.json", "src/index.js", "test/health.test.js"],
            "next": "run `npm install` before `npm start`"}


def _tpl_fastapi(name: str, target: str) -> dict:
    pkg = name.replace("-", "_")
    _w(target, "pyproject.toml",
       f'[project]\nname = "{name}"\nversion = "0.1.0"\nrequires-python = ">=3.10"\n'
       'dependencies = ["fastapi", "uvicorn[standard]"]\n\n'
       '[build-system]\nrequires = ["setuptools"]\nbuild-backend = "setuptools.build_meta"\n\n'
       f'[tool.setuptools]\npackages = ["{pkg}"]\n')
    _w(target, f"{pkg}/__init__.py", "")
    _w(target, f"{pkg}/main.py",
       "from fastapi import FastAPI\n\n"
       "app = FastAPI()\n\n\n"
       "@app.get('/health')\n"
       "def health():\n"
       "    return {'status': 'ok'}\n")
    _w(target, "tests/test_health.py",
       f"from fastapi.testclient import TestClient\nfrom {pkg}.main import app\n\n"
       "client = TestClient(app)\n\n\n"
       "def test_health():\n"
       "    r = client.get('/health')\n"
       "    assert r.status_code == 200\n"
       "    assert r.json() == {'status': 'ok'}\n")
    return {"files": ["pyproject.toml", f"{pkg}/main.py", "tests/test_health.py"],
            "next": f"run `uvicorn {pkg}.main:app --reload` to serve"}


def _tpl_python_cli(name: str, target: str) -> dict:
    pkg = name.replace("-", "_")
    _w(target, "pyproject.toml",
       f'[project]\nname = "{name}"\nversion = "0.1.0"\nrequires-python = ">=3.10"\n\n'
       f'[project.scripts]\n{name} = "{pkg}.cli:main"\n\n'
       '[build-system]\nrequires = ["setuptools"]\nbuild-backend = "setuptools.build_meta"\n\n'
       f'[tool.setuptools]\npackages = ["{pkg}"]\n')
    _w(target, f"{pkg}/__init__.py", "")
    _w(target, f"{pkg}/cli.py",
       "import argparse\nimport sys\n\n\n"
       "def main(argv=None):\n"
       "    p = argparse.ArgumentParser()\n"
       "    p.add_argument('name', nargs='?', default='world')\n"
       "    args = p.parse_args(argv)\n"
       "    print(f'hello, {args.name}')\n"
       "    return 0\n\n\n"
       "if __name__ == '__main__':\n"
       "    sys.exit(main())\n")
    _w(target, "tests/test_cli.py",
       f"from {pkg}.cli import main\n\n\n"
       "def test_runs(capsys):\n"
       "    assert main(['there']) == 0\n"
       "    assert 'hello, there' in capsys.readouterr().out\n")
    return {"files": ["pyproject.toml", f"{pkg}/cli.py", "tests/test_cli.py"],
            "next": f"run `python -m {pkg}.cli` or install with `pip install -e .`"}


def _tpl_static(name: str, target: str) -> dict:
    _w(target, "index.html",
       "<!doctype html>\n<html lang=\"en\">\n<head>\n"
       "  <meta charset=\"utf-8\">\n"
       "  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
       f"  <title>{name}</title>\n"
       "  <link rel=\"stylesheet\" href=\"styles.css\">\n</head>\n<body>\n"
       f"  <main><h1>{name}</h1><p>It works.</p></main>\n"
       "  <script src=\"app.js\"></script>\n</body>\n</html>\n")
    _w(target, "styles.css",
       "body { font-family: system-ui, sans-serif; margin: 2rem; }\n"
       "main { max-width: 40rem; margin: 0 auto; }\n")
    _w(target, "app.js", "console.log('ready');\n")
    return {"files": ["index.html", "styles.css", "app.js"],
            "next": "open index.html, or serve with `python -m http.server`"}


_TEMPLATES = {
    "node-express": _tpl_node_express,
    "fastapi": _tpl_fastapi,
    "python-cli": _tpl_python_cli,
    "static": _tpl_static,
}


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------
def cmd_recipes(_args) -> dict:
    return {"recipes": {k: {"method": v["method"], "language": v["language"]}
                        for k, v in RECIPES.items()}}


def cmd_check(args) -> dict:
    r = RECIPES.get(args.stack)
    if not r:
        return {"error": True, "message": f"unknown stack '{args.stack}'",
                "known": list(RECIPES)}
    out = {"stack": args.stack, "method": r["method"], "language": r["language"]}
    if r["method"] == "http_zip":
        out["ready"] = True
        out["note"] = "needs internet to reach start.spring.io"
    elif r["method"] == "cli":
        exe = r.get("cli", "")
        found = _which(exe)
        out["ready"] = bool(found)
        out["cli"] = exe
        out["path"] = found or None
        if not found:
            out["note"] = f"'{exe}' not on PATH — install Node.js or use an offline fallback stack"
    else:
        out["ready"] = True
        out["note"] = "offline embedded template"
    return out


def cmd_run(args) -> dict:
    r = RECIPES.get(args.stack)
    if not r:
        return {"error": True, "message": f"unknown stack '{args.stack}'", "known": list(RECIPES)}
    target = os.path.abspath(args.dir)
    try:
        _ensure_dir(target, args.force)
    except FileExistsError as e:
        return {"error": True, "message": str(e)}

    if r["method"] == "http_zip":
        detail = _run_spring(args.name, target, args)
    elif r["method"] == "cli":
        detail = _run_cli(args.stack, args.name, target, args)
    else:
        detail = _TEMPLATES[args.stack](args.name, target)

    if isinstance(detail, dict) and detail.get("error"):
        return detail
    return {"ok": True, "stack": args.stack, "name": args.name, "dir": target,
            "cmds": {"build": r["build_cmd"], "test": r["test_cmd"], "lint": r["lint_cmd"]},
            "detail": detail}


def _print(obj) -> None:
    out = json.dumps(obj, indent=2, ensure_ascii=False)
    sys.stdout.buffer.write(out.encode("utf-8"))
    sys.stdout.buffer.write(b"\n")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("recipes")

    c = sub.add_parser("check")
    c.add_argument("stack")

    run = sub.add_parser("run")
    run.add_argument("stack")
    run.add_argument("--name", required=True)
    run.add_argument("--dir", required=True)
    run.add_argument("--force", action="store_true")
    run.add_argument("--java-version", default="")
    run.add_argument("--group", default="")
    run.add_argument("--deps", default="")
    run.add_argument("--package-manager", default="")

    args = p.parse_args()
    handler = {"recipes": cmd_recipes, "check": cmd_check, "run": cmd_run}[args.cmd]
    result = handler(args)
    _print(result)
    return 1 if isinstance(result, dict) and result.get("error") else 0


if __name__ == "__main__":
    sys.exit(main())
