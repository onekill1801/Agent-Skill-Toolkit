#!/usr/bin/env python3
"""What machine-specific context does THIS machine have? (not in git — copied/installed by hand)

Checks, without reading or printing any secret value:
  - .env            : which file is loaded + which feature groups have their keys set
  - work/           : projects.json (clone_dir exists? is a git repo?) and agents.json
                      (account dirs: CLAUDE_CONFIG_DIR / HOME / CODEX_HOME exist?)
  - home dirs       : ~/.ssh keys, kubeconfig ($KUBECONFIG, ~/.kube/config, Lens), DBeaver
                      workspace, GitHub CLI login file, FCHAT_PRIVATE_KEY_PATH
  - features        : each feature -> ready | partial | missing, with what to copy/install

Only existence/non-emptiness is checked: key values and file contents are never read
(the .env is parsed by config.py into this process only, like every other tool).

Usage:
    python machine_context.py            # human summary
    python machine_context.py --json
(also printed by doctor.py)
"""

import argparse
import glob
import json
import os
import platform
import shutil
import sys

try:
    import config  # dev-automation .env loader; also forces UTF-8 stdout
except ImportError:
    config = None

# feature -> .env keys that must be non-empty
ENV_GROUPS = {
    "azure_devops": ["AZURE_DEVOPS_ORG", "AZURE_DEVOPS_PROJECT", "AZURE_DEVOPS_PAT"],
    "gitlab": ["GITLAB_URL", "GITLAB_PRIVATE_TOKEN"],
    "atask": ["ATASK_BASE_URL", "ATASK_PAT_TOKEN"],
    "tchat": ["FCHAT_BASE_API_URL", "FCHAT_BEARER_TOKEN"],
    "telegram": ["TELEGRAM_BOT_TOKEN"],
    "jenkins": ["JENKINS_URL", "JENKINS_TOKEN"],
    "kafka_ui": ["KAFKA_UI_URL", "KAFKA_UI_USER", "KAFKA_UI_PASSWORD"],
    "db_probe": ["DB_USER", "DB_PASSWORD"],
    "vpn": ["FORTIVPN_HOST"],
}
# keys in agents.json account env that point at a directory holding a login
ACCOUNT_DIR_KEYS = ("CLAUDE_CONFIG_DIR", "HOME", "USERPROFILE", "CODEX_HOME")
EXTRA_CLIS = ("claude", "codex", "orca", "gh", "oc")


def _repo_root() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", ".."))


def _work_dir(root: str) -> str:
    return os.environ.get("WORK_DIR") or os.path.join(root, "work")


def _get(key: str) -> str:
    if config is not None:
        return config.get(key)
    return os.environ.get(key, "").strip()


def _expand(path: str) -> str:
    return os.path.expanduser(os.path.expandvars(path or ""))


def _appdata() -> str:
    return os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Roaming")


def _load_json(path: str):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f), None
    except FileNotFoundError:
        return None, "missing"
    except (OSError, ValueError) as e:
        return None, f"unreadable: {e}"


# --------------------------------------------------------------------------- checks

def check_env(root: str) -> dict:
    env_file = os.environ.get("TOOLKIT_ENV_FILE") or os.path.join(root, ".env")
    groups = {}
    for name, keys in ENV_GROUPS.items():
        missing = [k for k in keys if not _get(k)]
        groups[name] = {"ok": not missing, "missing": missing}
    return {"file": env_file, "file_exists": os.path.isfile(env_file), "groups": groups}


def check_projects(root: str) -> dict:
    path = os.path.join(_work_dir(root), "projects.json")
    data, err = _load_json(path)
    if err:
        return {"path": path, "ok": False, "error": err}
    projects = {}
    for name, entry in (data or {}).items():
        if not isinstance(entry, dict) or name.startswith("_"):
            continue
        clone = entry.get("clone_dir")
        info = {"clone_dir": clone}
        if clone:
            d = _expand(clone)
            info["exists"] = os.path.isdir(d)
            info["git"] = os.path.exists(os.path.join(d, ".git"))
        projects[name] = info
    bad = [n for n, i in projects.items() if i.get("clone_dir") and not i.get("git")]
    return {"path": path, "ok": not bad, "count": len(projects), "not_cloned": bad, "projects": projects}


