#!/usr/bin/env python3
"""agent_init — give a repo a vendor-neutral knowledge standard under `.agent/`.

The problem this solves
-----------------------
Project knowledge tends to live in a vendor file (`CLAUDE.md`), so only one
agent can read it. Cursor, Gemini, Codex and every other tool start blind and
re-derive the architecture from source — slowly, and often wrongly.

`.agent/` is the single source of truth, committed to the product repo. Each
vendor file becomes a THIN POINTER (a few lines) into it, so knowledge is
written once and no copy can drift.

    .agent/project.yml        machine-readable facts (derived, no AI, no secrets)
    .agent/architecture.md    how the system is built
    .agent/conventions.md     rules code must follow
    .agent/learned.md         gotchas + known debt found while working
    .agent/decisions/         ADRs (or a pointer to where they already live)
    AGENTS.md                 entry point
    CLAUDE.md GEMINI.md
    .cursorrules .cursor/…    pointers — one per vendor convention. They are
    .github/copilot-…         cheap (4 lines), so where a vendor has had two
                              locations we emit both rather than guess.

Commands
--------
    scan   read the repo -> facts JSON. Deterministic: no agent, no network.
    draft  build the .agent/ tree into temp/agent_init/<project>/ for review.
    apply  [WRITE] copy the draft into the repo + write vendor pointers.
    check  re-scan and diff against the committed project.yml -> drift report.
    docs   sort an existing documentation pile into tiers of trust.
    add    clone a repo, register it, and draft — one command for a new project.

Re-running is safe: once `.agent/` holds knowledge, `draft` preserves it. It
never treats a generated pointer as a knowledge source — that would overwrite
the architecture with navigation text.

Migration is lossless: an existing CLAUDE.md is split by its own `##` headings
into architecture/conventions/learned, and `draft` reports any content line that
would be dropped so a human can see it before `apply`.

Stdlib only.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SKILLS = os.path.abspath(os.path.join(_HERE, "..", ".."))
sys.path.insert(0, os.path.join(_SKILLS, "dev-automation", "tools"))

import project_config  # noqa: E402
import spring_config   # noqa: E402

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

SPEC_VERSION = "0.1"

# Vendor entry files. All of them point into .agent/. Add a row when a new agent
# appears; nothing else in the repo changes.
POINTERS = {
    "AGENTS.md": "entry",
    "CLAUDE.md": "pointer",
    "GEMINI.md": "pointer",
    # Cursor has used both a root `.cursorrules` and `.cursor/rules/*.mdc`. Which
    # one is current is not worth guessing: a pointer is four lines, emitting a
    # spare one costs nothing, and guessing wrong costs the whole file. The root
    # form has a second advantage — repos routinely gitignore `.cursor/`.
    ".cursorrules": "pointer",
    ".cursor/rules/00-agent.mdc": "pointer",
    ".github/copilot-instructions.md": "pointer",
}

# Sentence stamped into every generated pointer. It is how we recognise our own
# output later: a pointer must never be mistaken for a knowledge source.
_POINTER_MARK = "This file is a pointer on purpose"

# Heading -> destination file when migrating an existing vendor doc. Matched
# case-insensitively as a substring of the `##` heading text.
_SPLIT = [
    ("conventions.md", ("layer rule", "naming convention", "anti-pattern",
                        "communication giua", "communication giữa",
                        "performance guideline", "quy uoc", "quy ước")),
    ("learned.md", ("tech debt", "known issue", "gotcha", "no ky thuat",
                    "nợ kỹ thuật")),
]


# --------------------------------------------------------------------------
# a tiny YAML subset (emit + load). Only what project.yml uses: nested maps,
# lists of scalars, scalars. No anchors, no multiline. Repo rule: stdlib only.
# --------------------------------------------------------------------------
def yaml_dump(obj, indent=0):
    """Emit only what `yaml_load` can read back.

    The invariant matters: `check` compares a re-scan against the committed
    file, so a value that dumps but does not load would read as permanent
    drift. A list of maps is therefore refused at the source — model it as a
    nested map instead.
    """
    pad = "  " * indent
    out = []
    for k, v in obj.items():
        if isinstance(v, dict):
            if v:
                out.append(f"{pad}{k}:")
                out.append(yaml_dump(v, indent + 1))
            else:
                out.append(f"{pad}{k}: {{}}")
        elif isinstance(v, (list, tuple)):
            if any(isinstance(x, (dict, list, tuple)) for x in v):
                raise ValueError(f"'{k}': list of maps is outside the supported "
                                 f"subset — use a nested map keyed by name")
            if not v:
                out.append(f"{pad}{k}: []")
            else:
                out.append(f"{pad}{k}:")
                out.extend(f"{pad}  - {_scalar(x)}" for x in v)
        else:
            out.append(f"{pad}{k}: {_scalar(v)}")
    return "\n".join(out)


def _scalar(v):
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    s = str(v)
    if s == "" or re.search(r"[:#\[\]{}\"']", s) or s != s.strip():
        return '"' + s.replace('"', '\\"') + '"'
    return s


def _unscalar(s):
    s = s.strip()
    if s in ("null", "~", ""):
        return None
    if s in ("true", "false"):
        return s == "true"
    if len(s) >= 2 and s[0] == s[-1] == '"':
        return s[1:-1].replace('\\"', '"')
    if re.fullmatch(r"-?\d+", s):
        return int(s)
    return s


def yaml_load(text):
    """Load the subset emitted by yaml_dump.

    Kept deliberately small: a key with no value opens either a nested map or a
    list, decided by whichever kind of line arrives next.
    """
    root = {}
    stack = [(-1, root)]        # (indent, container)
    pending = None              # (indent, parent, key) — a key with no value yet:
                                # the NEXT line decides whether it is a map or a list
    for raw in text.split("\n"):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        line = raw.strip()

        if line.startswith("- "):
            if pending is None or indent <= pending[0]:
                raise ValueError(f"list item without a key: {raw!r}")
            _, parent, key = pending
            if not isinstance(parent.get(key), list):
                parent[key] = []
            parent[key].append(_unscalar(line[2:]))
            continue

        # a deeper key means the pending key was a map after all
        if pending is not None and indent > pending[0]:
            p_indent, parent, key = pending
            child = {}
            parent[key] = child
            stack.append((p_indent, child))
        pending = None

        while len(stack) > 1 and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]
        key, _, rest = line.partition(":")
        key, rest = key.strip(), rest.strip()
        if not key:
            raise ValueError(f"line is not a key: {raw!r}")
        if rest == "":
            parent[key] = {}                    # provisional — may become a list
            pending = (indent, parent, key)
        elif rest == "[]":
            parent[key] = []
        elif rest == "{}":
            parent[key] = {}
        else:
            parent[key] = _unscalar(rest)
    return root


# --------------------------------------------------------------------------
# scan — deterministic facts. No agent, no network, and NO secrets: this file
# is committed, so hosts/users/passwords stay in the registry and in .env.
# --------------------------------------------------------------------------
def _run(cmd, cwd):
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=20)
        return r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _pom_facts(root):
    pom = os.path.join(root, "pom.xml")
    if not os.path.isfile(pom):
        return {}
    with open(pom, encoding="utf-8", errors="replace") as f:
        text = f.read(200000)

    def tag(name):
        m = re.search(r"<%s>([^<]+)</%s>" % (re.escape(name), re.escape(name)), text)
        return m.group(1).strip() if m else None

    facts = {"lang": "java", "build": "maven"}
    for key, tagname in (("java_version", "java.version"),
                         ("spring_boot", "spring-boot.version"),
                         ("jhipster", "jhipster-dependencies.version")):
        v = tag(tagname)
        if v:
            facts[key] = v
    if facts.get("jhipster"):
        facts["framework"] = "jhipster-spring-boot"
    elif facts.get("spring_boot"):
        facts["framework"] = "spring-boot"
    return facts


def _base_package(root, entrypoints=()):
    """Locate the base package and its top-level structure.

    Prefer the directory holding the `@SpringBootApplication` class: that is the
    base package by definition, and it works whatever the repo's layout is. The
    linear walk is only a fallback for repos with no discoverable entrypoint.

    Returns (package, dirpath). Earlier this looked for a `module/` directory and
    gave up otherwise, which quietly produced no layout at all for the classic
    layered repos (`domain/`, `service/`, `web/`) that are just as common.
    """
    src = os.path.join(root, "src", "main", "java")
    if not os.path.isdir(src):
        return None, None

    for rel in entrypoints:
        d = os.path.dirname(os.path.join(root, rel.replace("/", os.sep)))
        if os.path.isdir(d):
            return os.path.relpath(d, src).replace(os.sep, "."), d

    cur = src
    for _ in range(12):
        try:
            entries = [x for x in os.listdir(cur) if os.path.isdir(os.path.join(cur, x))]
        except OSError:
            return None, None
        if "module" in entries or len(entries) != 1:
            return os.path.relpath(cur, src).replace(os.sep, "."), cur
        cur = os.path.join(cur, entries[0])
    return None, None


def _default_branch(block, root):
    """The branch the project lives on — not whatever is checked out right now.

    A working tree is often parked on a feature branch, so `rev-parse HEAD` is
    the wrong answer and a confidently wrong one. Ask the registry, then the
    remote's own idea of its default, and only then fall back to HEAD.
    """
    explicit = block.get("default_branch") or block.get("default_target_branch")
    if explicit:
        return explicit
    ref = _run(["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"], root)
    if ref:
        return ref.split("/", 1)[-1]
    return _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], root) or None


def _registry_block(project):
    path = os.path.join(project_config.work_dir(), "projects.json") \
        if hasattr(project_config, "work_dir") else \
        os.path.join(os.environ.get("WORK_DIR") or os.path.join(_repo_root(), "work"),
                     "projects.json")
    if not os.path.isfile(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return (json.load(f) or {}).get(project, {})


# --------------------------------------------------------------------------
# observability standard
#
# One definition, two outputs: the requirement text an agent reads, and the
# check that decides whether a repo meets it. Keeping them in one structure is
# the only way they cannot drift apart.
#
# "required" means a repo that fails it is reported as non-compliant, and the
# entry file says so where every agent will see it. "advisory" is reported but
# does not fail. Editing this table is how the policy changes — no code change.
# --------------------------------------------------------------------------

OBSERVABILITY_VERSION = "1.0"

OBSERVABILITY_STANDARD = [
    {
        "id": "OBS-1", "level": "required",
        "title": "Có endpoint health",
        "why": "Không có nó thì orchestrator không biết khi nào service sẵn sàng, "
               "và không ai biết nó còn sống hay không ngoài việc thử gọi API.",
        "how": "Actuator (hoặc tương đương) bật, endpoint health lộ ra.",
    },
    {
        "id": "OBS-2", "level": "required",
        "title": "Có endpoint xuất metric",
        "why": "Không xuất được thì mọi metric bên dưới đều vô nghĩa.",
        "how": "Prometheus (hoặc tương đương) nằm trong danh sách endpoint lộ ra.",
    },
    {
        "id": "OBS-3", "level": "required",
        "title": "Mỗi đầu vào bất đồng bộ phải có ít nhất một metric nghiệp vụ",
        "why": "Đây là luật bắt được kiểu hỏng nguy hiểm nhất: hỏng mà health vẫn "
               "xanh. Một consumer Kafka có thân hàm rỗng, một activity định tuyến "
               "nhầm queue, một job nuốt lỗi — không tín hiệu hạ tầng nào thấy được. "
               "Một counter 'đã xử lý' thì thấy ngay.",
        "how": "Có @KafkaListener / @Scheduled / worker Temporal thì phải có "
               "MeterRegistry, @Timed, Counter/Timer builder hoặc Metrics.* .",
    },
    {
        "id": "OBS-4", "level": "required",
        "title": "Truyền correlation id qua ranh giới bất đồng bộ",
        "why": "Không có nó thì một sự cố đi qua Kafka trở thành hai sự cố rời rạc "
               "ở hai service, và không ai nối lại được.",
        "how": "MDC / traceId / correlationId xuất hiện ở cả nơi gửi và nơi nhận.",
    },
    {
        "id": "OBS-5", "level": "required",
        "title": "Đã ghi rõ 'khoẻ' nghĩa là gì",
        "why": "Bốn luật trên nói repo CÓ THỂ đo. Luật này nói đội đã quyết ĐO GÌ. "
               "Không có nó thì có metric cũng không ai biết ngưỡng nào là bất thường.",
        "how": ".agent/operations.md tồn tại và đã được điền (không còn phần khung).",
    },
    {
        "id": "OBS-6", "level": "advisory",
        "title": "Thư viện tracing đã bật, hoặc đã gỡ",
        "why": "Thư viện nằm trên classpath mà không bật tạo ảo giác là có tracing.",
        "how": "Có zipkin/sleuth/otel trong pom thì phải có cấu hình bật tương ứng.",
    },
]

_OBS_INTAKE = re.compile(r"@KafkaListener|@Scheduled|@WorkflowImpl|@ActivityImpl"
                         r"|@RabbitListener|@JmsListener")
_OBS_METER = re.compile(r"MeterRegistry|@Timed|Counter\.builder|Timer\.builder"
                        r"|\bMetrics\.(counter|timer|gauge)")
_OBS_CORR = re.compile(r"\bMDC\b|traceId|correlationId|X-B3-|X-Correlation")
_OBS_TRACE_LIB = re.compile(r"zipkin|sleuth|opentelemetry|skywalking", re.IGNORECASE)
_OBS_SRC_EXT = (".java", ".kt", ".py", ".js", ".ts", ".go")
# Vendored third-party trees must not count as project instrumentation, and
# must not be walked: eintelligent's com.fis.ai.jdk alone is 283 files.
_OBS_SKIP_DIRS = {"jdk", "target", "build", "node_modules", ".git", "generated"}


def _obs_scan_source(root):
    """One walk over src/main: where work enters, and what is instrumented."""
    found = {"intake": 0, "meters": 0, "correlation": 0,
             "intake_with_corr": 0, "intake_files": []}
    src = os.path.join(root, "src", "main")
    if not os.path.isdir(src):
        src = root
    for dirpath, dirnames, filenames in os.walk(src):
        dirnames[:] = [d for d in dirnames if d not in _OBS_SKIP_DIRS]
        for fn in filenames:
            if not fn.endswith(_OBS_SRC_EXT):
                continue
            path = os.path.join(dirpath, fn)
            try:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
            except OSError:
                continue
            has_corr = bool(_OBS_CORR.search(text))
            if _OBS_INTAKE.search(text):
                found["intake"] += 1
                rel = os.path.relpath(path, root).replace(os.sep, "/")
                if len(found["intake_files"]) < 5:
                    found["intake_files"].append(rel)
                # OBS-4 asks whether the id crosses the boundary, so it is only
                # satisfied where work enters. A traceId mentioned anywhere else
                # proves nothing: a test controller minting its own UUID would
                # otherwise pass the requirement.
                if has_corr:
                    found["intake_with_corr"] += 1
            if _OBS_METER.search(text):
                found["meters"] += 1
            if has_corr:
                found["correlation"] += 1
    return found


def _obs_scan_config(root):
    """Management endpoints and tracing, read from config rather than guessed."""
    out = {"exposed": [], "health": False, "metrics": False,
           "trace_lib": None, "trace_enabled": None}
    for rel in ("pom.xml", "build.gradle", "build.gradle.kts", "package.json"):
        path = os.path.join(root, rel)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                build = fh.read()
        except OSError:
            continue
        m = _OBS_TRACE_LIB.search(build)
        if m:
            out["trace_lib"] = m.group(0).lower()
    for base in (os.path.join(root, "src", "main", "resources"), root):
        if not os.path.isdir(base):
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in _OBS_SKIP_DIRS]
            for fn in filenames:
                if not (fn.endswith((".yml", ".yaml")) and fn.startswith("application")):
                    continue
                try:
                    with open(os.path.join(dirpath, fn), encoding="utf-8",
                              errors="replace") as fh:
                        text = fh.read()
                except OSError:
                    continue
                for line in text.splitlines():
                    s = line.strip()
                    if s.startswith("#"):
                        continue
                    if "include:" in s:
                        names = re.findall(r"[a-z][a-z0-9]+", s.split("include:", 1)[1])
                        out["exposed"] += [n for n in names if n != "include"]
                    if re.search(r"(zipkin|tracing|sleuth)", s, re.IGNORECASE) \
                            and "enabled:" in s:
                        out["trace_enabled"] = "true" in s.lower()
        break
    out["exposed"] = sorted(set(out["exposed"]))
    out["health"] = "health" in out["exposed"]
    out["metrics"] = any(k in out["exposed"] for k in ("prometheus", "metrics",
                                                       "jhimetrics"))
    return out


def observability(root):
    """Facts + per-requirement verdict. Re-derivable, so `check` can gate on it."""
    src = _obs_scan_source(root)
    cfg = _obs_scan_config(root)
    ops = os.path.join(root, ".agent", "operations.md")
    ops_filled = False
    if os.path.isfile(ops):
        try:
            with open(ops, encoding="utf-8", errors="replace") as fh:
                body = fh.read()
            ops_filled = _OPS_PLACEHOLDER not in body and len(body.strip()) > 400
        except OSError:
            ops_filled = False

    verdicts = {}
    verdicts["OBS-1"] = cfg["health"]
    verdicts["OBS-2"] = cfg["metrics"]
    # No async intake at all -> nothing to instrument, so the rule is satisfied
    # rather than vacuously failed. A library or a pure REST service is fine.
    verdicts["OBS-3"] = src["meters"] > 0 if src["intake"] else True
    verdicts["OBS-4"] = src["intake_with_corr"] > 0 if src["intake"] else True
    verdicts["OBS-5"] = ops_filled
    verdicts["OBS-6"] = (cfg["trace_enabled"] is True) if cfg["trace_lib"] else True

    facts = {
        "standard_version": OBSERVABILITY_VERSION,
        "endpoints_exposed": cfg["exposed"],
        "health_endpoint": cfg["health"],
        "metrics_endpoint": cfg["metrics"],
        "async_intake_files": src["intake"],
        "custom_meter_files": src["meters"],
        "correlation_files": src["correlation"],
        "intake_with_correlation": src["intake_with_corr"],
        "tracing_library": cfg["trace_lib"],
        "tracing_enabled": cfg["trace_enabled"],
        "operations_doc": ops_filled,
    }
    failed = [r["id"] for r in OBSERVABILITY_STANDARD if not verdicts[r["id"]]]
    facts["meets"] = sorted(set(verdicts) - set(failed))
    facts["fails"] = failed
    facts["compliant"] = not [r for r in OBSERVABILITY_STANDARD
                              if r["level"] == "required" and not verdicts[r["id"]]]
    return facts, src["intake_files"]


def scan(project, env=None):
    """Facts a machine can derive. Everything here must be re-derivable, so
    `check` can tell stale knowledge apart from an intentional edit."""
    block = _registry_block(project)
    root = block.get("clone_dir")
    if not root:
        raise ValueError(f"project '{project}': registry has no clone_dir "
                         f"(add it to work/projects.json)")
    if not os.path.isdir(root):
        raise ValueError(f"clone_dir not found: {root}")

    facts = {
        "spec_version": SPEC_VERSION,
        "name": project,
        # Two writers, two spellings: `new-product/registry.py add` writes
        # gitlab_path/default_target_branch, hand-written entries use
        # repo_path/default_branch. Read both rather than silently produce nulls.
        "repo": block.get("repo_path") or block.get("gitlab_path"),
        "default_branch": _default_branch(block, root),
        "stack": _pom_facts(root) or {"build": "unknown"},
        "commands": {},
        "layout": {},
        "datastores": {},
        "verification": {},
        "knowledge": {},
    }

    # commands — prefer the wrapper the repo ships with
    mvn = None
    if os.path.isfile(os.path.join(root, "mvnw")) or os.path.isfile(os.path.join(root, "mvnw.cmd")):
        mvn = "./mvnw"
    elif facts["stack"].get("build") == "maven":
        mvn = "mvn"
    if mvn:
        cmds = {"build": mvn + " -q -DskipTests package", "test": mvn + " -q test"}
        if os.path.isfile(os.path.join(root, "checkstyle.xml")):
            cmds["lint"] = mvn + " -q checkstyle:check"
        facts["commands"] = cmds

    # Entrypoint by annotation, not by filename: JHipster calls it `EtaskApp.java`,
    # so a `*Application.java` convention would silently find nothing.
    entry = []
    java_src = os.path.join(root, "src", "main", "java")
    for dirpath, dirnames, filenames in os.walk(java_src):
        dirnames[:] = [d for d in dirnames if d not in (".git", "target")]
        for fn in filenames:
            if not fn.endswith(".java"):
                continue
            path = os.path.join(dirpath, fn)
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    head = f.read(4000)
            except OSError:
                continue
            if "@SpringBootApplication" in head:
                entry.append(os.path.relpath(path, root).replace(os.sep, "/"))
    if entry:
        facts["layout"]["entrypoints"] = sorted(entry)[:5]

    # layout — whatever unit this repo actually reasons in. A modular repo has
    # `module/<name>`; a layered one has `domain/`, `service/`, `web/`. Report
    # the one that exists instead of assuming either.
    pkg, pkg_dir = _base_package(root, facts["layout"].get("entrypoints", ()))
    if pkg:
        facts["layout"]["base_package"] = pkg
    if pkg_dir and os.path.isdir(pkg_dir):
        subs = sorted(d for d in os.listdir(pkg_dir)
                      if os.path.isdir(os.path.join(pkg_dir, d)))
        module_dir = os.path.join(pkg_dir, "module")
        if os.path.isdir(module_dir):
            facts["layout"]["modules"] = sorted(
                d for d in os.listdir(module_dir)
                if os.path.isdir(os.path.join(module_dir, d)))
        elif subs:
            facts["layout"]["packages"] = subs

    # datastores — engine + where config lives. NEVER host/user/password.
    sc = spring_config.load(root, env=env or block.get("default_env") or "dev") or {}
    engine = (sc.get("db") or {}).get("engine")
    if engine:
        srcs = sc.get("sources") or []
        facts["datastores"][engine] = {
            "config": os.path.relpath(srcs[-1], root).replace(os.sep, "/") if srcs else None,
        }

    # verification — what evidence this repo can already produce
    tests = 0
    for dirpath, _, filenames in os.walk(os.path.join(root, "src", "test")):
        tests += sum(1 for fn in filenames if fn.endswith(".java") and "Test" in fn)
    ci = None
    for candidate in (".gitlab-ci.yml", "Jenkinsfile", ".github/workflows"):
        if os.path.exists(os.path.join(root, candidate)):
            ci = candidate
            break
    facts["verification"] = {"test_files": tests, "ci": ci}

    # observability — can this repo be watched, and does it meet the standard?
    obs, _intake = observability(root)
    facts["observability"] = obs

    # knowledge — what already exists, so we migrate instead of overwriting
    # "Holds decisions" means it actually contains one, not that the directory
    # exists: `git mv` leaves the source directory behind, and an empty
    # `docs/adr/` would otherwise keep winning after the ADRs moved to canon.
    # README.md does not count — every one of these dirs ships with one.
    adr = None
    for candidate in (".agent/decisions", "docs/adr", "doc/adr"):
        d = os.path.join(root, candidate)
        if os.path.isdir(d) and any(fn.endswith(".md") and fn.lower() != "readme.md"
                                    for fn in os.listdir(d)):
            adr = candidate
            break
    facts["knowledge"] = {
        "adr_dir": adr,
        "existing": sorted(p for p in POINTERS if os.path.isfile(os.path.join(root, p))),
        "has_agent_dir": os.path.isdir(os.path.join(root, ".agent")),
    }
    facts["_root"] = root
    return facts


# --------------------------------------------------------------------------
# draft — build the tree, migrating existing knowledge instead of inventing it
# --------------------------------------------------------------------------
def split_existing(text):
    """Split a vendor doc by its own `##` headings into the three .agent files.

    Returns (buckets, dropped). `dropped` holds every content line that did not
    land in a bucket, so a human can see exactly what migration would lose
    instead of trusting a count.
    """
    buckets = {"architecture.md": [], "conventions.md": [], "learned.md": []}
    dropped = []
    current = "architecture.md"
    seen_heading = False
    for line in text.split("\n"):
        if line.startswith("## "):
            seen_heading = True
            head = line[3:].strip().lower()
            current = "architecture.md"
            for dest, keys in _SPLIT:
                if any(k in head for k in keys):
                    current = dest
                    break
        elif not seen_heading and line.startswith("# "):
            dropped.append(line)           # document title — re-issued per file
            continue
        buckets[current].append(line)
    return buckets, dropped


def _header(project, title, source=None):
    if source:
        return (f"# {project} — {title}\n\n"
                f"> Migrated verbatim from `{source}`. That file is now a pointer:\n"
                f"> edit this one.\n\n")
    return f"# {project} — {title}\n\n"


def draft(project, out_dir=None, env=None):
    facts = scan(project, env=env)
    root = facts["_root"]
    out = out_dir or os.path.join(_repo_root(), "temp", "agent_init", project)
    agent = os.path.join(out, ".agent")
    if os.path.isdir(out):
        shutil.rmtree(out, ignore_errors=True)
    os.makedirs(os.path.join(agent, "decisions"), exist_ok=True)

    written = []
    migrated_from = None
    dropped = []

    # 1. facts
    pub = {k: v for k, v in facts.items() if not k.startswith("_")}
    pub["generated"] = {
        "by": "agent_init",
        "spec_version": SPEC_VERSION,
        "note": "derived from the repo — re-derive with `agent_init check`",
    }
    _write(os.path.join(agent, "project.yml"), yaml_dump(pub) + "\n", written)

    # 2. prose. Three cases, in order:
    #    (a) `.agent/` already holds knowledge -> PRESERVE it. Re-running draft
    #        must never regenerate prose a human has since edited.
    #    (b) a real vendor doc exists -> migrate it.
    #    (c) nothing -> skeleton.
    # Case (a) is not hypothetical: after the first apply, CLAUDE.md is our own
    # 5-line pointer. Treating that as the source would overwrite the migrated
    # architecture with the pointer text, silently.
    # Preserve EVERY hand-written .agent/*.md, not just the three we generate:
    # a file like flows.md is someone's work, and dropping it from the draft
    # would quietly un-list it from the entry file on the next apply.
    # (index.md is excluded — `docs` owns it and regenerates it.)
    agent_src = os.path.join(root, ".agent")
    try:
        _found = sorted(fn for fn in os.listdir(agent_src)
                        if fn.endswith(".md") and fn != "index.md"
                        and os.path.isfile(os.path.join(agent_src, fn)))
    except OSError:
        _found = []
    existing_agent = {fn: os.path.join(agent_src, fn) for fn in _found}
    # But whether to migrate/skeleton still keys off the three core files only:
    # having flows.md alone must not suppress creating architecture.md.
    has_core = any(fn in existing_agent
                   for fn in ("architecture.md", "conventions.md", "learned.md"))
    src_name = None
    for candidate in ("CLAUDE.md", "AGENTS.md", "GEMINI.md"):
        path = os.path.join(root, candidate)
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8", errors="replace") as f:
            head = f.read(2000)
        if _POINTER_MARK in head:
            continue                       # our own pointer, not a knowledge source
        src_name = candidate
        break
    titles = {"architecture.md": "Architecture",
              "conventions.md": "Conventions & rules",
              "learned.md": "Learned: gotchas & known debt"}
    preserved = []
    for fn, path in sorted(existing_agent.items()):
        shutil.copyfile(path, os.path.join(agent, fn))
        written.append(os.path.join(agent, fn))
        preserved.append(f".agent/{fn}")
    if has_core:
        src_name = None
    elif src_name:
        with open(os.path.join(root, src_name), encoding="utf-8") as f:
            text = f.read()
        buckets, dropped = split_existing(text)
        before = len([ln for ln in text.split("\n") if ln.strip()])
        after = sum(len([ln for ln in v if ln.strip()]) for v in buckets.values())
        accounted = len([ln for ln in dropped if ln.strip()])
        if before - after != accounted:                 # unexplained loss = bug
            raise ValueError(f"migration would lose {before - after - accounted} "
                             f"unaccounted line(s) from {src_name} — refusing to draft")
        migrated_from = src_name
        for fn, body in buckets.items():
            content = "\n".join(body).strip()
            if not content:
                continue
            _write(os.path.join(agent, fn),
                   _header(project, titles[fn], src_name) + content + "\n", written)
    else:
        for fn, title in titles.items():
            _write(os.path.join(agent, fn),
                   _header(project, title) + _skeleton(fn, facts), written)

    # 2b. the observability standard — same text in every repo, regenerated on
    # every apply so a repo cannot sit on an old version of the policy.
    # operations.md is the repo's own answer to it, so it is only skeletonised
    # when absent: the preserve loop above already copied a filled-in one.
    _write(os.path.join(agent, "standards", "observability.md"),
           _standard_doc(), written)
    if not os.path.isfile(os.path.join(agent, "operations.md")):
        _write(os.path.join(agent, "operations.md"),
               _operations_doc(project, facts), written)

    # 3. decisions — point at existing ADRs instead of duplicating them
    adr = facts["knowledge"].get("adr_dir")
    if adr and not adr.startswith(".agent"):
        body = (f"ADRs for this repo live in `{adr}/`. Read them there.\n\n"
                f"This file exists so `.agent/` has one obvious place to look for\n"
                f"*why* the system is the way it is.\n")
    else:
        body = ("One decision per file: `NNNN-short-slug.md`, with Context /\n"
                "Decision / Alternatives / Consequences / Status.\n")
    _write(os.path.join(agent, "decisions", "README.md"),
           f"# {project} — decisions\n\n" + body, written)

    # 4. pointers
    for path, kind in POINTERS.items():
        text = _entry_doc(project, facts, agent) if kind == "entry" else _pointer_doc(path)
        _write(os.path.join(out, path), text, written)

    return {
        "ok": True,
        "project": project,
        "draft_dir": out,
        "files": sorted(os.path.relpath(w, out).replace(os.sep, "/") for w in written),
        "migrated_from": migrated_from,
        "preserved": preserved,
        "dropped_lines": [ln for ln in dropped if ln.strip()],
        "ignored_pointers": ignored_pointers(root),
        "adr_dir": adr,
        "next": f"review the draft, then: agent_init apply --project {project} --yes",
    }


def _skeleton(fn, facts):
    if fn == "architecture.md":
        layout = facts.get("layout") or {}
        mods = layout.get("modules") or layout.get("packages") or []
        body = "\n".join(f"- `{m}` — TODO: responsibility" for m in mods) or "- TODO"
        title = "Modules" if layout.get("modules") else "Packages"
        return f"## {title}\n\n" + body + "\n"
    if fn == "conventions.md":
        return ("## Rules\n\n"
                "1. **Thêm đầu vào bất đồng bộ nào (Kafka listener, scheduled job,\n"
                "   worker) thì phải thêm metric nghiệp vụ cho nó.** Đây là yêu cầu\n"
                "   bắt buộc dùng chung — `.agent/standards/observability.md` OBS-3.\n"
                "   Không có nó thì service hỏng mà health vẫn xanh.\n"
                "2. TODO: what code here must do, and must never do.\n")
    return ("## Gotchas\n\n- TODO: something that surprised someone. "
            "Include `Discovered in: <task>`.\n")



# What each knowledge file answers. Files not listed here still get a row —
# the table is built from what is actually on disk, so a hand-written
# `.agent/*.md` is never invisible to an agent reading only the entry file.
_KNOWLEDGE_BLURB = {
    "architecture.md": "How the system is built",
    "conventions.md": "Rules code must follow",
    "learned.md": "Gotchas and known debt",
    "flows.md": "What happens at runtime: lifecycles, events, call paths",
    "operations.md": "**REQUIRED** — what healthy means, and what to check after a change",
    "glossary.md": "Domain terms and what they mean here",
    "testing.md": "How this repo is tested",
}
# Order the known ones sensibly; anything else follows, alphabetically.
_KNOWLEDGE_ORDER = ["architecture.md", "conventions.md", "flows.md",
                    "operations.md", "learned.md", "glossary.md", "testing.md"]


def _knowledge_rows(agent_dir):
    """Rows for every .agent/*.md that exists, known ones first."""
    try:
        found = [f for f in os.listdir(agent_dir)
                 if f.endswith(".md") and f != "index.md"]
    except OSError:
        found = []
    ranked = sorted(found, key=lambda f: (
        _KNOWLEDGE_ORDER.index(f) if f in _KNOWLEDGE_ORDER else len(_KNOWLEDGE_ORDER),
        f))
    rows = []
    for fn in ranked:
        blurb = _KNOWLEDGE_BLURB.get(fn)
        if blurb is None:
            blurb = _first_heading(os.path.join(agent_dir, fn)) or "Project knowledge"
        rows.append(f"| `.agent/{fn}` | {blurb} |")
    return rows


def _first_heading(path):
    """Fall back to a file's own '# Title — blurb' when we have no canned one."""
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("# "):
                    title = line[2:].strip()
                    for sep in (" — ", " - ", ": "):
                        if sep in title:
                            return title.split(sep, 1)[1].strip()
                    return title
    except OSError:
        pass
    return None


_OPS_PLACEHOLDER = "TODO: người điền phần này"


def _standard_doc():
    """The requirement text, generated from OBSERVABILITY_STANDARD so the words
    an agent reads and the check that gates the repo can never disagree."""
    lines = [
        f"# Chuẩn quan sát (observability) — v{OBSERVABILITY_VERSION}",
        "",
        "> **Đây là yêu cầu bắt buộc, áp cho mọi repo.** File này được sinh ra từ",
        "> định nghĩa chuẩn dùng chung — **đừng sửa tay**. Sửa chuẩn ở toolkit rồi",
        "> chạy lại `agent_init apply`.",
        "",
        "Mục tiêu không phải là \"có nhiều biểu đồ\". Mục tiêu là: **khi hệ thống",
        "hỏng, có tín hiệu cho biết** — thay vì phải đọc code hàng giờ mới phát hiện.",
        "",
        "Mức `required` không đạt → repo bị báo **không tuân thủ**, và dòng cảnh báo",
        "sẽ xuất hiện ngay trong `AGENTS.md` để mọi agent đọc source đều thấy.",
        "",
    ]
    for rule in OBSERVABILITY_STANDARD:
        badge = "**BẮT BUỘC**" if rule["level"] == "required" else "khuyến nghị"
        lines += [
            f"## {rule['id']} — {rule['title']}",
            "",
            f"*Mức:* {badge}",
            "",
            f"**Vì sao:** {rule['why']}",
            "",
            f"**Đạt khi:** {rule['how']}",
            "",
        ]
    lines += [
        "## Kiểm tra",
        "",
        "```",
        "python agentkit.py agent_init check --project <tên>",
        "```",
        "",
        "Kết quả trả về khối `compliance`: `ok`, `failed_required`, và lý do từng",
        "mục chưa đạt. Dùng `compliance.ok` để chặn ở CI — nó tách biệt với `status`",
        "(drift), vì \"repo đã đổi mà tài liệu chưa theo\" và \"repo chưa bao giờ đạt",
        "chuẩn\" là hai vấn đề khác nhau, cách sửa khác nhau.",
        "",
    ]
    return "\n".join(lines)


def _operations_doc(project, facts):
    """Skeleton for the half a machine cannot derive: what healthy means here."""
    obs = facts.get("observability") or {}
    exposed = obs.get("endpoints_exposed") or []
    lines = [
        f"# {project} — Operations: đo lường & giám sát",
        "",
        "> Bốn luật đầu của chuẩn nói repo **có thể** đo. File này nói đội đã quyết",
        "> **đo gì** và **ngưỡng nào là bất thường**. Máy không suy ra được phần này.",
        "",
        "## Hiện trạng (do `agent_init scan` dò ra, tự cập nhật)",
        "",
        "| | |",
        "|---|---|",
        f"| Endpoint lộ ra | {', '.join(exposed) if exposed else '**không có**'} |",
        f"| Health endpoint | {'có' if obs.get('health_endpoint') else '**không**'} |",
        f"| Metric endpoint | {'có' if obs.get('metrics_endpoint') else '**không**'} |",
        f"| File có đầu vào bất đồng bộ | {obs.get('async_intake_files', 0)} |",
        f"| File có metric nghiệp vụ | {obs.get('custom_meter_files', 0)} |",
        f"| File có correlation id | {obs.get('correlation_files', 0)} |",
        f"| Thư viện tracing | {obs.get('tracing_library') or 'không có'}"
        f" (bật: {obs.get('tracing_enabled')}) |",
        "",
        "## 'Khoẻ' nghĩa là gì ở service này",
        "",
        f"{_OPS_PLACEHOLDER}. Liệt kê **3–5 tín hiệu**, không nhiều hơn. Mỗi tín hiệu",
        "cần: tên metric, ngưỡng, và điều gì xảy ra với người dùng khi nó vượt ngưỡng.",
        "",
        "| Tín hiệu | Ngưỡng bất thường | Ảnh hưởng tới ai |",
        "|---|---|---|",
        "| | | |",
        "",
        "## Sau khi sửa thì kiểm gì",
        "",
        "Phần được dùng nhiều nhất. Với mỗi vùng hay sửa, ghi cách xác nhận thay đổi",
        "chạy đúng — tên metric hoặc truy vấn log cụ thể, không phải \"kiểm tra log\".",
        "",
        "| Sửa vùng nào | Kiểm gì |",
        "|---|---|",
        "| | |",
        "",
        "## Dashboard & alert",
        "",
        "Link tới nơi xem thật. Không có thì ghi \"chưa có\" — đừng để trống.",
        "",
        "## Điểm mù đã biết",
        "",
        "Thứ hiện **không** đo được, và hệ quả khi nó hỏng.",
        "",
    ]
    return "\n".join(lines)


def _entry_doc(project, facts, agent_dir=None):
    st = facts.get("stack") or {}
    cmds = facts.get("commands") or {}
    adr = (facts.get("knowledge") or {}).get("adr_dir") or ".agent/decisions"
    rows = _knowledge_rows(agent_dir) if agent_dir else [
        "| `.agent/architecture.md` | How the system is built |",
        "| `.agent/conventions.md` | Rules code must follow |",
        "| `.agent/learned.md` | Gotchas and known debt |",
    ]
    lines = [
        f"# {project}",
        "",
        "Agent-readable project knowledge lives in **`.agent/`**. Read it before",
        "changing code: it is the single source of truth, and every other agent",
        "file in this repo is only a pointer to it.",
        "",
    ]
    # The compliance banner sits above the table on purpose: the entry file is
    # the one thing every agent reads, so an unmet requirement has to be
    # unavoidable rather than filed away in a document nobody opens.
    obs = facts.get("observability") or {}
    fails = [r for r in OBSERVABILITY_STANDARD
             if r["level"] == "required" and r["id"] in (obs.get("fails") or [])]
    if fails:
        lines += [
            f"> ⚠️ **Repo này CHƯA đạt chuẩn quan sát v{OBSERVABILITY_VERSION}** — "
            f"thiếu {len(fails)} yêu cầu bắt buộc:",
            ">",
        ]
        lines += [f"> - **{r['id']}** — {r['title']}" for r in fails]
        lines += [
            ">",
            "> Nghĩa là service này có thể hỏng mà không phát ra tín hiệu nào. "
            "Đang thêm/sửa đầu vào bất đồng bộ (Kafka, scheduler, worker) thì "
            "**xử lý luôn phần đo**, đừng để lần sau.",
            "> Chi tiết: `.agent/standards/observability.md`",
            "",
        ]
    elif obs:
        lines += [
            f"> ✅ Đạt chuẩn quan sát v{OBSERVABILITY_VERSION}. Giữ nguyên trạng thái "
            "này khi thêm đầu vào bất đồng bộ mới — xem "
            "`.agent/standards/observability.md`.",
            "",
        ]
    lines += [
        "| File | What it answers |",
        "|---|---|",
        "| `.agent/project.yml` | Machine facts: stack, commands, modules, datastores |",
    ] + rows + [
        f"| `{adr}/` | Why it is built that way |",
        "",
        "## Quick facts",
        "",
    ]
    stack_line = st.get("framework") or st.get("lang") or "unknown"
    if st.get("java_version"):
        stack_line += f" (Java {st['java_version']})"
    lines.append(f"- Stack: {stack_line}")
    if cmds.get("build"):
        lines.append(f"- Build: `{cmds['build']}`")
    if cmds.get("test"):
        lines.append(f"- Test: `{cmds['test']}`")
    layout = facts.get("layout") or {}
    if layout.get("base_package"):
        lines.append(f"- Base package: `{layout['base_package']}`")
    for key, label in (("modules", "Modules"), ("packages", "Packages")):
        items = layout.get(key) or []
        if items:
            lines.append(f"- {label} ({len(items)}): {', '.join(items)}")
    lines += [
        "",
        "## Editing this knowledge",
        "",
        "Edit the files under `.agent/`. Never edit a pointer file and never copy",
        "content out of `.agent/` into one — copies drift, pointers cannot.",
        "",
    ]
    return "\n".join(lines)


def _pointer_doc(path):
    head = ""
    if path.endswith(".mdc"):
        head = "---\ndescription: Project knowledge\nalwaysApply: true\n---\n\n"
    return (head +
            "Project knowledge for this repo lives in `AGENTS.md` and `.agent/`.\n"
            "Read `AGENTS.md` first, then the `.agent/` file it points you to.\n"
            "\n"
            "This file is a pointer on purpose: it is generated, and editing it\n"
            "would create a second source of truth. Edit `.agent/` instead.\n")


# Which agent each pointer serves. Only used to decide whether a vendor has
# been left with no reachable pointer at all.
_VENDOR = {
    "AGENTS.md": "generic",
    "CLAUDE.md": "claude",
    "GEMINI.md": "gemini",
    ".cursorrules": "cursor",
    ".cursor/rules/00-agent.mdc": "cursor",
    ".github/copilot-instructions.md": "copilot",
}


def ignored_pointers(root):
    """Vendors the repo's .gitignore would cut off entirely.

    A pointer that cannot be committed does not exist for anyone else on the
    team. But a vendor with two candidate locations (Cursor: `.cursorrules` and
    `.cursor/rules/`) is fine as long as ONE survives — warning about the other
    is noise, and noise is how a check earns the right to be ignored.
    """
    ignored, reachable = [], set()
    for path in POINTERS:
        rel = path.replace("/", os.sep)
        r = subprocess.run(["git", "check-ignore", "-q", rel],
                           cwd=root, capture_output=True, text=True)
        if r.returncode == 0:
            ignored.append(path)
        else:
            reachable.add(_VENDOR.get(path, path))
    return [p for p in ignored if _VENDOR.get(p, p) not in reachable]


def _write(path, text, written):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    written.append(path)


def _repo_root():
    d = _HERE
    for _ in range(6):
        if os.path.isdir(os.path.join(d, ".git")) or os.path.isfile(os.path.join(d, "CLAUDE.md")):
            return d
        d = os.path.dirname(d)
    return _HERE


# --------------------------------------------------------------------------
# apply / check
# --------------------------------------------------------------------------
def apply(project, yes=False, draft_dir=None, env=None):
    facts = scan(project, env=env)
    root = facts["_root"]
    src = draft_dir or os.path.join(_repo_root(), "temp", "agent_init", project)
    if not os.path.isdir(src):
        raise ValueError(f"no draft at {src} — run `agent_init draft --project {project}` first")

    plan = []
    for dirpath, _, filenames in os.walk(src):
        for fn in filenames:
            rel = os.path.relpath(os.path.join(dirpath, fn), src).replace(os.sep, "/")
            exists = os.path.isfile(os.path.join(root, rel))
            plan.append({"file": rel, "action": "overwrite" if exists else "create"})
    plan.sort(key=lambda p: p["file"])

    if not yes:
        return {"ok": False, "dry_run": True, "project": project, "target": root,
                "plan": plan,
                "next": "re-run with --yes to write these files into the repo"}

    for item in plan:
        s = os.path.join(src, item["file"].replace("/", os.sep))
        d = os.path.join(root, item["file"].replace("/", os.sep))
        os.makedirs(os.path.dirname(d), exist_ok=True)
        shutil.copyfile(s, d)
    ignored = ignored_pointers(root)
    out = {"ok": True, "project": project, "target": root,
           "written": [p["file"] for p in plan],
           "next": "review `git diff` in the product repo, then commit"}
    if ignored:
        out["ignored_pointers"] = ignored
        out["warning"] = ("these pointers are gitignored, so teammates will never "
                          "see them — un-ignore them or drop them: " + ", ".join(ignored))
    return out


def check(project, env=None):
    """Drift: does the committed project.yml still describe the code?"""
    facts = scan(project, env=env)
    root = facts["_root"]
    path = os.path.join(root, ".agent", "project.yml")
    if not os.path.isfile(path):
        return {"ok": False, "project": project, "status": "missing",
                "message": ".agent/project.yml not found — run draft + apply first"}
    with open(path, encoding="utf-8") as f:
        committed = yaml_load(f.read())

    drift = []
    for label, old, new in (
        ("stack.framework", (committed.get("stack") or {}).get("framework"),
         (facts.get("stack") or {}).get("framework")),
        ("stack.java_version", (committed.get("stack") or {}).get("java_version"),
         (facts.get("stack") or {}).get("java_version")),
        ("default_branch", committed.get("default_branch"), facts.get("default_branch")),
        ("verification.ci", (committed.get("verification") or {}).get("ci"),
         (facts.get("verification") or {}).get("ci")),
    ):
        if str(old) != str(new):
            drift.append({"field": label, "committed": old, "actual": new})

    ignored = ignored_pointers(root)
    if ignored:
        drift.append({"field": "pointers.gitignored", "committed": [], "actual": ignored})

    for key in ("modules", "packages"):
        old_list = (committed.get("layout") or {}).get(key) or []
        new_list = (facts.get("layout") or {}).get(key) or []
        if sorted(old_list) != sorted(new_list):
            drift.append({"field": f"layout.{key}",
                          "added": sorted(set(new_list) - set(old_list)),
                          "removed": sorted(set(old_list) - set(new_list))})

    # Compliance is reported beside drift, not folded into it: "the repo moved"
    # and "the repo never met the standard" need different fixes, and a caller
    # that gates CI on one must not be tripped by the other.
    obs = facts.get("observability") or {}
    intake_files = observability(root)[1]
    reqs = {}
    for rule in OBSERVABILITY_STANDARD:
        passed = rule["id"] in (obs.get("meets") or [])
        reqs[rule["id"]] = {
            "level": rule["level"], "title": rule["title"],
            "status": "pass" if passed else "fail",
        }
        if not passed:
            reqs[rule["id"]]["how"] = rule["how"]
    failed_required = [r["id"] for r in OBSERVABILITY_STANDARD
                       if r["level"] == "required"
                       and reqs[r["id"]]["status"] == "fail"]
    compliance = {
        "ok": not failed_required,
        "standard": f"observability {OBSERVABILITY_VERSION}",
        "failed_required": failed_required,
        "failed_advisory": [r["id"] for r in OBSERVABILITY_STANDARD
                            if r["level"] == "advisory"
                            and reqs[r["id"]]["status"] == "fail"],
        "requirements": reqs,
    }
    if "OBS-3" in failed_required and intake_files:
        compliance["uninstrumented_intake"] = intake_files

    return {"ok": not drift, "project": project,
            "status": "in-sync" if not drift else "drift",
            "drift": drift,
            "compliance": compliance,
            "message": "knowledge still matches the code" if not drift else
            "the repo moved and .agent/ did not — reconcile before trusting it"}


# --------------------------------------------------------------------------
# add — clone, register and draft in one step
# --------------------------------------------------------------------------
def parse_repo_url(url):
    """`https://host/group/sub/name.git` or `git@host:group/name.git` -> (name, path)."""
    u = url.strip().rstrip("/")
    if u.startswith("git@") or (u.startswith("ssh://") and "@" in u):
        path = u.split(":", 1)[-1] if ":" in u.split("@", 1)[-1] else u
        path = path.split("@", 1)[-1].split(":", 1)[-1]
    else:
        after = u.split("://", 1)[-1]
        if "/" not in after:                  # host only — there is no project here
            raise ValueError(f"cannot read a project path out of '{url}'")
        path = after.split("/", 1)[1]
    path = path[:-4] if path.endswith(".git") else path
    path = path.strip("/")
    if not path:
        raise ValueError(f"cannot read a project path out of '{url}'")
    return path.rsplit("/", 1)[-1], path


def _gitlab_project(repo_path):
    """Ask GitLab for the project's id and default branch. Best effort: a repo
    still initialises fine without them, just with less in project.yml."""
    try:
        sys.path.insert(0, os.path.join(_SKILLS, "dev-automation", "tools"))
        import urllib.parse

        import config
        import gitlab_api
        base = (config.get("GITLAB_URL") or "").rstrip("/")
        if not base or not config.get("GITLAB_PRIVATE_TOKEN"):
            return {"error": "GITLAB_URL/GITLAB_PRIVATE_TOKEN not configured"}
        url = f"{base}/api/v4/projects/{urllib.parse.quote(repo_path, safe='')}"
        d = gitlab_api._request(url)
        return {"id": d.get("id"), "default_branch": d.get("default_branch"),
                "web_url": d.get("web_url")}
    except Exception as e:                    # noqa: BLE001 — never block init on the API
        return {"error": f"{type(e).__name__}: {e}"}


def add(repo_url, name=None, branch=None, yes=False):
    """Clone -> look up -> register -> draft. Stops before touching the repo.

    The last step is `draft`, never `apply`: the point of the review step is
    that a human sees what would land in a company repo before it lands.
    """
    derived_name, repo_path = parse_repo_url(repo_url)
    name = name or derived_name
    work = os.environ.get("WORK_DIR") or os.path.join(_repo_root(), "work")
    clone_dir = os.path.join(work, name)

    steps = [
        {"step": "clone", "detail": f"{repo_url} -> {clone_dir}",
         "skipped": os.path.isdir(os.path.join(clone_dir, ".git"))},
        {"step": "lookup", "detail": f"GitLab project id for {repo_path}"},
        {"step": "register", "detail": f"work/projects.json <- {name}"},
        {"step": "draft", "detail": "build .agent/ into temp/ for review"},
    ]
    if not yes:
        return {"ok": False, "dry_run": True, "name": name, "repo_path": repo_path,
                "clone_dir": clone_dir, "steps": steps,
                "next": "re-run with --yes (clones + writes the registry; "
                        "does NOT touch the product repo)"}

    out = {"ok": True, "name": name, "repo_path": repo_path, "clone_dir": clone_dir}

    # 1. clone
    if os.path.isdir(os.path.join(clone_dir, ".git")):
        out["clone"] = "already present"
    else:
        os.makedirs(work, exist_ok=True)
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
        r = subprocess.run(["git", "clone", repo_url, clone_dir],
                           capture_output=True, text=True, env=env)
        if r.returncode != 0:
            return {"error": True, "step": "clone",
                    "message": (r.stderr or r.stdout).strip()[-800:]}
        out["clone"] = "cloned"

    # 2. look up what only the server knows
    meta = _gitlab_project(repo_path)
    out["gitlab"] = meta
    default_branch = branch or meta.get("default_branch") \
        or _run(["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
                clone_dir).split("/", 1)[-1] or None

    # 3. register — reuse the writer the rest of the toolkit already uses
    sys.path.insert(0, os.path.join(_SKILLS, "new-product", "tools"))
    import registry
    reg = registry.cmd_add(argparse.Namespace(
        name=name, clone_dir=clone_dir, stack="",
        gitlab_project_id=str(meta.get("id") or ""), gitlab_path=repo_path,
        azure_project="", default_target_branch=default_branch or "main",
        build_cmd="", test_cmd="", lint_cmd="", force=True))
    out["registry"] = reg.get("entry", reg)

    # 4. draft
    out["draft"] = draft(name)
    out["next"] = (f"review {out['draft']['draft_dir']}, then: "
                   f"agent_init apply --project {name} --yes")
    return out


# --------------------------------------------------------------------------
# docs — sort an existing documentation pile into the three tiers
# --------------------------------------------------------------------------
# A repo that has been worked on for a year accumulates plans, progress reports
# and one-off reviews next to its real documentation. To an agent they all read
# as current intent, so a stale `PHASE2_PROGRESS.md` is worse than no document
# at all. Three tiers, by how much an agent should trust them:
#
#   .agent/decisions/  canon — why the system is the way it is (ADRs)
#   docs/reference/    still true, consult on demand (api, features, perf, ...)
#   docs/guides/       cross-cutting rules that are still authoritative
#   docs/archive/      point-in-time plans/reports — explicitly NOT authoritative
#
# Classification is deterministic so the plan can be reviewed and re-run.

# A curated subdirectory is assumed already sorted: keep its subtree intact.
_REFERENCE_DIRS = ("api", "features", "perf", "security", "flow", "ai-agent",
                   "ai", "postman", "architecture")

# Point-in-time documents. Applied to files sitting directly in docs/ only —
# inside a curated directory the same word usually names a real spec.
_ARCHIVE_PAT = re.compile(
    r"(^|[_\-])(plan\d*|plans?|progress|report|review|summary|check|checklist"
    r"|wave\d*|phase\d*|note|log|todo|draft)([_\-.]|$)", re.IGNORECASE)


def plan_docs(root, include_reference=False):
    """Decide where every existing document should live. Pure: moves nothing.

    Curated subtrees (`api/`, `perf/`, ...) are left alone by default. They are
    already sorted, source comments link into them by path, and relocating them
    buys tidiness at the price of rewriting the code that points there. Pass
    `include_reference` when that trade is worth it for a given repo.
    """
    plan = []
    docs = os.path.join(root, "docs")
    if not os.path.isdir(docs):
        return plan
    for dirpath, dirnames, filenames in os.walk(docs):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for fn in sorted(filenames):
            src = os.path.relpath(os.path.join(dirpath, fn), root).replace(os.sep, "/")
            rest = src[len("docs/"):]
            top = rest.split("/")[0] if "/" in rest else None

            if top == "adr":
                dst, bucket, why = ".agent/decisions/" + rest[len("adr/"):], \
                    "canon", "an ADR is canon: it says why"
            elif top in _REFERENCE_DIRS or top is not None:
                bucket = "reference"
                why = f"curated `{top}/` subtree"
                if include_reference:
                    dst = "docs/reference/" + rest
                else:
                    dst = src                     # stays put; indexed, not moved
                    why += " — left in place (source comments link to it)"
            elif _ARCHIVE_PAT.search(os.path.splitext(fn)[0]):
                dst, bucket, why = "docs/archive/" + fn, "archive", \
                    "point-in-time document at docs root"
            else:
                dst, bucket, why = "docs/guides/" + fn, "guide", \
                    "cross-cutting doc, still authoritative"
            plan.append({"src": src, "dst": dst, "bucket": bucket, "why": why,
                         "moves": dst != src})
    return plan


_LINKABLE = (".md", ".mdc", ".java", ".kt", ".xml", ".yml", ".yaml", ".json",
             ".sh", ".properties", ".sql", ".txt")


def _rewrite_links(root, moves, extra_subs=()):
    """Repoint every reference that names a path we just moved.

    Source comments point at design docs by path (68 java files do so in this
    repo), so a move that only fixes markdown leaves the code pointing at
    nothing. Cheap to include them; expensive to discover later.
    """
    touched = []
    targets = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d not in (".git", "target", "node_modules", "build")]
        targets += [os.path.join(dirpath, fn) for fn in filenames
                    if fn.endswith(_LINKABLE)]
    # longest source first so `docs/api/x.md` is not half-replaced by `docs/api`
    subs = sorted(((m["src"], m["dst"]) for m in moves),
                  key=lambda p: len(p[0]), reverse=True) + list(extra_subs)
    for path in targets:
        try:
            with open(path, encoding="utf-8") as f:
                text = original = f.read()
        except (OSError, UnicodeDecodeError):
            continue
        for old, new in subs:
            if old in text:
                text = text.replace(old, new)
        if text != original:
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
            touched.append(os.path.relpath(path, root).replace(os.sep, "/"))
    return sorted(touched)


def _index_doc(project, plan, adr_files):
    by_bucket = {}
    for item in plan:
        by_bucket.setdefault(item["bucket"], []).append(item["dst"])
    lines = [
        f"# {project} — document index",
        "",
        "Generated by `agent_init docs`. It exists so an agent can tell which",
        "documents to trust before reading any of them.",
        "",
        "| Tier | Where | Trust |",
        "|---|---|---|",
        "| Canon | `.agent/` + `.agent/decisions/` | Authoritative. Read first. |",
        "| Guides | `docs/guides/` | Cross-cutting rules, still authoritative. |",
        "| Reference | `docs/reference/` | True but detailed. Read on demand. |",
        "| Archive | `docs/archive/` | **Point-in-time. NOT current intent.** |",
        "",
        f"## Decisions ({len(adr_files)})",
        "",
    ]
    lines += [f"- `.agent/decisions/{fn}`" for fn in adr_files] or ["- none yet"]
    for bucket, title in (("guide", "Guides"), ("reference", "Reference"),
                          ("archive", "Archive")):
        items = sorted(by_bucket.get(bucket, []))
        lines += ["", f"## {title} ({len(items)})", ""]
        if bucket == "archive" and items:
            lines += ["> Kept for history. Do not treat any of these as the",
                      "> current plan — check `.agent/` and git first.", ""]
        lines += [f"- `{p}`" for p in items] or ["- none"]
    lines.append("")
    return "\n".join(lines)


def docs(project, yes=False, env=None, include_reference=False):
    facts = scan(project, env=env)
    root = facts["_root"]
    plan = plan_docs(root, include_reference=include_reference)
    summary = {}
    for item in plan:
        b = item["bucket"]
        summary.setdefault(b, {"files": 0, "moves": 0})
        summary[b]["files"] += 1
        summary[b]["moves"] += 1 if item["moves"] else 0
    todo = [i for i in plan if i["moves"]]

    if not yes:
        return {"ok": False, "dry_run": True, "project": project, "target": root,
                "summary": summary, "will_move": len(todo), "moves": todo,
                "next": "review, then re-run with --yes (git mv, nothing deleted)"}

    moved, failed = [], []
    for item in todo:
        s = os.path.join(root, item["src"].replace("/", os.sep))
        d = os.path.join(root, item["dst"].replace("/", os.sep))
        if not os.path.isfile(s):
            continue
        os.makedirs(os.path.dirname(d), exist_ok=True)
        r = subprocess.run(["git", "mv", item["src"], item["dst"]],
                           cwd=root, capture_output=True, text=True)
        if r.returncode != 0:
            try:                              # untracked file: plain move
                shutil.move(s, d)
            except OSError as e:
                failed.append({"file": item["src"], "error": str(e)})
                continue
        moved.append(item["dst"])

    # `git mv` leaves the source directory behind; an empty `docs/adr/` would
    # keep looking like the home of decisions.
    for dirpath, dirnames, filenames in os.walk(os.path.join(root, "docs"), topdown=False):
        if not dirnames and not filenames:
            try:
                os.rmdir(dirpath)
            except OSError:
                pass

    adr_files = sorted(os.listdir(os.path.join(root, ".agent", "decisions"))) \
        if os.path.isdir(os.path.join(root, ".agent", "decisions")) else []
    adr_files = [f for f in adr_files if f != "README.md"]

    # `docs/adr/0001..0003` style ranges are prose, not paths — fix them too
    touched = _rewrite_links(root, todo, extra_subs=(("docs/adr/", ".agent/decisions/"),))

    # ADRs moved, so the facts moved with them: re-emit project.yml or `check`
    # would compare knowledge against a stale snapshot.
    refreshed = scan(project, env=env)
    pub = {k: v for k, v in refreshed.items() if not k.startswith("_")}
    pub["generated"] = {"by": "agent_init", "spec_version": SPEC_VERSION,
                        "note": "derived from the repo — re-derive with `agent_init check`"}
    with open(os.path.join(root, ".agent", "project.yml"), "w",
              encoding="utf-8", newline="\n") as f:
        f.write(yaml_dump(pub) + "\n")

    index_path = os.path.join(root, ".agent", "index.md")
    with open(index_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(_index_doc(project, plan, adr_files))

    return {"ok": True, "project": project, "target": root, "summary": summary,
            "moved": len(moved), "failed": failed,
            "project_yml_refreshed": True,
            "links_rewritten_in": touched,
            "index": ".agent/index.md",
            "next": "review `git status` + `git diff` in the product repo, then commit"}


def main():
    ap = argparse.ArgumentParser(
        description="Vendor-neutral project knowledge under .agent/")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add", help="clone + register + draft a repo in one step")
    a.add_argument("--repo", required=True, help="git URL")
    a.add_argument("--name", default=None, help="registry name (default: from the URL)")
    a.add_argument("--branch", default=None, help="default branch (default: ask GitLab)")
    a.add_argument("--yes", action="store_true",
                   help="[WRITE] clone + write the registry (product repo untouched)")

    for name in ("scan", "draft", "apply", "check", "docs"):
        p = sub.add_parser(name)
        p.add_argument("--project", required=True)
        p.add_argument("--env", default=None)
        if name == "draft":
            p.add_argument("--out", default=None)
        if name == "apply":
            p.add_argument("--yes", action="store_true",
                           help="[WRITE] actually copy the draft into the repo")
            p.add_argument("--draft-dir", default=None)
        if name == "docs":
            p.add_argument("--yes", action="store_true",
                           help="[WRITE] git mv the existing docs into the tiers")
            p.add_argument("--include-reference", action="store_true",
                           help="also relocate curated subtrees (api/, perf/, ...) — "
                                "this rewrites every source comment that links to them")
    args = ap.parse_args()

    try:
        if args.cmd == "add":
            out = add(args.repo, name=args.name, branch=args.branch, yes=args.yes)
        elif args.cmd == "scan":
            out = {k: v for k, v in scan(args.project, env=args.env).items()
                   if not k.startswith("_")}
        elif args.cmd == "draft":
            out = draft(args.project, out_dir=args.out, env=args.env)
        elif args.cmd == "apply":
            out = apply(args.project, yes=args.yes, draft_dir=args.draft_dir, env=args.env)
        elif args.cmd == "docs":
            out = docs(args.project, yes=args.yes, env=args.env,
                       include_reference=args.include_reference)
        else:
            out = check(args.project, env=args.env)
    except ValueError as e:
        out = {"error": True, "message": str(e)}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 1 if isinstance(out, dict) and out.get("error") else 0


if __name__ == "__main__":
    sys.exit(main())
