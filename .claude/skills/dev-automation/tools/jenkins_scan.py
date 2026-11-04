#!/usr/bin/env python3
"""Which branch each project runs on per environment — read from Jenkins, written to the registry.

    python jenkins_scan.py scan [--out temp/jenkins-scan.json] [--workers 8]   # READ-ONLY crawl of every job
    python jenkins_scan.py show [--project P] [--scan F]                         # table from the last scan
    python jenkins_scan.py apply [--scan F] [--only k1,k2]                       # [WRITE] work/projects.json

`scan` walks the whole job tree, maps the TOP folder to an environment (ENV_OF_TOP), and for each pipeline job
reads `config.xml` → the inline script (comments stripped) → GIT_REPO / the `git clone` URL and GIT_BRANCH
(environment assignment, parameter default, `-b <branch>`, `git branch:`, `branches: [[name:]]`). When the config
is forbidden (PROD jobs answer 403 to our user) it reads the first part of the last build's console for the
`git clone … -b <branch> …/<repo>` line. Credentials are never stored (URLs keep only host + path).

`apply` writes, per registry project (matched by repo_path) and env: `environments.<env>.jenkins.job/branch`
(+ `jobs` when several jobs build the repo) and `environments.<env>.deployed_branch` when every job agrees.
An existing different `jenkins.job` is kept and reported, never overwritten. Backup + lock like workspace.apply.
"""

import argparse
import concurrent.futures as cf
import html
import json
import os
import re
import runpy
import shutil
import sys
import time
import urllib.error
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
runpy.run_path(os.path.join(os.path.dirname(os.path.dirname(_HERE)), "_runtime", "bootstrap.py"))["activate"]()
from agent_skill_toolkit.workflows.security import spring  # noqa: E402  (strip_comments keeps string literals)

sys.path.insert(0, _HERE)
import jenkins as J  # noqa: E402
import probe_common as pc  # noqa: E402,F401  (UTF-8 stdout on Windows)
import project_config  # noqa: E402

# top-level Jenkins folder -> environment key used in projects.json (None = not a deployment: skip)
ENV_OF_TOP = {"DEMO": "demo", "DEMO-FCI": "demo-fci", "DEMO-SABECO": "demo-sabeco", "DEV-K8S": "dev",
              "Special-SANBOX": "sandbox", "PROD-KYTA-NEW": "prod", "PROD-H100": "prod-h100",
              "Special-Scan-Code": None, "Special-SonarQube-IDAAS": None}
SKIP_SUB = {"example", "test"}                      # sample/test folders under an env
CONSOLE_BYTES = 400_000
TREE = "jobs[name,_class,jobs[name,_class,jobs[name,_class,jobs[name,_class,jobs[name,_class,jobs[name,_class]]]]]]"


def _get(path: str, limit: int = None):
    req = urllib.request.Request(J._base() + "/" + path.lstrip("/"), headers=J._auth_header())
    try:
        with urllib.request.urlopen(req, context=J._ssl_context(), timeout=90) as r:
            return r.status, r.read(limit).decode("utf-8", "replace") if limit else r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except (urllib.error.URLError, OSError) as e:
        return None, f"{type(e).__name__}: {e}"


def job_api_path(path: str) -> str:
    return "/".join("job/" + p for p in path.split("/"))


def list_jobs() -> list:
    st, body = _get("api/json?tree=" + TREE)
    if st != 200:
        raise RuntimeError(f"Jenkins job tree: HTTP {st} {body[:200]}")
    out = []

    def walk(node, parts):
        for j in node.get("jobs") or []:
            p = parts + [j["name"]]
            if "jobs" in j:
                walk(j, p)
            else:
                out.append("/".join(p))
    walk(json.loads(body), [])
    return out


def env_of(path: str):
    parts = path.split("/")
    env = ENV_OF_TOP.get(parts[0], "?")
    if env and len(parts) > 2 and parts[1].lower() in SKIP_SUB:
        return None
    return env


