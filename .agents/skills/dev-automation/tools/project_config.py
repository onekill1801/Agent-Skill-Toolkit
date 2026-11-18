#!/usr/bin/env python3
"""Per-project, per-environment config for the stack-verify probes.

Each project in `<work_dir>/projects.json` may target several environments
(local / dev / uat / sandbox / prod). A `stack` block holds shared defaults; each
`environments.<env>` block overrides it (deep-merged). The resolved values are
injected into os.environ via setdefault, so the existing `config.get(...)` calls
in every probe pick them up unchanged.

Priority (highest first): explicit CLI flag > real shell env > resolved env stack
(environments.<env> over stack base) > global .env defaults.

work_dir = $WORK_DIR or <repo>/work (same as test_runner.py).

projects.json shape (only `stack` / `environments` are read here):
{
  "atask": {
    "default_env": "dev",
    "protected_envs": ["prod", "production"],
    "stack": {                                   # shared defaults across envs
      "db": {"engine": "postgres", "user": "atask_app", "name": "atask", "port": "5432"},
      "redis": {"port": "6379", "db": "0"},
      "jenkins": {"job": "group/job/atask-ci"}
    },
    "environments": {
      "local": {"api_base_url": "http://localhost:8080",
                "db": {"host": "localhost"}, "redis": {"host": "localhost"},
                "kafka_rest_url": "http://localhost:8082"},
      "dev":   {"api_base_url": "https://atask.dev",
                "db": {"host": "pg.dev"}, "redis": {"host": "redis.dev"},
                "kafka_rest_url": "http://kafka-rest.dev:8082"},
      "uat":   {"api_base_url": "https://atask.uat", "db": {"host": "pg.uat"}},
      "prod":  {"api_base_url": "https://atask.prod", "db": {"host": "pg.prod"}}
    }
  }
}

Backward compatible: a project with only `stack` and no `environments` resolves to
that single stack regardless of --env.
"""

import json
import os

DEFAULT_PROTECTED = ["prod", "production"]

# Flat registry key -> environment variable.
_FLAT = {
    "api_base_url": "API_BASE_URL",
    "api_auth_header": "API_AUTH_HEADER",
    "pg_url": "PG_URL",
    "kafka_rest_url": "KAFKA_REST_URL",
    "kafka_rest_auth": "KAFKA_REST_AUTH",
}

# Nested section -> {registry key -> environment variable}.
_NESTED = {
    "db": {"host": "DB_HOST", "port": "DB_PORT", "user": "DB_USER",
           "password": "DB_PASSWORD", "name": "DB_NAME",
           "engine": "DB_ENGINE", "schema": "DB_SCHEMA"},
    "redis": {"host": "REDIS_HOST", "port": "REDIS_PORT",
              "password": "REDIS_PASSWORD", "db": "REDIS_DB"},
    "jenkins": {"url": "JENKINS_URL", "user": "JENKINS_USER", "token": "JENKINS_TOKEN"},
    "kafka_ui": {"url": "KAFKA_UI_URL", "login_path": "KAFKA_UI_LOGIN_PATH",
                 "user": "KAFKA_UI_USER", "password": "KAFKA_UI_PASSWORD"},
}


def _repo_root() -> str:
    """Walk up from this file to the repo root (where CLAUDE.md / .git lives)."""
    search = os.path.dirname(os.path.abspath(__file__))
    for _ in range(6):
        if os.path.isdir(os.path.join(search, ".git")) or os.path.isfile(os.path.join(search, "CLAUDE.md")):
            return search
        search = os.path.dirname(search)
    return os.path.dirname(os.path.abspath(__file__))


def _work_dir() -> str:
    """Registry dir: $WORK_DIR if set, else <repo>/work."""
    return os.environ.get("WORK_DIR") or os.path.join(_repo_root(), "work")


