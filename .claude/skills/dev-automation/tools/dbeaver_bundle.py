#!/usr/bin/env python3
"""ONE portable DBeaver project ("Kyta") built from the connections of every machine, plus the doc of
how each DB is reached (direct / which port-forward).

    python dbeaver_bundle.py collect --label win [--workspace W]   # copy this machine's projects -> work/dbeaver/sources/<label>/
    python dbeaver_bundle.py check [--only PREFIX] [--timeout 8]   # try each entry's candidate logins (read-only `select 1`)
    python dbeaver_bundle.py build                                 # catalog + sources (+ check) -> work/dbeaver/bundle/Kyta
    python dbeaver_bundle.py install [--workspace W] [--force]     # [WRITE] bundle -> DBeaver workspace (DBeaver must be closed)
    python dbeaver_bundle.py doc [--out F]                          # markdown: every connection + how to reach it

work/dbeaver/ (gitignored; synced by secret_sync like work/secrets):
    sources/<label>/<project>/.dbeaver/{data-sources,credentials-config}.json   raw copies, DBeaver-encrypted
    catalog.json   curated: one entry per real DB target — folder "ENV/product", name, engine, forward
                   (name in platforms.json econtract.forwards) or host/port, database, template + candidates
                   (source connections whose saved login may work for it)
    check.json     last `check`: per entry, which candidate logged in (no secret stored)
    bundle/Kyta/   built project (credentials-config.json encrypted the DBeaver way)
Forwarded entries point at localhost:<the forward's fixed local port>, identical on every machine; start
them with `podfwd.py start-saved --platform econtract [--group ...]`. Credentials are never printed.
"""

import argparse
import copy
import hashlib
import json
import os
import platform
import runpy
import shutil
import socket
import subprocess
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_SKILLS = os.path.dirname(os.path.dirname(_HERE))
runpy.run_path(os.path.join(_SKILLS, "_runtime", "bootstrap.py"))["activate"]()
from agent_skill_toolkit.integrations import dbeaver_store as ds  # noqa: E402

sys.path.insert(0, _HERE)
import mysql_client  # noqa: E402
import postgres_client  # noqa: E402
import probe_common as pc  # noqa: E402
import project_config  # noqa: E402

PLATFORM = "econtract"
CONN_TYPE = {"PROD": "prod", "UAT": "test", "SANDBOX": "test"}   # DBeaver built-in types (prod = confirm + red)


def base_dir() -> str:
    return os.path.join(project_config._work_dir(), "dbeaver")