_REPO_PATH = re.compile(r"(?:https?://)?(?:[^@/\s'\"]+@)?(gitlab\.[\w.-]+)/([\w.\-/]+?)(?:\.git)?(?=['\"\s&;)]|$)")


def repo_of(url_or_path: str, host_default: str = ""):
    """(host, group/repo) from a clone URL or a bare 'group/repo.git' value; credentials dropped."""
    v = url_or_path.strip().strip("'\"")
    m = _REPO_PATH.search(v)
    if m:
        return m.group(1).lower(), m.group(2).removesuffix(".git")
    m = re.fullmatch(r"([\w.\-]+(?:/[\w.\-]+)+)", v)
    return (host_default, m.group(1).removesuffix(".git")) if m else (None, None)


def _assign(script: str, var: str):
    """Literal values assigned to `var` in the (comment-free) script: env `X = "v"`, `def X = 'v'`,
    parameter defaults string(name: 'X', defaultValue: 'v') / choice(name: 'X', choices: ['v', …])."""
    vals = re.findall(rf"(?<![\w.])(?:env\.)?{var}\s*=(?!=)\s*['\"]([^'\"$]+)['\"]", script)
    for call in re.finditer(rf"\b(?:string|choice|gitParameter)\s*\(([^)]*name:\s*['\"]{var}['\"][^)]*)\)", script):
        m = re.search(r"defaultValue:\s*['\"]([^'\"]+)['\"]", call.group(1)) or \
            re.search(r"choices:\s*\[\s*['\"]([^'\"]+)['\"]", call.group(1))
        if m:
            vals.append(m.group(1))
    return list(dict.fromkeys(vals))


def _dynamic(script: str, var: str) -> bool:
    """`var` is (also) assigned something that is not a plain literal: `X = params.B`, `X = "${Y}"`, `X = f()`."""
    for m in re.finditer(rf"(?<![\w.])(?:env\.)?{var}\s*=(?!=)\s*([^\n;]*)", script):   # env.X = … too (R3 #2)
        rhs = m.group(1).strip()
        if not re.fullmatch(r"(['\"])[^'\"$]*\1[\s,)}\]]*", rhs):      # a literal, maybe closing a block
            return True
    return False


AUX_REPO = re.compile(r"(?i)(^|/)(charts?|iac|helm[\w-]*|config[\w-]*|[\w-]*shared-library|devops?-[\w-]*)(\.git)?$")
_VAR = re.compile(r"\$\{(?:env\.|params\.)?(\w+)\}|\$(?:env\.|params\.)?(\w+)")
_CRED_VAR = re.compile(r"(?i)cer|cred|token|pass|secret|key|auth")


def subst(token: str, s: str):
    """`token` with every ${VAR}/$VAR replaced by the ONE literal assigned to it in the script. A variable
    with several different assignments is ambiguous -> None; one without any -> None (unless it is a
    credential, which only ever sits in the user part of a URL and is irrelevant here). Codex R1 #2, #5."""
    token = token.strip().strip("'\"")
    out, pos = [], 0
    for m in _VAR.finditer(token):
        var = m.group(1) or m.group(2)
        vals = _assign(s, var)
        if _dynamic(s, var):                             # also set from params/env/… at run time (R2 #2)
            return None
        if len(vals) == 1:
            rep = vals[0]
        elif not vals and _CRED_VAR.search(var):
            rep = "CRED"
        else:
            return None
        out += [token[pos:m.start()], rep]
        pos = m.end()
    out.append(token[pos:])
    return "".join(out)


_OPT_WITH_ARG = {"-b", "--branch", "--depth", "-c", "--config", "-o", "--origin", "--reference", "-j", "--jobs",
                 "--filter", "--separate-git-dir", "--template", "-u", "--upload-pack", "--shallow-since",
                 "--shallow-exclude"}