def check_agents(root: str) -> dict:
    path = os.path.join(_work_dir(root), "agents.json")
    data, err = _load_json(path)
    if err:
        return {"path": path, "ok": False, "error": err}
    accounts = {}
    for backend, accs in ((data or {}).get("accounts") or {}).items():
        for acc, spec in (accs or {}).items():
            env = (spec or {}).get("env") or {}
            dirs = {k: _expand(v) for k, v in env.items() if k in ACCOUNT_DIR_KEYS}
            missing = [k for k, d in dirs.items() if not os.path.isdir(d)]
            accounts[f"{backend}@{acc}"] = {"ok": not missing, "missing_dirs": {k: dirs[k] for k in missing}}
    bad = [a for a, i in accounts.items() if not i["ok"]]
    return {"path": path, "ok": bool(accounts) and not bad, "count": len(accounts),
            "unusable": bad, "accounts": accounts}


def check_ssh() -> dict:
    d = os.path.join(os.path.expanduser("~"), ".ssh")
    keys = []
    if os.path.isdir(d):
        for p in glob.glob(os.path.join(d, "id_*")):
            if not p.endswith(".pub") and os.path.isfile(p):
                keys.append(os.path.basename(p))
    return {"dir": d, "ok": bool(keys), "private_keys": sorted(keys),
            "config": os.path.isfile(os.path.join(d, "config")),
            "known_hosts": os.path.isfile(os.path.join(d, "known_hosts"))}


def _registry_kubeconfigs(projects_path: str) -> list:
    data, err = _load_json(projects_path)
    found = []

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k == "kubeconfig" and isinstance(v, str):
                    found.append(v)
                else:
                    walk(v)
        elif isinstance(o, list):
            for x in o:
                walk(x)
    if not err:
        walk(data)
    return sorted(set(found))


def check_kube(root: str) -> dict:
    try:
        import k8s
        lens_dirs, expand = k8s.lens_dirs(), k8s.expand_kubeconfig
    except Exception:  # noqa: BLE001 — k8s.py optional here
        lens_dirs, expand = [], _expand
    env_paths = [p for p in os.environ.get("KUBECONFIG", "").split(os.pathsep) if p]
    default = os.path.join(os.path.expanduser("~"), ".kube", "config")
    candidates = [_expand(p) for p in env_paths] or [default]
    existing = [p for p in candidates if os.path.isfile(p)]
    lens = {}
    for d in lens_dirs:
        files = glob.glob(os.path.join(d, "kubeconfigs", "*"))
        if files:
            lens[d] = len(files)
    registry = {}
    for ref in _registry_kubeconfigs(os.path.join(_work_dir(root), "projects.json")):
        registry[ref] = os.path.isfile(expand(ref))
    reg_missing = [r for r, ok in registry.items() if not ok]
    return {"ok": bool(existing or lens) and not reg_missing, "kubeconfigs": existing,
            "looked_at": candidates, "lens_kubeconfigs": lens,
            "registry_refs": registry, "registry_missing": reg_missing}


def check_dbeaver() -> dict:
    try:
        import dbeaver
        ws = dbeaver.workspace()
    except Exception:  # noqa: BLE001
        ws = os.environ.get("DBEAVER_WORKSPACE", "")
    sources = glob.glob(os.path.join(ws, "*", ".dbeaver", "data-sources*.json")) if ws else []
    return {"workspace": ws, "ok": bool(sources), "data_source_files": len(sources)}


