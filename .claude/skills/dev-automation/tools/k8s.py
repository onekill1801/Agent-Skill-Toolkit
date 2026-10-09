#!/usr/bin/env python3
"""Kubernetes / OpenShift READ-ONLY client for the agent (wraps kubectl).

Lens does nothing magic: it runs kubectl against a kubeconfig. This tool does the
same — so it reuses the kubeconfigs (and, as fallback, the kubectl binaries) Lens
already has on the machine. Works on Windows, macOS and Linux.

READ-ONLY by construction: every kubectl call goes through `_kubectl()`, which
refuses any verb outside READ_VERBS. Write ops (restart/rollout/patch) are NOT in
this version.

Target selection (never relies on kubectl's current-context — avoids hitting the
wrong cluster):
    --project P --env E      reads projects.json -> environments.<E>.k8s (see below)
    --context C [--kubeconfig F] [-n NS]   explicit; overrides the registry

projects.json:
    "etask": {"environments": {"dev": {"k8s": {
        "kubeconfig": "~/.kube/config",      # or "lens:<file>" = Lens' pasted kubeconfigs dir
        "context":    "fis-mbf-idaas-dev/api-...:6443/system:serviceaccount:...",
        "namespace":  "fis-mbf-idaas-dev",
        "kubectl_version": "1.28",           # optional: pick Lens' bundled kubectl of that minor
        "insecure_skip_tls_verify": false    # optional, last resort (rotated self-signed CA) — warn the user
    }}}}

kubectl lookup: $KUBECTL_BIN > PATH > Lens bundled binaries (matching kubectl_version,
else newest).

Usage:
    python k8s.py doctor                                  # kubectl + kubeconfigs found on this machine
    python k8s.py contexts [--kubeconfig F]               # contexts in a kubeconfig (no network)
    python k8s.py access   --project etask --env dev      # who am I + what may I do (auth can-i)
    python k8s.py pods     --project etask --env dev [-l app=x]
    python k8s.py deploys  --project etask --env dev
    python k8s.py events   --project etask --env dev [--for <name>] [--warnings]
    python k8s.py describe --project etask --env dev pod/<name> | deploy/<name>
    python k8s.py logs     --project etask --env dev <pod | deploy/NAME | -l k=v>
                           [--since 30m] [--tail 200] [--previous] [--errors] [--grep REGEX] [-c container]
    python k8s.py get      --project etask --env dev <kind> [name]     # generic, JSON trimmed

Output: one JSON object. Errors: {"error": true, "kind": auth|forbidden|not_found|
tls|unreachable|timeout|kubectl_missing|config|usage, "message", "hint"}.
"""

import argparse
import atexit
import glob
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

import probe_common as pc  # noqa: F401  (forces UTF-8 stdout on Windows)
import project_config

READ_VERBS = {"get", "describe", "logs", "auth", "version", "config", "top", "api-resources"}
DEFAULT_TIMEOUT = 40          # seconds per kubectl call
MAX_LOG_LINES_OUT = 400       # cap lines returned to the agent (token budget)
# Plain text (Java/Spring), JSON ("level":"error"), and glog (E1009 12:00:00.123 ...) styles.
ERROR_PATTERNS = re.compile(r'\b(ERROR|FATAL|Exception|Caused by|panic|OOMKilled)\b'
                            r'|"level"\s*:\s*"(error|fatal|panic|dpanic)"|(^|\s)[EF]\d{4} \d\d:\d\d', re.I)
WARN_PATTERNS = re.compile(r'\bWARN(ING)?\b|"level"\s*:\s*"warn(ing)?"|(^|\s)W\d{4} \d\d:\d\d', re.I)


class K8sError(Exception):
    def __init__(self, kind: str, message: str, hint: str = ""):
        super().__init__(message)
        self.kind, self.message, self.hint = kind, message, hint

    def to_dict(self) -> dict:
        d = {"error": True, "kind": self.kind, "message": self.message}
        if self.hint:
            d["hint"] = self.hint
        return d


# --------------------------------------------------------------------------- paths