def _words(args: str) -> list:
    """Shell-like split of one command line (quotes grouped, ' / " stripped); stops at ; && | or a closing
    quote of the enclosing Groovy sh '…' string."""
    out, cur, q = [], "", ""
    for ch in args:
        if q:
            if ch == q:
                q = ""
            else:
                cur += ch
            continue
        if ch in "\"'":
            q = ch
            continue
        if ch.isspace():
            if cur:
                out.append(cur)
                cur = ""
            continue
        if ch in ";|&)":
            break
        cur += ch
    if cur:
        out.append(cur)
    return out


def _clone_lines(text: str):
    """(branch argument, repository argument) of every `git [-c k=v] clone …` line, per git's own syntax:
    options (and their values) skipped, the first positional argument is the repository. Shell line
    continuations (backslash + newline) are joined first: a clone split over two lines keeps its URL."""
    text = re.sub(r"\\[ \t]*\r?\n[ \t]*", " ", text)
    for m in re.finditer(r"git\s+(?:-c\s+\S+\s+)*clone\b([^\n]*)", text):
        words = _words(m.group(1))
        branch, pos, k = "", [], 0
        while k < len(words):
            w = words[k]
            if w in ("-b", "--branch") and k + 1 < len(words):
                branch, k = words[k + 1], k + 2
                continue
            if w.startswith("--branch="):
                branch = w.split("=", 1)[1]
            elif w in _OPT_WITH_ARG:
                k += 2
                continue
            elif not w.startswith("-"):
                pos.append(w)
            k += 1
        yield branch, (pos[0] if pos else "")


def _repo_arg(url_tok: str, s: str):
    """The repository argument with its userinfo (user:token@) dropped, then variables substituted."""
    tok = url_tok.strip()
    m = re.match(r"^([a-z+]+://)?(?:[^@/\s]*@)?(.*)$", tok)
    rest = (m.group(1) or "") + m.group(2) if m else tok
    return subst(rest, s)


def _pick_main(clones: list):
    """Application clone: helpers (config / charts / iac / shared libs) are excluded FIRST, then the one fed
    by GIT_BRANCH wins, else the first remaining (Codex R1 #6)."""
    app = [c for c in clones if c["repo"] and not AUX_REPO.search(c["repo"][1])]
    return next((c for c in app if c["branch_var"] in ("GIT_BRANCH", "BRANCH", "BRANCH_NAME")), None) or \
        (app[0] if app else None)


_URL_GUARD = "␞"                                    # stands in for the // of scheme:// while stripping


def strip_groovy_comments(script: str) -> str:
    """Comments blanked, strings kept (runtime strip_comments) — but `scheme://` is never a comment start,
    even outside a string literal (a bare URL in a shell block)."""
    guarded = re.sub(r"(?<=[A-Za-z0-9+]):(?=//)", ":" + _URL_GUARD, script).replace(_URL_GUARD + "//", _URL_GUARD * 2)
    return spring.strip_comments(guarded).replace(_URL_GUARD * 2, "//")


def _branch(token: str, s: str):
    """A branch argument resolved to ONE plain ref name, else None (every source goes through this — R2 #3)."""
    if not token:
        return None
    b = subst(token, s)
    if b is None:
        return None
    b = b.replace("refs/heads/", "")
    return b if re.fullmatch(r"[\w./-]+", b) and "$" not in b else None


