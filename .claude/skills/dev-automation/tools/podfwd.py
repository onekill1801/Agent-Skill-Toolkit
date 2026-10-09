#!/usr/bin/env python3
"""Port-forward pods/services to localhost (oc or kubectl) and pull files from pods.

Uses `oc` when available (OpenShift), else `kubectl`. Target resolution is shared with
k8s.py: `--project P --env E` (projects.json environments.<env>.k8s) or explicit
`--context C [--kubeconfig F] [-n NS]`. Never uses the current-context.

Each forward runs in a detached SUPERVISOR that restarts the tunnel when it drops
(pod restart, network blip); a `-l selector` target is re-resolved to a running pod on
every restart. Forwards bind to 127.0.0.1 only.

    python podfwd.py start <svc/x|pod/x|deploy/x|-l app=db> <local>:<remote> [--name N] [target opts]
    python podfwd.py start-saved --project P --env E      # environments.<env>.forwards in projects.json
    python podfwd.py list
    python podfwd.py stop <name|all>
    python podfwd.py logs <name> [--tail 40]
    python podfwd.py fetch <pod|deploy/x|-l sel> <path-in-pod> [--to DIR] [-c container]   # pod -> local only

projects.json:
    "environments": {"uat": {"k8s": {"context": "...", "namespace": "fis-mbf-idaas-uat"},
                             "forwards": [{"name": "idaas-db-uat", "target": "pod/idaas-db-57-0",
                                           "ports": "13307:3306"}]}}

Protected envs (project_config protected_envs, or a context/namespace containing "prod")
need --allow-prod. Output: one JSON object (except `logs`).
"""

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time

import k8s
import probe_common as pc
import project_config

HOME = os.path.expanduser("~")


def _state_dir() -> str:
    d = os.path.join(project_config._repo_root(), "temp", "port_forwards")
    os.makedirs(d, exist_ok=True)
    return d


def _state_path(name: str) -> str:
    return os.path.join(_state_dir(), re.sub(r"[^A-Za-z0-9_.-]", "_", name) + ".json")


def _load(name: str) -> dict:
    try:
        with open(_state_path(name), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save(st: dict) -> None:
    tmp = _state_path(st["name"]) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _state_path(st["name"]))


def _alive(pid) -> bool:
    if not pid:
        return False
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True).stdout
        return str(pid) in out
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


def _cli() -> str:
    return os.environ.get("OC_BIN") or shutil.which("oc") or k8s.find_kubectl()


def _base(t: dict) -> list:
    cmd = [_cli()]
    if t.get("kubeconfig"):
        cmd += ["--kubeconfig", t["kubeconfig"]]
    cmd += ["--context", t["context"]]
    if t.get("namespace"):
        cmd += ["-n", t["namespace"]]
    return cmd


def _protected(args, t: dict) -> bool:
    ctx = getattr(args, "_ctx", None)
    if getattr(args, "project", None):
        try:
            ctx = project_config.resolve(args.project, getattr(args, "env", None))
        except project_config.ResolveError:
            ctx = None
    if ctx and ctx.get("protected"):
        return True
    # Only the namespace / the context's namespace part: OpenShift context names embed the
    # API host (e.g. ".../api-prod01-fis-cloud-fpt-com:6443/..."), which says nothing about the env.
    scope = t.get("namespace") or (t.get("context") or "").split("/")[0]
    return bool(re.search(r"(^|[-_.])(prod|prd|production)([-_.]|$)", scope, re.I))


def _resolve_pod(t: dict, selector: str) -> str:
    out = subprocess.run(_base(t) + ["get", "pods", "-l", selector, "--field-selector=status.phase=Running",
                                     "-o", "jsonpath={.items[0].metadata.name}"],
                         capture_output=True, text=True, timeout=30)
    if out.returncode != 0 or not out.stdout.strip():
        raise k8s.K8sError("not_found", f"no running pod for -l {selector}: {(out.stderr or '').strip()[-200:]}")
    return "pod/" + out.stdout.strip()


def _ports_ok(ports: str) -> bool:
    return bool(re.fullmatch(r"\d{2,5}:\d{2,5}", ports or ""))


# --------------------------------------------------------------------------- commands