def lens_dirs() -> list:
    """Candidate Lens-family config dirs for this OS (Lens, OpenLens, Freelens)."""
    system = platform.system()
    home = os.path.expanduser("~")
    if system == "Windows":
        base = os.environ.get("APPDATA") or os.path.join(home, "AppData", "Roaming")
    elif system == "Darwin":
        base = os.path.join(home, "Library", "Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
    return [os.path.join(base, n) for n in ("Lens", "OpenLens", "Freelens")]


def expand_kubeconfig(path: str) -> str:
    """'~/x' -> home path; 'lens:<file>' -> <Lens>/kubeconfigs/<file> (first that exists)."""
    if not path:
        return ""
    if path.startswith("lens:"):
        name = path[len("lens:"):]
        cands = [os.path.join(d, "kubeconfigs", name) for d in lens_dirs()]
        return next((c for c in cands if os.path.isfile(c)), cands[0])
    return os.path.expanduser(os.path.expandvars(path))


def _version_key(v: str):
    return tuple(int(x) for x in re.findall(r"\d+", v)) or (0,)


def lens_kubectls() -> dict:
    """{minor_version: path} of kubectl binaries Lens has downloaded."""
    exe = "kubectl.exe" if platform.system() == "Windows" else "kubectl"
    found = {}
    for d in lens_dirs():
        for p in glob.glob(os.path.join(d, "binaries", "kubectl", "*", exe)):
            found.setdefault(os.path.basename(os.path.dirname(p)), p)
    return dict(sorted(found.items(), key=lambda kv: _version_key(kv[0])))


def find_kubectl(want_version: str = "", explicit: str = "") -> str:
    if explicit:
        return explicit
    if os.environ.get("KUBECTL_BIN"):
        return os.environ["KUBECTL_BIN"]
    lens = lens_kubectls()
    if want_version and want_version in lens:
        return lens[want_version]
    on_path = shutil.which("kubectl")
    if on_path:
        return on_path
    if lens:
        return list(lens.values())[-1]
    raise K8sError("kubectl_missing", "kubectl not found (PATH, KUBECTL_BIN, Lens binaries)",
                   "install kubectl or open Lens once so it downloads its bundled kubectl")


# --------------------------------------------------------------------------- target

def resolve_target(args) -> dict:
    """Merge registry (--project/--env) with explicit flags. Explicit wins."""
    reg = {}
    if getattr(args, "project", None):
        try:
            ctx = project_config.resolve(args.project, getattr(args, "env", None))
        except project_config.ResolveError as e:
            raise K8sError("config", str(e))
        reg = ctx["stack"].get("k8s") or {}
        if not reg and not args.context:
            raise K8sError("config", f"no k8s block for project '{args.project}' env '{ctx['env']}'",
                           "add environments.<env>.k8s {kubeconfig, context, namespace} to work/projects.json")
    t = {
        "kubeconfig": expand_kubeconfig(args.kubeconfig or reg.get("kubeconfig", "")),
        "context": args.context or reg.get("context", ""),
        "namespace": args.namespace or reg.get("namespace", ""),
        "kubectl_version": reg.get("kubectl_version", ""),
        "insecure": bool(getattr(args, "insecure", False) or reg.get("insecure_skip_tls_verify")),
    }
    if not t["context"]:
        raise K8sError("usage", "no target: pass --project/--env or --context",
                       "never falls back to kubectl's current-context on purpose")
    if t["kubeconfig"] and not os.path.isfile(t["kubeconfig"]):
        raise K8sError("config", f"kubeconfig not found: {t['kubeconfig']}")
    t["kubectl"] = find_kubectl(t["kubectl_version"], reg.get("kubectl", ""))
    if t["insecure"]:
        t["kubeconfig"] = insecure_kubeconfig(t)
    return t


def insecure_kubeconfig(t: dict) -> str:
    """kubectl rejects --insecure-skip-tls-verify while the cluster still carries a CA, so
    write a minified temp copy (0600, deleted at exit) with the CA stripped. Source untouched."""
    cmd = [t["kubectl"]] + (["--kubeconfig", t["kubeconfig"]] if t["kubeconfig"] else []) + \
          ["config", "view", "--raw", "--minify", "--flatten", "--context", t["context"], "-o", "json"]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    if p.returncode:
        raise classify(p.stderr, p.returncode)
    cfg = json.loads(p.stdout)
    for c in cfg.get("clusters", []):
        c["cluster"].pop("certificate-authority-data", None)
        c["cluster"].pop("certificate-authority", None)
        c["cluster"]["insecure-skip-tls-verify"] = True
    fd, path = tempfile.mkstemp(prefix="k8s-insecure-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(cfg, f)
    atexit.register(lambda: os.path.exists(path) and os.remove(path))
    t["insecure"] = False  # the temp kubeconfig already says so; don't pass the flag twice
    return path


# --------------------------------------------------------------------------- kubectl

def classify(stderr: str, rc: int) -> K8sError:
    s = stderr or ""
    low = s.lower()
    last = next((ln for ln in reversed(s.strip().splitlines()) if ln.strip()), s.strip())[:500]
    if "unauthorized" in low or "provide credentials" in low or "must be logged in" in low:
        return K8sError("auth", last,
                        "token expired/revoked — get a new one (Rancher: Download KubeConfig; OpenShift: oc login / new SA token) and update the kubeconfig")
    if "forbidden" in low:
        return K8sError("forbidden", last, "this identity lacks RBAC for that resource; run `k8s.py access`")
    if "notfound" in low.replace(" ", "") or "not found" in low:
        return K8sError("not_found", last)
    if "x509" in low or "certificate" in low:
        return K8sError("tls", last,
                        "cluster CA in the kubeconfig no longer matches the server (e.g. Rancher rotated its CA): "
                        "download a fresh kubeconfig; last resort: insecure_skip_tls_verify in the registry")
    if any(x in low for x in ("connection refused", "no such host", "i/o timeout",
                              "unable to connect", "dial tcp", "tls handshake timeout")):
        return K8sError("unreachable", last, "check VPN / network to the API server")
    if "context" in low and "does not exist" in low:
        return K8sError("config", last, "run `k8s.py contexts` to list valid names")
    return K8sError("kubectl", last or f"kubectl exited {rc}")


def _base_cmd(t: dict, namespaced: bool = True) -> list:
    cmd = [t["kubectl"]]
    if t.get("kubeconfig"):
        cmd += ["--kubeconfig", t["kubeconfig"]]
    cmd += ["--context", t["context"]]
    if namespaced and t.get("namespace"):
        cmd += ["-n", t["namespace"]]
    return cmd


def _kubectl(t: dict, args: list, namespaced: bool = True, timeout: int = DEFAULT_TIMEOUT,
             ok_rc=(0,)) -> str:
    """Run a READ-ONLY kubectl command; return stdout or raise K8sError."""
    verb = args[0] if args else ""
    if verb not in READ_VERBS:
        raise K8sError("usage", f"refusing non-read kubectl verb '{verb}' (read-only tool)")
    if verb == "config" and args[1:2] not in (["view"], ["get-contexts"]):
        raise K8sError("usage", "only `config view|get-contexts` are allowed")
    cmd = _base_cmd(t, namespaced) + ["--request-timeout", f"{max(timeout - 5, 5)}s"] + args
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        raise K8sError("kubectl_missing", f"cannot execute {t['kubectl']}")
    except subprocess.TimeoutExpired:
        raise K8sError("timeout", f"kubectl {verb} timed out after {timeout}s", "check VPN / API server")
    if p.returncode not in ok_rc:
        raise classify(p.stderr, p.returncode)
    return p.stdout


def _json(t, args, namespaced=True) -> dict:
    return json.loads(_kubectl(t, args + ["-o", "json"], namespaced) or "{}")


# --------------------------------------------------------------------------- helpers

def _age(ts: str) -> str:
    if not ts:
        return ""
    try:
        dt = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return ts
    s = int((datetime.now(timezone.utc) - dt).total_seconds())
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if s >= n:
            return f"{s // n}{unit}"
    return f"{s}s"


def summarize_pod(p: dict) -> dict:
    st = p.get("status", {})
    cs = st.get("containerStatuses") or []
    ready = sum(1 for c in cs if c.get("ready"))
    problems = []
    for c in cs:
        w = (c.get("state") or {}).get("waiting")
        if w:
            problems.append(f"{c['name']}: {w.get('reason')}")
        lt = (c.get("lastState") or {}).get("terminated")
        if lt and lt.get("reason") not in (None, "Completed"):
            problems.append(f"{c['name']} last exit: {lt.get('reason')} (code {lt.get('exitCode')})")
    return {
        "name": p["metadata"]["name"],
        "phase": st.get("phase"),
        "ready": f"{ready}/{len(cs)}",
        "restarts": sum(c.get("restartCount", 0) for c in cs),
        "age": _age(p["metadata"].get("creationTimestamp")),
        "node": p.get("spec", {}).get("nodeName"),
        "images": [c.get("image") for c in p.get("spec", {}).get("containers", [])],
        "problems": problems,
    }


def summarize_deploy(d: dict) -> dict:
    spec, st = d.get("spec", {}), d.get("status", {})
    conds = {c["type"]: c for c in st.get("conditions", [])}
    prog = conds.get("Progressing", {})
    return {
        "name": d["metadata"]["name"],
        "kind": d.get("kind"),
        "ready": f"{st.get('readyReplicas', 0)}/{spec.get('replicas', 0)}",
        "updated": st.get("updatedReplicas", 0),
        "available": st.get("availableReplicas", 0),
        "images": [c.get("image") for c in spec.get("template", {}).get("spec", {}).get("containers", [])],
        "age": _age(d["metadata"].get("creationTimestamp")),
        "rollout": prog.get("reason"),
        "selector": (spec.get("selector") or {}).get("matchLabels") or spec.get("selector"),
    }


def selector_for(t: dict, target: str) -> str:
    """deploy/NAME (or dc/, sts/, ds/) -> 'k=v,k2=v2' label selector of its pods."""
    obj = _json(t, ["get", target])
    sel = (obj.get("spec") or {}).get("selector") or {}
    labels = sel.get("matchLabels", sel) if isinstance(sel, dict) else {}
    if not labels or not all(isinstance(v, str) for v in labels.values()):
        raise K8sError("usage", f"{target} has no simple label selector; pass -l explicitly")
    return ",".join(f"{k}={v}" for k, v in sorted(labels.items()))


def filter_lines(lines: list, pattern: str, context: int = 0, flags: int = 0) -> list:
    rx = re.compile(pattern, flags)
    keep = set()
    for i, ln in enumerate(lines):
        if rx.search(ln):
            keep.update(range(max(0, i - context), min(len(lines), i + context + 1)))
    return [lines[i] for i in sorted(keep)]


# --------------------------------------------------------------------------- commands

def cmd_doctor(args) -> dict:
    kubectl = None
    try:
        kubectl = find_kubectl()
    except K8sError:
        pass
    version = None
    if kubectl:
        try:
            p = subprocess.run([kubectl, "version", "--client", "-o", "json"], capture_output=True,
                               text=True, timeout=15)
            version = json.loads(p.stdout).get("clientVersion", {}).get("gitVersion")
        except Exception:  # noqa: BLE001
            pass
    default_kc = os.environ.get("KUBECONFIG") or os.path.join(os.path.expanduser("~"), ".kube", "config")
    lens_kcs = []
    for d in lens_dirs():
        lens_kcs += [f"lens:{os.path.basename(p)}" for p in glob.glob(os.path.join(d, "kubeconfigs", "*"))
                     if os.path.isfile(p)]
    return {"os": platform.system(), "kubectl": kubectl, "kubectl_version": version,
            "lens_kubectls": lens_kubectls(),
            "default_kubeconfig": default_kc if os.path.isfile(default_kc.split(os.pathsep)[0]) else None,
            "lens_kubeconfigs": lens_kcs}


def cmd_contexts(args) -> dict:
    kc = expand_kubeconfig(args.kubeconfig or "")
    t = {"kubectl": find_kubectl(), "kubeconfig": kc, "context": ""}
    cmd = [t["kubectl"]] + (["--kubeconfig", kc] if kc else []) + ["config", "view", "-o", "json"]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    if p.returncode:
        raise classify(p.stderr, p.returncode)
    cfg = json.loads(p.stdout or "{}")
    clusters = {c["name"]: c["cluster"].get("server") for c in cfg.get("clusters", [])}
    out = [{"name": c["name"], "namespace": c["context"].get("namespace"),
            "user": c["context"].get("user"), "server": clusters.get(c["context"].get("cluster"))}
           for c in cfg.get("contexts", [])]
    return {"kubeconfig": kc or "(default)", "count": len(out), "contexts": out}


def cmd_access(args) -> dict:
    t = resolve_target(args)
    who = None
    try:
        who = json.loads(_kubectl(t, ["auth", "whoami", "-o", "json"], namespaced=False)) \
            .get("status", {}).get("userInfo", {}).get("username")
    except K8sError as e:
        if e.kind in ("auth", "unreachable", "timeout"):
            raise
    checks = {}
    for verb, res in (("get", "pods"), ("get", "pods/log"), ("get", "deployments"), ("get", "events"),
                      ("get", "configmaps"), ("get", "secrets"), ("create", "pods/exec"),
                      ("create", "pods/portforward"), ("patch", "deployments"), ("update", "configmaps")):
        out = _kubectl(t, ["auth", "can-i", verb, res], ok_rc=(0, 1)).strip()
        checks[f"{verb} {res}"] = out == "yes"
    return {"context": t["context"], "namespace": t["namespace"], "user": who, "can": checks}


def cmd_pods(args) -> dict:
    t = resolve_target(args)
    q = ["get", "pods"] + (["-l", args.selector] if args.selector else [])
    pods = [summarize_pod(p) for p in _json(t, q).get("items", [])]
    def healthy_now(p):
        if p["phase"] == "Succeeded":
            return True
        r, n = p["ready"].split("/")
        return p["phase"] == "Running" and r == n
    unhealthy = [p["name"] for p in pods if not healthy_now(p)]
    # running fine now, but restarted / last exit was abnormal (OOMKilled, Error...)
    attention = [p["name"] for p in pods if healthy_now(p) and (p["problems"] or p["restarts"])]
    return {"namespace": t["namespace"], "count": len(pods), "unhealthy": unhealthy,
            "attention": attention, "pods": pods}


def cmd_deploys(args) -> dict:
    t = resolve_target(args)
    items = _json(t, ["get", "deployments"]).get("items", [])
    try:  # OpenShift DeploymentConfig, if the API exists
        items += _json(t, ["get", "deploymentconfigs"]).get("items", [])
    except K8sError:
        pass
    deps = [summarize_deploy(d) for d in items]
    return {"namespace": t["namespace"], "count": len(deps), "deployments": deps}


def cmd_events(args) -> dict:
    t = resolve_target(args)
    items = _json(t, ["get", "events"]).get("items", [])
    out = []
    for e in items:
        io = e.get("involvedObject", {})
        if args.warnings and e.get("type") != "Warning":
            continue
        if args.for_ and args.for_.split("/")[-1] not in io.get("name", ""):
            continue
        ts = e.get("lastTimestamp") or e.get("eventTime") or (e.get("metadata") or {}).get("creationTimestamp")
        out.append({"time": ts, "age": _age(ts), "type": e.get("type"), "reason": e.get("reason"),
                    "object": f"{io.get('kind')}/{io.get('name')}", "count": e.get("count"),
                    "message": (e.get("message") or "").strip()[:400]})
    out.sort(key=lambda x: x["time"] or "")
    return {"namespace": t["namespace"], "count": len(out), "events": out[-args.limit:]}


def cmd_describe(args) -> dict:
    t = resolve_target(args)
    text = _kubectl(t, ["describe"] + args.target.split("/", 1) if "/" in args.target
                    else ["describe", args.target])
    return {"target": args.target, "describe": text}


def cmd_logs(args) -> dict:
    t = resolve_target(args)
    selector, target = args.selector, args.target
    if not selector and target and "/" in target and not target.startswith("pod/"):
        selector = selector_for(t, target)  # deploy/x -> all its pods, not just one
    q = ["logs", "--tail", str(args.tail), "--timestamps"]
    if args.since:
        q += ["--since", args.since]
    if args.previous:
        q += ["--previous"]
    if args.container:
        q += ["-c", args.container]
    else:
        q += ["--all-containers"]
    if selector:
        q += ["-l", selector, "--prefix", "--max-log-requests", "20"]
    elif target:
        q += [target.split("/", 1)[-1]]
    else:
        raise K8sError("usage", "logs needs a pod, deploy/NAME, or -l selector")
    lines = _kubectl(t, q, timeout=max(DEFAULT_TIMEOUT, 90)).splitlines()
    stats = {"lines": len(lines),
             "errors": sum(1 for ln in lines if ERROR_PATTERNS.search(ln)),
             "warnings": sum(1 for ln in lines if WARN_PATTERNS.search(ln))}
    if args.errors:
        lines = filter_lines(lines, ERROR_PATTERNS.pattern, args.context_lines, re.I)
    if args.grep:
        lines = filter_lines(lines, args.grep, args.context_lines)
    truncated = len(lines) > MAX_LOG_LINES_OUT
    return {"target": target or f"-l {selector}", "selector": selector, "stats": stats,
            "returned": min(len(lines), MAX_LOG_LINES_OUT), "truncated": truncated,
            "lines": lines[-MAX_LOG_LINES_OUT:]}


def cmd_get(args) -> dict:
    t = resolve_target(args)
    if args.kind.lower() in ("secret", "secrets") and not args.show_secrets:
        q = ["get", "secrets"] + ([args.name] if args.name else [])
        obj = _json(t, q)
        for it in obj.get("items", [obj]):
            if "data" in it:
                it["data"] = {k: "<redacted>" for k in it["data"]}
        return obj
    return _json(t, ["get", args.kind] + ([args.name] if args.name else []))


# --------------------------------------------------------------------------- cli

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Read-only kubectl wrapper for agents")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def target_opts(p):
        p.add_argument("--project")
        p.add_argument("--env")
        p.add_argument("--context")
        p.add_argument("--kubeconfig")
        p.add_argument("-n", "--namespace")
        p.add_argument("--insecure", action="store_true",
                       help="skip TLS verification (WARNING: only for a cluster with a rotated/self-signed CA)")
        return p

    sub.add_parser("doctor")
    p = sub.add_parser("contexts")
    p.add_argument("--kubeconfig")
    target_opts(sub.add_parser("access"))
    p = target_opts(sub.add_parser("pods"))
    p.add_argument("-l", "--selector")
    target_opts(sub.add_parser("deploys"))
    p = target_opts(sub.add_parser("events"))
    p.add_argument("--for", dest="for_")
    p.add_argument("--warnings", action="store_true")
    p.add_argument("--limit", type=int, default=50)
    p = target_opts(sub.add_parser("describe"))
    p.add_argument("target")
    p = target_opts(sub.add_parser("logs"))
    p.add_argument("target", nargs="?")
    p.add_argument("-l", "--selector")
    p.add_argument("-c", "--container")
    p.add_argument("--since")
    p.add_argument("--tail", type=int, default=200)
    p.add_argument("--previous", action="store_true")
    p.add_argument("--grep")
    p.add_argument("--errors", action="store_true", help="keep only error lines (text/JSON/glog styles)")
    p.add_argument("-C", "--context-lines", type=int, default=0)
    p = target_opts(sub.add_parser("get"))
    p.add_argument("kind")
    p.add_argument("name", nargs="?")
    p.add_argument("--show-secrets", action="store_true", help="do not redact Secret data")
    return ap


COMMANDS = {"doctor": cmd_doctor, "contexts": cmd_contexts, "access": cmd_access, "pods": cmd_pods,
            "deploys": cmd_deploys, "events": cmd_events, "describe": cmd_describe,
            "logs": cmd_logs, "get": cmd_get}


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        out = COMMANDS[args.cmd](args)
        if getattr(args, "insecure", False) and isinstance(out, dict):
            out["warning"] = "TLS verification DISABLED for this call"
    except K8sError as e:
        out = e.to_dict()
    except json.JSONDecodeError as e:
        out = {"error": True, "kind": "kubectl", "message": f"unexpected kubectl output: {e}"}
    return pc.emit(out)


if __name__ == "__main__":
    sys.exit(main())