def parse_script(script: str, host_default: str) -> dict:
    """Main repo + branch of an inline pipeline. Each `git clone` is resolved on its own: its URL and its
    -b argument are substituted IN FULL (host kept — Codex R1 #1; suffixes kept — R1 #5); anything not
    resolvable to one literal stays unresolved (branch None) instead of guessed."""
    s = strip_groovy_comments(script)
    clones = []
    for br_tok, url_tok in _clone_lines(s):
        url = _repo_arg(url_tok, s) if url_tok else None
        repo = repo_of(url, host_default) if url else (None, None)
        v = _VAR.search(br_tok or "")
        clones.append({"repo": repo if repo[1] else None, "branch": _branch(br_tok, s),
                       "branch_var": (v.group(1) or v.group(2)) if v else ""})
    for m in re.finditer(r"\bgit\s*\(?\s*(?:[^)\n]*?)url:\s*['\"]([^'\"]+)['\"][^)\n]*", s):
        bm = re.search(r"branch:\s*['\"]([^'\"]+)['\"]", m.group(0))
        repo = repo_of(subst(m.group(1), s) or "", host_default)
        clones.append({"repo": repo if repo[1] else None, "branch": _branch(bm.group(1), s) if bm else None,
                       "branch_var": ""})
    for m in re.finditer(r"branches:\s*\[\s*\[\s*name:\s*['\"]([^'\"]+)['\"][\s\S]{0,400}?url:\s*['\"]([^'\"]+)", s):
        repo = repo_of(subst(m.group(2), s) or "", host_default)
        clones.append({"repo": repo if repo[1] else None, "branch": _branch(m.group(1).replace("*/", ""), s),
                       "branch_var": ""})
    main = _pick_main(clones)
    aux = [c["repo"][1] for c in clones if c is not main and c["repo"]]
    if main is None:
        return {"repos": [], "branches": [], "aux": list(dict.fromkeys(aux))}
    return {"repos": [main["repo"]], "branches": [main["branch"]] if main["branch"] else [],
            "aux": list(dict.fromkeys(aux))}


def parse_console(text: str, host_default: str) -> dict:
    """From a build log: the application clone line and the -b ON THAT LINE only (a later `git checkout`
    may belong to another workspace — Codex R1 #4)."""
    clones = []
    for br_tok, url_tok in _clone_lines(text):
        repo = repo_of(re.sub(r"^([a-z+]+://)?[^@/\s]*@", r"\1", url_tok), host_default) if url_tok else (None, None)
        br = br_tok.strip("'\"") if br_tok else None
        clones.append({"repo": repo if repo[1] else None,
                       "branch": br if br and re.fullmatch(r"[\w./-]+", br) else None, "branch_var": ""})
    main = _pick_main(clones)
    if main is None:
        return {"repos": [], "branches": []}
    return {"repos": [main["repo"]], "branches": [main["branch"]] if main["branch"] else []}


def scan_job(path: str, host_default: str) -> dict:
    rec = {"path": path, "job": job_api_path(path), "env": env_of(path)}
    st, xml = _get(rec["job"] + "/config.xml")
    rec["config"] = st
    info = {"repos": [], "branches": []}
    if st == 200:
        m = re.search(r"<script>(.*?)</script>", xml, re.S)
        if m:
            info = parse_script(html.unescape(m.group(1)), host_default)
        else:                                            # SCM-defined pipeline / freestyle
            urls = re.findall(r"<url>([^<]+)</url>", xml)
            info = {"repos": [x for x in (repo_of(u, host_default) for u in urls) if x[1]],
                    "branches": [b.replace("*/", "") for b in re.findall(r"<name>([^<]+)</name>", xml)
                                 if "/" in b or b in ("master", "main", "develop")]}
        rec["source"] = "config"
    st_b, meta = _get(rec["job"] + "/api/json?tree=lastBuild[number,result,timestamp],"
                                   "lastSuccessfulBuild[number,timestamp]")
    if st_b == 200:
        d = json.loads(meta)
        lb, ls = d.get("lastBuild") or {}, d.get("lastSuccessfulBuild") or {}
        rec["last_build"] = {k: lb.get(k) for k in ("number", "result", "timestamp")} if lb else None
        rec["last_success"] = {k: ls.get(k) for k in ("number", "timestamp")} if ls else None
    if (not info["repos"] or not info["branches"]) and (rec.get("last_build") or {}).get("number"):
        build = (rec.get("last_success") or rec["last_build"])["number"]
        st_c, text = _get(f"{rec['job']}/{build}/consoleText", limit=CONSOLE_BYTES)
        if st_c == 200:
            c = parse_console(text, host_default)
            key = lambda rs: [((h or "").lower(), (r or "").lower()) for h, r in rs]       # host AND path (R3 #1)
            same = not info["repos"] or key(info["repos"]) == key(c["repos"])
            # the console branch only counts for the SAME repository the config names (Codex R2 #1)
            info = {"repos": info["repos"] or c["repos"],
                    "branches": info["branches"] or (c["branches"] if same else []),
                    "aux": info.get("aux", [])}
            rec["source"] = (rec.get("source", "") + "+console#" + str(build)).lstrip("+")
    rec["repos"] = [{"host": h, "path": r} for h, r in info["repos"]]
    rec["branches"] = info["branches"]
    if info.get("aux"):
        rec["aux_repos"] = info["aux"]
    return rec