def start(args, target: str, ports: str, name: str = "") -> dict:
    if not _ports_ok(ports):
        raise k8s.K8sError("usage", "ports must be <local>:<remote>, e.g. 13307:3306")
    t = k8s.resolve_target(args)
    if _protected(args, t) and not getattr(args, "allow_prod", False):
        raise k8s.K8sError("forbidden", f"{t['context']} / {t.get('namespace')} looks like PROD — pass --allow-prod")
    local = int(ports.split(":")[0])
    name = name or f"{(t.get('namespace') or 'ns')}-{target.replace('/', '-').replace('=', '-')}-{local}"
    old = _load(name)
    if old and _alive(old.get("pid")):
        return {"name": name, "already_running": True, "pid": old["pid"], "listening": k8s._port_open(local)}
    if k8s._port_open(local):
        raise k8s.K8sError("usage", f"local port {local} is already in use")
    st = {"name": name, "target": target, "ports": ports, "local_port": local, "target_spec": t,
          "selector": target[3:].strip() if target.startswith("-l") else None,
          "started": int(time.time()), "restarts": 0, "cli": os.path.basename(_cli()),
          "log": _state_path(name)[:-5] + ".log"}
    _save(st)
    cmd = [sys.executable, os.path.abspath(__file__), "_supervise", name]
    kw = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        kw["creationflags"] = 0x00000008 | 0x00000200   # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    proc = subprocess.Popen(cmd, **kw)
    st["pid"] = proc.pid
    _save(st)
    for _ in range(40):                                  # wait up to ~10 s for the tunnel
        if k8s._port_open(local):
            break
        time.sleep(0.25)
    up = k8s._port_open(local)
    out = {"name": name, "pid": proc.pid, "listening": up, "local": f"127.0.0.1:{local}",
           "target": f"{t['context']} / {t.get('namespace')} / {target}"}
    if not up:
        out["hint"] = f"not up yet — see `podfwd.py logs {name}` (auth expired? `oc login` again)"
    return out


def supervise(name: str) -> None:
    """Detached loop: (re)start the port-forward until `stop` removes the state file."""
    stop = {"flag": False}
    child = {"p": None}

    def _term(*_):
        stop["flag"] = True
        if child["p"] and child["p"].poll() is None:
            child["p"].terminate()

    signal.signal(signal.SIGTERM, _term)
    backoff = 1
    while not stop["flag"]:
        st = _load(name)
        if not st:
            return
        t = st["target_spec"]
        with open(st["log"], "a", encoding="utf-8") as log:
            try:
                target = _resolve_pod(t, st["selector"]) if st.get("selector") else st["target"]
                argv = _base(t) + ["port-forward", "--address", "127.0.0.1", target, st["ports"]]
                log.write(f"[{time.strftime('%H:%M:%S')}] start {target} {st['ports']}\n")
                log.flush()
                started = time.time()
                child["p"] = subprocess.Popen(argv, stdout=log, stderr=log, stdin=subprocess.DEVNULL)
                child["p"].wait()
                if time.time() - started > 60:
                    backoff = 1                          # it ran fine for a while → reset
            except Exception as e:  # noqa: BLE001
                log.write(f"[{time.strftime('%H:%M:%S')}] error: {e}\n")
        if stop["flag"] or not _load(name):
            return
        st = _load(name)
        st["restarts"] = st.get("restarts", 0) + 1
        st["last_restart"] = int(time.time())
        _save(st)
        time.sleep(backoff)
        backoff = min(backoff * 2, 30)


def list_all() -> dict:
    rows = []
    for f in sorted(os.listdir(_state_dir())):
        if not f.endswith(".json"):
            continue
        st = _load(f[:-5])
        if not st:
            continue
        t = st.get("target_spec") or {}
        rows.append({"name": st["name"], "local": f"127.0.0.1:{st['local_port']}", "target": st["target"],
                     "context": t.get("context"), "namespace": t.get("namespace"),
                     "supervisor_alive": _alive(st.get("pid")), "listening": k8s._port_open(st["local_port"]),
                     "restarts": st.get("restarts", 0)})
    return {"count": len(rows), "forwards": rows}