def _load(name: str, default=None):
    path = os.path.join(base_dir(), name)
    if not os.path.isfile(path):
        if default is not None:
            return default
        raise FileNotFoundError(f"{path} missing")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _save(name: str, obj) -> str:
    path = os.path.join(base_dir(), name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    return path


def forwards() -> dict:
    plat = project_config.load_platform(PLATFORM)
    return {k: v for k, v in (plat.get("forwards") or {}).items() if not k.startswith("_")}


def local_port(fwd: dict) -> int:
    return int(str(fwd["ports"]).split(":")[0])


# --------------------------------------------------------------------------- sources

_cache = {}


def source(src: str) -> dict:
    """'win/General-new' -> read_project (decrypted creds kept in memory only)."""
    if src not in _cache:
        _cache[src] = ds.read_project(os.path.join(base_dir(), "sources", *src.split("/")))
    return _cache[src]


def source_conn(key: str):
    """'win/General-new/<conn id>' -> (connection dict, creds dict)."""
    label, proj, cid = key.split("/", 2)
    p = source(f"{label}/{proj}")
    return p["data"]["connections"][cid], (p["creds"].get(cid) or {})


def collect(label: str, workspace: str) -> dict:
    out = []
    for proj in ds.projects(workspace):
        dst = os.path.join(base_dir(), "sources", label, proj, ".dbeaver")
        os.makedirs(dst, exist_ok=True)
        for f in ("data-sources.json", "credentials-config.json"):
            src = os.path.join(workspace, proj, ".dbeaver", f)
            if os.path.isfile(src):
                shutil.copy2(src, os.path.join(dst, f))
        out.append(proj)
    return {"label": label, "workspace": workspace, "projects": out}


# --------------------------------------------------------------------------- check

def _login(engine: str, host: str, port: int, user: str, password: str, db, timeout: int) -> tuple:
    """('ok'|'auth'|'unsupported'|'unreachable'|'error', short message)."""
    try:
        socket.create_connection((host, port), timeout=min(timeout, 5)).close()
    except OSError as e:
        return "unreachable", str(e)[:120]
    try:
        if engine == "mysql":
            cli = mysql_client.connect(host, port, user, password, db or None, timeout=timeout)
        else:
            cli = postgres_client.connect(host, port, user, password, db or "postgres", timeout=timeout)
        try:
            cli.query("select 1")
        finally:
            cli.close()
        return "ok", ""
    except (mysql_client.MySQLError, postgres_client.PgError, OSError) as e:
        msg = str(e)
        if "caching_sha2" in msg or "not supported" in msg.lower():
            return "unsupported", msg[:120]
        low = msg.lower()
        if any(w in low for w in ("access denied", "password authentication failed", "authentication", "28p01", "1045")):
            return "auth", msg[:120]
        return "error", msg[:120]


def check(only: str = "", timeout: int = 8, label: str = "") -> dict:
    """Results are kept PER MACHINE (`label`, default the hostname): reachability differs by network.
    LOCAL/* entries are skipped — they only mean something on their own machine."""
    cat, fw = _load("catalog.json"), forwards()
    results = _load("check.json", {})
    label = label or socket.gethostname()
    summary = {}
    for e in cat["connections"]:
        if e.get("engine") not in ("mysql", "postgres") or not e["folder"].startswith(only) \
                or e["folder"].startswith("LOCAL"):
            continue
        host, port = ("127.0.0.1", local_port(fw[e["forward"]])) if e.get("forward") else (e["host"], int(e["port"]))
        tried, seen, chosen = [], set(), None
        for key in e["candidates"]:
            _, cred = source_conn(key)
            c = cred.get("#connection") or {}
            sig = (c.get("user"), c.get("password"))
            if not c.get("user") or sig in seen:
                continue
            seen.add(sig)
            status, msg = _login(e["engine"], host, port, c["user"], c.get("password") or "", e.get("database"), timeout)
            tried.append({"candidate": key, "status": status, "message": msg})
            if status == "ok":
                chosen = key
                break
            if status == "unreachable":
                break
        state = "ok" if chosen else (tried[-1]["status"] if tried else "no-credentials")
        results.setdefault(f"{e['folder']}/{e['name']}", {})[label] = {
            "state": state, "chosen": chosen, "tried": tried, "endpoint": f"{host}:{port}",
            "at": time.strftime("%Y-%m-%d %H:%M")}
        summary[state] = summary.get(state, 0) + 1
    _save("check.json", results)
    return {"label": label, "summary": summary, "file": os.path.join(base_dir(), "check.json")}


def _chosen(checked: dict) -> str:
    """The candidate that logged in on any machine ('' if none)."""
    return next((r["chosen"] for r in checked.values() if isinstance(r, dict) and r.get("chosen")), "")


# --------------------------------------------------------------------------- build

def _conn_id(entry: dict, driver: str) -> str:
    return f"{driver}-kyta-{hashlib.sha1((entry['folder'] + '/' + entry['name']).encode()).hexdigest()[:16]}"


def _remote(f: dict) -> str:
    """'svc/haproxy-db-prod' + '25432:5432' -> 'haproxy-db-prod:5432'."""
    return f"{f['target'].split('/', 1)[-1]}:{str(f['ports']).split(':')[1]}"


def display_name(e: dict, fw: dict) -> str:
    """The navigator label says where the bytes go: ':25432 ⇐ haproxy-db-prod:5432 [prod-pg-kyta]'."""
    if e.get("forward"):
        f = fw[e["forward"]]
        return f"{e['name']} · :{local_port(f)} ⇐ {_remote(f)} [{e['forward']}]"
    if e.get("host"):
        return f"{e['name']} · {e['host']}:{e['port']}"
    return e["name"]


def description(e: dict, fw: dict, access: dict) -> str:
    """Connection description (DBeaver: Edit Connection → General): how to reach it and where that is configured."""
    env = e["folder"].split("/")[0]
    if e.get("forward"):
        f = fw[e["forward"]]
        a = access.get(f.get("access", ""), {})
        prod = " --allow-prod" if f.get("prod") else ""
        return "\n".join([
            f"PORT-FORWARD `{e['forward']}`: localhost:{local_port(f)} -> {a.get('namespace')}/{f['target']}:"
            f"{str(f['ports']).split(':')[1]} ({f.get('what', '')})",
            f"k8s: kubeconfig {a.get('kubeconfig')} · context {a.get('context')}",
            f"Config: work/platforms.json -> econtract.forwards.{e['forward']} (access `{f.get('access')}` in "
            "econtract.k8s_access)",
            f"Open (toolkit root): python agentkit.py podfwd start-saved --platform econtract --only {e['forward']}{prod}",
            "Check: python agentkit.py podfwd list · Doc: work/memory/db-connections.md · Catalog: work/dbeaver/catalog.json"])
    if env.startswith(("LOCAL", "LAB")):
        return f"{env}: only on that machine/network. Catalog: work/dbeaver/catalog.json"
    if e.get("host") and "azure.com" in e["host"]:
        return f"PUBLIC {e['host']}:{e['port']} (Azure, customer). Doc: work/memory/db-connections.md"
    if e.get("host"):
        return (f"DIRECT {e['host']}:{e['port']} — no forward; office network (company machine / FortiClient VPN). "
                "Doc: work/memory/db-connections.md")
    return "Catalog: work/dbeaver/catalog.json"


def _creds_for(e: dict, checked: dict) -> dict:
    chosen = _chosen(checked)
    order = ([chosen] if chosen else []) + [e["template"]] + e["candidates"]
    for key in order:
        _, cred = source_conn(key)
        if (cred.get("#connection") or {}).get("user") or (cred.get("#connection") or {}).get("password"):
            return cred
    return {}


def build() -> dict:
    cat, fw = _load("catalog.json"), forwards()
    checked = _load("check.json", {})
    access = project_config.load_platform(PLATFORM).get("k8s_access") or {}
    folders, conns, creds, models, drivers = {}, {}, {}, {}, {}
    for e in cat["connections"]:
        tmpl, _ = source_conn(e["template"])
        c = copy.deepcopy(tmpl)
        cid = _conn_id(e, c.get("driver") or "conn")
        parts = e["folder"].split("/")
        for i in range(len(parts)):
            path = "/".join(parts[:i + 1])
            folders.setdefault(path, {"parent": "/".join(parts[:i])} if i else {})
        c["name"], c["folder"] = display_name(e, fw), e["folder"]
        c["description"] = description(e, fw, access)
        cfg = c.setdefault("configuration", {})
        if e.get("forward"):
            cfg.update(configurationType="MANUAL", host="localhost", port=str(local_port(fw[e["forward"]])))
            cfg.pop("url", None)
            if e.get("database"):
                cfg["database"] = e["database"]
            else:
                cfg.pop("database", None)
        env = parts[0]
        if env in CONN_TYPE:
            cfg["type"] = CONN_TYPE[env]
        conns[cid] = c
        cred = _creds_for(e, checked.get(f"{e['folder']}/{e['name']}", {}))
        if cred:
            creds[cid] = cred
            c["save-password"] = True
        label, proj, old_id = e["template"].split("/", 2)
        vm = (source(f"{label}/{proj}")["data"].get("virtual-models") or {}).get(old_id)
        if vm:
            models[cid] = vm
    for src in {k.rsplit("/", 1)[0] for e in cat["connections"] for k in e["candidates"]}:
        for prov, ds_ in (source(src)["data"].get("drivers") or {}).items():
            drivers.setdefault(prov, {}).update(ds_)
    data = {"folders": folders, "connections": conns}
    if models:
        data["virtual-models"] = models
    if drivers:
        data["drivers"] = drivers
    bundle = os.path.join(base_dir(), "bundle")
    ds.write_project(bundle, cat.get("project", "Kyta"), data, creds,
                     comment="Merged from every machine by dbeaver_bundle.py — edit work/dbeaver/catalog.json, not here")
    return {"project": cat.get("project", "Kyta"), "connections": len(conns), "with_credentials": len(creds),
            "folders": len(folders), "bundle": os.path.join(bundle, cat.get("project", "Kyta"))}


# --------------------------------------------------------------------------- install

def dbeaver_running() -> bool:
    if platform.system() == "Windows":
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq dbeaver.exe"], capture_output=True, text=True).stdout
        return "dbeaver.exe" in out.lower()
    out = subprocess.run(["ps", "-axo", "args="], capture_output=True, text=True).stdout
    return any("dbeaver" in ln.lower() and "dbeaver_bundle" not in ln for ln in out.splitlines())


def install(workspace: str, force: bool = False) -> dict:
    cat = _load("catalog.json")
    name = cat.get("project", "Kyta")
    src = os.path.join(base_dir(), "bundle", name)
    if not os.path.isdir(src):
        raise FileNotFoundError("no bundle — run `dbeaver_bundle.py build` first")
    if dbeaver_running() and not force:
        raise RuntimeError("DBeaver is running — close it first (it rewrites data-sources.json on exit)")
    dst = os.path.join(workspace, name)
    backup = None
    if os.path.isdir(dst):
        backup = os.path.join(project_config._repo_root(), "temp", "dbeaver-backups",
                              time.strftime("%Y%m%d-%H%M%S"), name)
        shutil.copytree(dst, backup)
    proj = ds.read_project(src)
    ds.write_project(workspace, name, proj["data"], proj["creds"],
                     comment="Merged from every machine by dbeaver_bundle.py — edit work/dbeaver/catalog.json, not here")
    out = {"installed": dst, "connections": len(proj["data"].get("connections", {})), "backup": backup}
    if not ds.registered(workspace, name):
        out["next"] = (f"DBeaver does not list `{name}` yet: File > Import > General > Existing Projects into Workspace, "
                       f"root = {workspace}, tick {name}, UNtick 'Copy projects into workspace' (once per machine)")
    return out


# --------------------------------------------------------------------------- doc

def doc() -> str:
    cat, fw = _load("catalog.json"), forwards()
    checked = _load("check.json", {})
    access = project_config.load_platform(PLATFORM).get("k8s_access") or {}
    mark = {"ok": "✅", "auth": "❌ sai login", "unreachable": "⛔ không tới", "unsupported": "🟡 tới được (MySQL 8, chưa thử login)",
            "error": "⚠️ lỗi", "no-credentials": "— chưa lưu login"}
    L = ["---", "name: db-connections",
         "description: Every DB connection (DBeaver project Kyta) by env/product and how to reach it — direct, "
         "public, or which port-forward (fixed ports 1xxxx sandbox / 2xxxx prod / 3xxxx pod); generated by dbeaver_bundle.py doc",
         "metadata:", "  type: reference", "---", "",
         "# Kết nối DB (DBeaver project `Kyta`) — cách vào từng DB", "",
         f"Sinh bởi `dbeaver_bundle.py doc` lúc {time.strftime('%Y-%m-%d %H:%M')} từ `work/dbeaver/catalog.json` "
         "và `work/platforms.json` (econtract.forwards). Không chứa mật khẩu.", "",
         "## Quy ước cổng forward (giống nhau trên mọi máy)", "",
         "- `1xxxx` = sandbox/tool qua `svc/haproxy-service` (ns `fpt-tool`)",
         "- `2xxxx` = **PROD** qua `svc/haproxy-db-prod` (ns `fpt-tool`) — cần `--allow-prod`",
         "- `3xxxx` = pod/svc riêng (idaas UAT, Vitess sandbox)", "",
         "Mở forward (chạy nền, tự nối lại; bind 127.0.0.1):", "```",
         "python agentkit.py podfwd start-saved --platform econtract --group sandbox",
         "python agentkit.py podfwd start-saved --platform econtract --group uat",
         "python agentkit.py podfwd start-saved --platform econtract --group prod --allow-prod",
         "python agentkit.py podfwd start-saved --platform econtract --only sb-pg-kyta,prod-pg-kyta --allow-prod",
         "python agentkit.py podfwd list          # đang chạy?    · podfwd stop <tên|all>", "```", "",
         "| Forward | Cổng local | Đích | k8s (kubeconfig · context) | Là gì |", "|---|---|---|---|---|"]
    for n, f in sorted(fw.items(), key=lambda kv: local_port(kv[1])):
        a = access.get(f["access"], {})
        L.append(f"| `{n}` | **{local_port(f)}** | `{a.get('namespace')}/{f['target']}:{f['ports'].split(':')[1]}` | "
                 f"`{a.get('kubeconfig')}` · `{a.get('context', '')[:60]}` | {f.get('what', '')} |")
    L += ["", "## Kết nối theo môi trường", "",
          "Cột *Vào bằng*: **fwd** = mở forward ở trên rồi nối `localhost:<cổng>`; **trực tiếp** = mạng nội bộ "
          "(máy công ty / VPN FortiClient); **public** = Internet; **LAB/LOCAL** = máy riêng, chỉ chạy trên máy đó. "
          "Cột *Kiểm*: lần `dbeaver_bundle.py check` gần nhất trên từng máy (đăng nhập `select 1`; ⛔ = timeout/cổng đóng "
          "từ máy đó — có thể cần mạng khác hoặc DB đã ngừng; LOCAL không kiểm).", ""]
    cur = None
    for e in sorted(cat["connections"], key=lambda e: (e["folder"].split("/")[0] not in ("PROD", "UAT", "SANDBOX", "SIT"),
                                                         ["PROD", "UAT", "SANDBOX", "SIT"].index(e["folder"].split("/")[0])
                                                         if e["folder"].split("/")[0] in ("PROD", "UAT", "SANDBOX", "SIT") else 9,
                                                         e["folder"], e["name"])):
        env = e["folder"].split("/")[0]
        if env != cur:
            cur = env
            L += ["", f"### {env}", "", "| Thư mục | Kết nối | Engine · DB | Vào bằng | Kiểm | Tên cũ |", "|---|---|---|---|---|---|"]
        if e.get("forward"):
            how = f"fwd `{e['forward']}` → `localhost:{local_port(fw[e['forward']])}`"
        elif env.startswith(("LAB", "LOCAL")):
            how = env
        elif e.get("host") and "azure.com" in e["host"]:
            how = f"public `{e['host']}:{e['port']}`"
        elif e.get("host"):
            how = f"trực tiếp `{e['host']}:{e['port']}`"
        else:
            how = "file / nhúng"
        ck = checked.get(f"{e['folder']}/{e['name']}", {})
        state = " · ".join(h + " " + mark.get(r.get("state"), "") for h, r in sorted(ck.items())) or "—"
        aliases = ", ".join(a.split(":", 1)[1] for a in e.get("aliases", [])[:4])
        L.append(f"| {e['folder']} | {e['name']} | {e.get('engine')} · {e.get('database') or '—'} | {how} | "
                 f"{state} | {aliases} |")
    L += ["", "## Cập nhật", "",
          "1. Sửa `work/dbeaver/catalog.json` (thêm/sửa entry) hoặc `platforms.json` (forward).",
          "2. `dbeaver_bundle.py check` (trên máy vào được mạng) → `build` → đóng DBeaver → `install` trên từng máy.",
          "3. `secret_sync push <host> --only work/dbeaver` để mang sang máy khác, rồi `install` ở đó.",
          "4. `dbeaver_bundle.py doc --out work/memory/db-connections.md` để cập nhật tài liệu này."]
    return "\n".join(L) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="dbeaver_bundle.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("collect")
    p.add_argument("--label", required=True)
    p.add_argument("--workspace", default="")
    p = sub.add_parser("check")
    p.add_argument("--only", default="", help="folder prefix, e.g. PROD or SANDBOX/eDrive")
    p.add_argument("--timeout", type=int, default=8)
    p.add_argument("--label", default="", help="name of this machine in check.json (default: hostname)")
    sub.add_parser("build")
    p = sub.add_parser("install")
    p.add_argument("--workspace", default="")
    p.add_argument("--force", action="store_true", help="install even if a DBeaver process is seen")
    p = sub.add_parser("doc")
    p.add_argument("--out", default="")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "collect":
            out = collect(a.label, a.workspace or ds.default_workspace())
        elif a.cmd == "check":
            out = check(a.only, a.timeout, a.label)
        elif a.cmd == "build":
            out = build()
        elif a.cmd == "install":
            out = install(a.workspace or ds.default_workspace(), a.force)
        else:
            text = doc()
            if a.out:
                with open(a.out, "w", encoding="utf-8", newline="\n") as f:
                    f.write(text)
                out = {"written": a.out}
            else:
                print(text)
                return 0
    except (OSError, ValueError, RuntimeError, KeyError) as e:
        out = {"error": True, "message": f"{type(e).__name__}: {e}"}
    return pc.emit(out)


if __name__ == "__main__":
    sys.exit(main())