def scan(workers: int = 8) -> dict:
    from urllib.parse import urlparse
    import config
    host = (urlparse(config.get("GITLAB_URL", "")).hostname or "gitlab.fis.vn").lower()
    jobs = list_jobs()
    t0 = time.time()
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        recs = list(ex.map(lambda p: scan_job(p, host), jobs))
    return {"scanned_at": time.strftime("%Y-%m-%d %H:%M:%S"), "jenkins": J._base(), "gitlab_host": host,
            "seconds": round(time.time() - t0, 1), "jobs": recs}


def _norm_repo(p: str) -> str:
    return (p or "").lower().strip("/").removesuffix(".git")


def plan(scan_doc: dict, reg: dict) -> dict:
    """{project: {env: {"jobs": [...], "branches": [...], "branch": b|None}}} + repos no project has."""
    host = scan_doc.get("gitlab_host", "")
    by_repo = {}
    for k, e in reg.items():
        if isinstance(e, dict) and e.get("repo_path") and not e.get("remote_host"):
            by_repo.setdefault(_norm_repo(e["repo_path"]), []).append(k)
    out, unmatched = {}, {}
    for rec in scan_doc["jobs"]:
        if not rec.get("env") or rec["env"] == "?":
            continue
        for repo in rec.get("repos") or []:
            if repo.get("host") and repo["host"] != host:
                continue
            keys = by_repo.get(_norm_repo(repo["path"]))
            if not keys:
                unmatched.setdefault(_norm_repo(repo["path"]), []).append(f"{rec['env']}:{rec['path']}")
                continue
            for k in keys:
                slot = out.setdefault(k, {}).setdefault(rec["env"], {"jobs": [], "branches": []})
                slot["jobs"].append({"job": rec["job"], "branch": (rec.get("branches") or [None])[0],
                                     "source": rec.get("source"), "last_build": rec.get("last_build")})
                for b in (rec.get("branches") or [])[:1]:
                    if b not in slot["branches"]:
                        slot["branches"].append(b)
    for key, envs in out.items():
        repo_name = _norm_repo((reg.get(key) or {}).get("repo_path", "")).rsplit("/", 1)[-1]
        for slot in envs.values():
            slot["jobs"].sort(key=lambda j: j["job"])
            name = lambda j: j["job"].rsplit("/", 1)[-1].lower()
            primary = next((j for j in slot["jobs"] if name(j) == key), None) or                 next((j for j in slot["jobs"] if name(j) == repo_name), None) or slot["jobs"][0]
            slot["primary"] = primary["job"]
            slot["branch"] = primary["branch"]          # several jobs = several deployments: each keeps its own
    return {"projects": out, "unmatched_repos": unmatched}