def stop(name: str) -> dict:
    names = [r["name"] for r in list_all()["forwards"]] if name == "all" else [name]
    done = []
    for n in names:
        st = _load(n)
        if not st:
            done.append({"name": n, "error": "not found"})
            continue
        try:
            os.remove(_state_path(n))                    # supervisor exits when its state disappears
        except OSError:
            pass
        pid = st.get("pid")
        if pid and _alive(pid):
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
            else:
                try:
                    os.killpg(os.getpgid(int(pid)), signal.SIGTERM)
                except OSError:
                    pass
        done.append({"name": n, "stopped": True})
    return {"stopped": done}


def start_saved(args) -> dict:
    ctx = project_config.resolve(args.project, args.env)
    fwds = ctx["stack"].get("forwards") or []
    if not fwds:
        raise k8s.K8sError("config", f"no `forwards` for {args.project} env {ctx['env']} in projects.json")
    return {"started": [start(args, f["target"], f["ports"], f.get("name", "")) for f in fwds]}


def fetch(args) -> dict:
    t = k8s.resolve_target(args)
    if _protected(args, t) and not args.allow_prod:
        raise k8s.K8sError("forbidden", "PROD target — pass --allow-prod to copy files from it")
    pod = args.pod
    if pod.startswith("-l"):
        pod = _resolve_pod(t, pod[2:].strip())
    elif pod.startswith(("deploy/", "deployment/", "dc/", "sts/")):
        pod = _resolve_pod(t, k8s.selector_for(t, pod))
    pod = pod.split("/", 1)[-1]
    dest = args.to or os.path.join(project_config._work_dir(), "pods", t.get("namespace") or "ns", pod)
    os.makedirs(dest, exist_ok=True)
    cli = _cli()
    if os.path.basename(cli).startswith("oc"):
        argv = _base(t) + ["rsync", f"{pod}:{args.path}", dest, "--no-perms=true"]
    else:
        argv = _base(t) + ["cp", f"{pod}:{args.path}", os.path.join(dest, os.path.basename(args.path.rstrip('/')))]
    if args.container:
        argv += ["-c", args.container]
    p = subprocess.run(argv, capture_output=True, text=True, timeout=600)
    if p.returncode != 0:
        raise k8s.classify(p.stderr, p.returncode)
    files = sum(len(fs) for _, _, fs in os.walk(dest))
    return {"pod": pod, "path": args.path, "to": dest.replace(HOME, "~"), "files": files}


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if argv[:1] == ["_supervise"]:
        supervise(argv[1])
        return 0
    ap = argparse.ArgumentParser(prog="podfwd.py")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def topts(p):
        p.add_argument("--project")
        p.add_argument("--env")
        p.add_argument("--context")
        p.add_argument("--kubeconfig")
        p.add_argument("-n", "--namespace")
        p.add_argument("--insecure", action="store_true")
        p.add_argument("--allow-prod", action="store_true")
        return p

    p = topts(sub.add_parser("start"))
    p.add_argument("target", help="svc/x | pod/x | deploy/x | '-l app=db'")
    p.add_argument("ports", help="<local>:<remote>")
    p.add_argument("--name", default="")
    topts(sub.add_parser("start-saved"))
    sub.add_parser("list")
    p = sub.add_parser("stop")
    p.add_argument("name")
    p = sub.add_parser("logs")
    p.add_argument("name")
    p.add_argument("--tail", type=int, default=40)
    p = topts(sub.add_parser("fetch"))
    p.add_argument("pod")
    p.add_argument("path")
    p.add_argument("--to", default="")
    p.add_argument("-c", "--container", default="")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "start":
            out = start(a, a.target, a.ports, a.name)
        elif a.cmd == "start-saved":
            out = start_saved(a)
        elif a.cmd == "list":
            out = list_all()
        elif a.cmd == "stop":
            out = stop(a.name)
        elif a.cmd == "logs":
            st = _load(a.name)
            try:
                with open(st.get("log", ""), encoding="utf-8", errors="replace") as f:
                    print("".join(f.readlines()[-a.tail:]), end="")
                return 0
            except OSError:
                out = {"error": True, "message": f"no log for {a.name}"}
        else:
            out = fetch(a)
    except k8s.K8sError as e:
        out = e.to_dict()
    except project_config.ResolveError as e:
        out = {"error": True, "kind": "config", "message": str(e)}
    return pc.emit(out)


if __name__ == "__main__":
    sys.exit(main())