def check_github() -> dict:
    cands = [os.environ.get("GH_CONFIG_DIR", "")]
    if platform.system() == "Windows":
        cands.append(os.path.join(_appdata(), "GitHub CLI"))
    cands.append(os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config"), "gh"))
    hosts = next((os.path.join(c, "hosts.yml") for c in cands
                  if c and os.path.isfile(os.path.join(c, "hosts.yml"))), None)
    return {"gh": shutil.which("gh"), "hosts_file": hosts, "ok": bool(shutil.which("gh") and hosts)}


def check_files() -> dict:
    out = {}
    key_path = _get("FCHAT_PRIVATE_KEY_PATH")
    if key_path:
        out["FCHAT_PRIVATE_KEY_PATH"] = {"path": key_path, "ok": os.path.isfile(_expand(key_path))}
    return out


def check_vpn_client() -> dict:
    if platform.system() == "Windows":
        roots = [os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("ProgramFiles(x86)", "")]
        found = next((os.path.join(r, "Fortinet", "FortiClient") for r in roots
                      if r and os.path.isdir(os.path.join(r, "Fortinet", "FortiClient"))), None)
        return {"client": "FortiClient", "path": found, "ok": bool(found)}
    exe = shutil.which("openfortivpn") or next(
        (p for p in ("/opt/homebrew/bin/openfortivpn", "/usr/local/bin/openfortivpn") if os.path.isfile(p)), None)
    return {"client": "openfortivpn", "path": exe, "ok": bool(exe)}


# --------------------------------------------------------------------------- features

def _feature(ok_parts: dict, hint: str) -> dict:
    missing = [k for k, ok in ok_parts.items() if not ok]
    if not missing:
        state = "ready"
    elif len(missing) == len(ok_parts):
        state = "missing"
    else:
        state = "partial"
    return {"state": state, "missing": missing, "hint": "" if state == "ready" else hint}


def features(r: dict) -> dict:
    g = r["env"]["groups"]
    clis = r["clis"]
    return {
        "azure_devops": _feature({".env AZURE_*": g["azure_devops"]["ok"]}, "copy .env"),
        "gitlab_api": _feature({".env GITLAB_*": g["gitlab"]["ok"]}, "copy .env"),
        "git_clone_push": _feature({"git": clis["git"], "~/.ssh key": r["ssh"]["ok"],
                                    "projects cloned": r["projects"]["ok"]},
                                   "copy ~/.ssh; clone repos into clone_dir (or fix clone_dir in work/projects.json)"),
        "atask": _feature({".env ATASK_*": g["atask"]["ok"]}, "copy .env"),
        "tchat": _feature({".env FCHAT_*": g["tchat"]["ok"],
                           **{k: v["ok"] for k, v in r["files"].items()}},
                          "copy .env (+ file at FCHAT_PRIVATE_KEY_PATH); tokens may need re-login"),
        "telegram": _feature({".env TELEGRAM_BOT_TOKEN": g["telegram"]["ok"]}, "copy .env"),
        "jenkins": _feature({".env JENKINS_*": g["jenkins"]["ok"]}, "copy .env"),
        "kafka_ui": _feature({".env KAFKA_UI_*": g["kafka_ui"]["ok"]}, "copy .env"),
        "k8s": _feature({"kubectl (PATH or Lens)": clis["kubectl"] or bool(r["kube"]["lens_kubeconfigs"]),
                         "kubeconfig": r["kube"]["ok"]},
                        "copy ~/.kube/config and/or <Lens>/kubeconfigs; install kubectl or Lens"),
        "podfwd_fetch": _feature({"oc": clis["oc"]}, "install oc (OpenShift CLI)"),
        "dbeaver_probe": _feature({"DBeaver workspace": r["dbeaver"]["ok"], ".env DB_USER/DB_PASSWORD": g["db_probe"]["ok"]},
                                  "copy DBeaverData/workspace6; set DB_USER/DB_PASSWORD"),
        "vpn": _feature({".env FORTIVPN_HOST": g["vpn"]["ok"], "VPN client": r["vpn_client"]["ok"]},
                        "install FortiClient/openfortivpn; copy .env"),
        "agent_accounts": _feature({"work/agents.json": r["agents"]["ok"], "claude CLI": clis["claude"]},
                                   "copy work/agents.json and fix account dirs; log in to each account (Orca)"),
        "codex_review": _feature({"codex CLI": clis["codex"]}, "install codex; copy ~/.codex or codex login"),
        "github": _feature({"gh login": r["github"]["ok"]}, "install gh; gh auth login"),
    }


def collect(root: str = None) -> dict:
    root = root or os.environ.get("TOOLKIT_ROOT") or _repo_root()
    r = {
        "root": root,
        "env": check_env(root),
        "projects": check_projects(root),
        "agents": check_agents(root),
        "ssh": check_ssh(),
        "kube": check_kube(root),
        "dbeaver": check_dbeaver(),
        "github": check_github(),
        "files": check_files(),
        "vpn_client": check_vpn_client(),
        "clis": {c: bool(shutil.which(c)) for c in ("git", "kubectl") + EXTRA_CLIS},
    }
    r["features"] = features(r)
    return r


MARK = {"ready": "✓", "partial": "~", "missing": "·"}


def human(r: dict) -> str:
    env = r["env"]
    lines = ["Bối cảnh máy (thứ không nằm trong git):",
             f"  .env        : {'có' if env['file_exists'] else 'KHÔNG CÓ'} — {env['file']}"]
    p = r["projects"]
    if p.get("error"):
        lines.append(f"  projects    : {p['error']} — {p['path']}")
    else:
        lines.append(f"  projects    : {p['count']} project"
                     + (f", chưa clone: {', '.join(p['not_cloned'])}" if p["not_cloned"] else ", đã clone đủ"))
    a = r["agents"]
    if a.get("error"):
        lines.append(f"  agents.json : {a['error']} — {a['path']}")
    else:
        lines.append(f"  agents.json : {a['count']} tài khoản"
                     + (f", thiếu thư mục login: {', '.join(a['unusable'])}" if a["unusable"] else ", đủ thư mục login"))
    s = r["ssh"]
    lines.append(f"  ~/.ssh      : {', '.join(s['private_keys']) or 'không có private key'}")
    k = r["kube"]
    kube_desc = ", ".join(k["kubeconfigs"]) or "không có"
    if k["lens_kubeconfigs"]:
        kube_desc += f" · Lens: {sum(k['lens_kubeconfigs'].values())} file"
    if k["registry_missing"]:
        kube_desc += f" · registry trỏ tới file không có: {', '.join(k['registry_missing'])}"
    lines.append(f"  kubeconfig  : {kube_desc}")
    d = r["dbeaver"]
    lines.append(f"  DBeaver     : {d['data_source_files']} file data-sources — {d['workspace']}")
    gh = r["github"]
    lines.append(f"  GitHub CLI  : {'đã login' if gh['ok'] else ('có gh, chưa login' if gh['gh'] else 'chưa cài gh')}")
    for key, f in r["files"].items():
        lines.append(f"  {key}: {'có' if f['ok'] else 'KHÔNG CÓ'} — {f['path']}")
    lines += ["", "Tính năng (✓ sẵn sàng · ~ thiếu một phần · · chưa có):"]
    for name, f in r["features"].items():
        tail = f"  thiếu: {', '.join(f['missing'])} → {f['hint']}" if f["missing"] else ""
        lines.append(f"  {MARK[f['state']]} {name:<15}{tail}".rstrip())
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Machine-specific context check (no secret values read)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    r = collect()
    print(json.dumps(r, ensure_ascii=False, indent=2) if args.json else human(r))
    return 0


if __name__ == "__main__":
    sys.exit(main())