def load(name: str) -> dict:
    """Return the project entry from projects.json, or {} if missing/unreadable."""
    path = os.path.join(_work_dir(), "projects.json")
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f).get(name, {})
    except (OSError, json.JSONDecodeError):
        return {}


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override onto base (one level of nested dicts is enough)."""
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


class ResolveError(Exception):
    """Raised when an environment cannot be resolved unambiguously."""


def resolve(name: str, env: str = None) -> dict:
    """Resolve a project + environment into a merged stack + safety metadata.

    Returns {"project", "env", "stack", "protected", "known_envs"}.
    Raises ResolveError if the project defines multiple environments but none was
    selected (no --env and no default_env) — we never guess which env to hit.
    """
    if not name:
        return {"project": None, "env": None, "stack": {}, "protected": False, "known_envs": []}

    block = load(name)
    base = block.get("stack", {})
    envs = block.get("environments", {})
    known = sorted(envs.keys())

    chosen = env or block.get("default_env")
    if envs and not chosen:
        raise ResolveError(
            f"project '{name}' has multiple environments; pass --env (one of: {', '.join(known)})"
        )
    if chosen and envs and chosen not in envs:
        raise ResolveError(
            f"unknown env '{chosen}' for project '{name}' (known: {', '.join(known) or 'none'})"
        )

    merged = _deep_merge(base, envs.get(chosen, {})) if chosen else base

    # Spring gap-fill: most Java projects keep the real per-env DB connection in
    # application-<env>.yml, not in the registry. Fill ONLY the keys the registry
    # leaves empty (registry always wins), so probes hit the same DB the app uses.
    clone_dir = block.get("clone_dir")
    db = merged.get("db") or {}
    if clone_dir and os.path.isdir(clone_dir) and (not db.get("host") or not db.get("name")):
        try:
            import spring_config
            sc = spring_config.load(clone_dir, env=chosen) or {}
        except Exception:  # noqa: BLE001 — gap-fill must never break resolution
            sc = {}
        sdb = sc.get("db") or {}
        if sdb:
            merged = dict(merged)
            merged["db"] = {**sdb, **{k: v for k, v in db.items() if v not in (None, "")}}
            merged["_spring_source"] = (sc.get("sources") or [""])[-1]

    protected_list = [e.lower() for e in block.get("protected_envs", DEFAULT_PROTECTED)]
    protected = bool(chosen) and chosen.lower() in protected_list
    return {"project": name, "env": chosen, "stack": merged,
            "protected": protected, "known_envs": known}


def inject(stack: dict) -> dict:
    """Inject a resolved stack dict into os.environ (setdefault). Returns applied keys."""
    applied = {}
    for key, env_key in _FLAT.items():
        if stack.get(key) not in (None, ""):
            os.environ.setdefault(env_key, str(stack[key]))
            applied[env_key] = "registry"
    for section, mapping in _NESTED.items():
        sect = stack.get(section)
        if not isinstance(sect, dict):
            continue
        for key, env_key in mapping.items():
            if sect.get(key) not in (None, ""):
                os.environ.setdefault(env_key, str(sect[key]))
                applied[env_key] = "registry"
    return applied


def guard(ctx: dict, mutating: bool, allow_prod: bool):
    """Return an error dict to abort if a mutating op targets a protected env.

    Read-only ops are always allowed. Mutating ops against a protected env
    (e.g. prod) require allow_prod=True (the --allow-prod flag).
    """
    if mutating and ctx.get("protected") and not allow_prod:
        return {
            "error": True, "passed": False,
            "message": (f"refusing a mutating operation against protected env "
                        f"'{ctx['env']}' (project '{ctx['project']}'). "
                        f"Pass --allow-prod to override."),
        }
    return None


def apply_args(args, mutating: bool = False):
    """Convenience for tools: resolve(--project,--env) -> inject -> guard.

    Reads args.project, args.env, args.allow_prod (any may be absent). Returns
    None on success (env injected), or an error dict the tool should emit & exit.
    """
    project = getattr(args, "project", None)
    if not project:
        return None
    try:
        ctx = resolve(project, getattr(args, "env", None))
    except ResolveError as e:
        return {"error": True, "passed": False, "message": str(e)}
    inject(ctx["stack"])
    args._ctx = ctx  # stash for tools that want env-aware fallbacks (e.g. jenkins job)
    return guard(ctx, mutating, getattr(args, "allow_prod", False))


def target_branch(name: str, env: str = None) -> str:
    """Git branch to base work on / target MRs at for the resolved env.

    Resolution: environments.<env>.branch  >  project.default_target_branch.
    With branch-per-env flow, choosing an env picks its branch (dev/uat/prod/...).
    """
    try:
        ctx = resolve(name, env)
    except ResolveError:
        ctx = {"stack": {}}
    b = ctx.get("stack", {}).get("branch")
    return b or load(name).get("default_target_branch", "")


def default_jenkins_job(name: str, env: str = None) -> str:
    """The project's configured Jenkins job path for the resolved env, if any."""
    try:
        ctx = resolve(name, env)
    except ResolveError:
        return ""
    return ctx["stack"].get("jenkins", {}).get("job", "")