def apply(scan_path: str, only: list = None) -> dict:
    with open(scan_path, encoding="utf-8") as f:
        scan_doc = json.load(f)
    path = os.path.join(project_config._work_dir(), "projects.json")
    from agent_skill_toolkit.core import locks
    with locks.file_lock(path + ".lock", timeout=30):
        with open(path, encoding="utf-8") as f:
            reg = json.load(f)
        p = plan(scan_doc, reg)
        changes, conflicts = [], []
        for key, envs in p["projects"].items():
            if only and key not in only:
                continue
            for env, slot in envs.items():
                e = reg[key].setdefault("environments", {}).setdefault(env, {})
                before = json.dumps(e, sort_keys=True)
                jk = e.setdefault("jenkins", {})
                jobs = {j["job"]: j for j in slot["jobs"]}
                if jk.get("job") and jk["job"] not in jobs:
                    conflicts.append({"project": key, "env": env, "registry_job": jk["job"],
                                      "jenkins_jobs": sorted(jobs)})
                    if not jk:
                        e.pop("jenkins", None)
                    continue
                anchor = jobs[jk["job"]] if jk.get("job") else jobs[slot["primary"]]
                jk["job"] = anchor["job"]
                if anchor["branch"]:
                    jk["branch"] = anchor["branch"]
                    e["deployed_branch"] = anchor["branch"]
                if len(jobs) > 1:
                    jk["jobs"] = [{"job": j["job"], "branch": j["branch"]} for j in slot["jobs"]]
                else:
                    jk.pop("jobs", None)
                after_core = json.dumps(e, sort_keys=True)
                if after_core != before:
                    jk["scanned_at"] = scan_doc.get("scanned_at")
                    changes.append({"project": key, "env": env, "job": anchor["job"], "branch": anchor["branch"]})
        if not changes:
            return {"changes": [], "conflicts": conflicts, "written": False}
        backup = f"{path}.bak-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}-{os.urandom(3).hex()}"
        shutil.copy2(path, backup)
        tmp = f"{path}.tmp-{os.getpid()}"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(reg, f, ensure_ascii=False, indent=2)
            f.write("\n")
        with open(tmp, encoding="utf-8") as f:
            json.load(f)
        os.replace(tmp, path)
    return {"changes": changes, "conflicts": conflicts, "written": True, "backup": backup}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    default_scan = os.path.join(project_config._repo_root(), "temp", "jenkins-scan.json")
    p = sub.add_parser("scan")
    p.add_argument("--out", default=default_scan)
    p.add_argument("--workers", type=int, default=8)
    p = sub.add_parser("show")
    p.add_argument("--scan", default=default_scan)
    p.add_argument("--project", default="")
    p = sub.add_parser("apply")
    p.add_argument("--scan", default=default_scan)
    p.add_argument("--only", default="")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "scan":
            doc = scan(a.workers)
            os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
            with open(a.out, "w", encoding="utf-8", newline="\n") as f:
                json.dump(doc, f, ensure_ascii=False, indent=1)
            jobs = doc["jobs"]
            print(json.dumps({"scan": a.out, "jobs": len(jobs), "seconds": doc["seconds"],
                              "config_forbidden": sum(1 for j in jobs if j.get("config") == 403),
                              "no_repo": sum(1 for j in jobs if j.get("env") and not j.get("repos")),
                              "no_branch": sum(1 for j in jobs if j.get("env") and not j.get("branches"))},
                             ensure_ascii=False, indent=2))
        elif a.cmd == "show":
            with open(a.scan, encoding="utf-8") as f:
                doc = json.load(f)
            p = plan(doc, project_config._registry())
            for key in sorted(p["projects"]):
                if a.project and key != a.project:
                    continue
                cells = [f"{env}={slot['branch'] or '?'}" + (f"(+{len(slot['jobs']) - 1} job)" if len(slot["jobs"]) > 1 else "")
                         for env, slot in sorted(p["projects"][key].items())]
                print(f"{key:<28} " + "  ".join(cells))
            if not a.project:
                print(f"\n(repo trên Jenkins chưa có trong registry: {len(p['unmatched_repos'])})")
        else:
            print(json.dumps(apply(a.scan, [k for k in a.only.split(",") if k]), ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, RuntimeError) as e:
        print(json.dumps({"error": True, "message": f"{type(e).__name__}: {e}"}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    sys.exit(main())
